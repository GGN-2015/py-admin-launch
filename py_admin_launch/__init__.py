"""Cross-platform helpers for launching a command as administrator/root."""

import argparse
import ctypes
import os
import platform
import signal
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence, Union


PathLike = Union[str, os.PathLike]
Command = Union[PathLike, Sequence[PathLike]]


class AdminLaunchError(RuntimeError):
    """Raised when a command cannot be launched with administrator privileges."""


@dataclass(frozen=True)
class LaunchResult:
    """Result returned by :func:`launch`.

    ``elevated`` means the command was launched with administrator/root rights.
    When the current process is already elevated, this is true even though no
    prompt is shown. ``returncode`` is available when ``wait=True``.
    """

    elevated: bool
    returncode: Optional[int] = None
    pid: Optional[int] = None


def is_admin() -> bool:
    """Return whether this process is already running as administrator/root."""

    system = platform.system()
    if system == "Windows":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid is not None and geteuid() == 0)


def launch(
    command: Command,
    *args: PathLike,
    cwd: Optional[PathLike] = None,
    wait: bool = False,
) -> LaunchResult:
    """Launch ``command`` as administrator/root when needed.

    If the current process is already elevated, the command is launched
    directly as the current user. Otherwise, the platform-specific elevation
    mechanism is used:

    * Linux: terminal/headless ``sudo`` or desktop ``pkexec``.
    * Windows: a hidden UAC helper using the caller's standard streams.
    * macOS: ``osascript`` with ``administrator privileges``.

    ``command`` is safest as a sequence, for example
    ``launch(["python", "-m", "http.server"])``. A string is treated as the
    executable path, and any extra positional arguments are appended.
    """

    argv = _normalize_command(command, args)
    normalized_cwd = None if cwd is None else os.fspath(cwd)

    if is_admin():
        return _launch_direct(argv, cwd=normalized_cwd, wait=wait, elevated=True)

    system = platform.system()
    if system == "Windows":
        from ._windows import launch_windows
        return launch_windows(argv, cwd=normalized_cwd, wait=wait)
    if system == "Darwin":
        return _launch_macos(argv, cwd=normalized_cwd, wait=wait)
    if system == "Linux":
        return _launch_linux(argv, cwd=normalized_cwd, wait=wait)

    raise AdminLaunchError(f"unsupported platform: {system or 'unknown'}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command line entry point."""

    parser = argparse.ArgumentParser(
        prog="py-admin-launch",
        description="Launch a command with administrator/root privileges.",
    )
    wait_group = parser.add_mutually_exclusive_group()
    wait_group.add_argument(
        "--wait",
        action="store_true",
        dest="wait",
        default=True,
        help="wait for the command to finish and return its exit code (default)",
    )
    wait_group.add_argument(
        "--no-wait",
        action="store_false",
        dest="wait",
        help="start the command and return after the elevation request is handed off",
    )
    parser.add_argument(
        "--cwd",
        help="working directory for the launched command",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="command to launch; use -- before commands that start with an option",
    )

    namespace = parser.parse_args(argv)
    command_argv = list(namespace.command)
    if command_argv[:1] == ["--"]:
        command_argv.pop(0)
    if not command_argv:
        parser.error("missing command")

    try:
        result = launch(command_argv, cwd=namespace.cwd, wait=namespace.wait)
    except KeyboardInterrupt:
        return 130
    except (AdminLaunchError, OSError) as exc:
        print(f"py-admin-launch: {exc}", file=sys.stderr)
        return 1

    code = result.returncode or 0
    return 128 - code if os.name == "posix" and code < 0 else code


def _normalize_command(command: Command, args: Sequence[PathLike]) -> List[str]:
    if isinstance(command, (str, os.PathLike)):
        argv = [os.fspath(command)]
    else:
        argv = [os.fspath(part) for part in command]

    argv.extend(os.fspath(arg) for arg in args)
    if not argv or not argv[0]:
        raise ValueError("command must include an executable")
    if any("\x00" in part for part in argv):
        raise ValueError("command arguments must not contain NUL bytes")
    return argv


def _launch_direct(
    argv: Sequence[str],
    *,
    cwd: Optional[str],
    wait: bool,
    elevated: bool,
) -> LaunchResult:
    process = subprocess.Popen(argv, cwd=cwd)
    if wait:
        try:
            code = process.wait()
        except KeyboardInterrupt:
            if os.name == "posix" and process.poll() is None:
                try:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait()
                except ProcessLookupError:
                    pass
                except PermissionError:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        raise AdminLaunchError("permission denied interrupting the elevated process")
            raise
        return LaunchResult(elevated=elevated, returncode=code, pid=process.pid)
    return LaunchResult(elevated=elevated, pid=process.pid)


def _launch_linux(
    argv: Sequence[str],
    *,
    cwd: Optional[str],
    wait: bool,
) -> LaunchResult:
    tty = _has_controlling_tty()
    desktop = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    sudo, pkexec = shutil.which("sudo"), shutil.which("pkexec")
    command = _resolve_linux_executable(argv, cwd)
    if sudo and (tty or not desktop or not pkexec):
        prefix = [sudo, "--"] if tty else [sudo, "-n", "--"]
        elevated_argv = prefix + command
    elif pkexec:
        gui_env = _linux_gui_env_assignments() if desktop else []
        prefix = [pkexec, "--keep-cwd"]
        if not (tty and os.isatty(0)):
            prefix.append("--disable-internal-agent")
        elevated_argv = prefix + (["/usr/bin/env", *gui_env] if gui_env else []) + command
    else:
        raise AdminLaunchError("install sudo or polkit (pkexec) to launch as root")
    return _launch_direct(elevated_argv, cwd=cwd, wait=wait, elevated=True)


def _has_controlling_tty() -> bool:
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def _resolve_linux_executable(argv: Sequence[str], cwd: Optional[str]) -> List[str]:
    command_argv = list(argv)
    executable = command_argv[0]
    if os.path.dirname(executable):
        command_argv[0] = os.path.abspath(os.path.join(cwd or os.curdir, executable))
        return command_argv

    path = os.pathsep.join(os.path.join(cwd or os.curdir, entry) for entry in os.get_exec_path())
    resolved = shutil.which(executable, path=path)
    if resolved is not None:
        command_argv[0] = os.path.abspath(resolved)
    return command_argv


def _linux_gui_env_assignments() -> List[str]:
    assignments = []
    for name in _LINUX_GUI_ENV_VARS:
        value = os.environ.get(name)
        if value:
            assignments.append("{}={}".format(name, value))
    if "XAUTHORITY" not in os.environ:
        fallback_xauthority = os.path.expanduser("~/.Xauthority")
        if os.path.exists(fallback_xauthority):
            assignments.append(f"XAUTHORITY={fallback_xauthority}")
    return assignments


def _launch_macos(
    argv: Sequence[str],
    *,
    cwd: Optional[str],
    wait: bool,
) -> LaunchResult:
    osascript = shutil.which("osascript")
    if osascript is None:
        return _launch_sudo(argv, cwd=cwd, wait=wait)

    shell_command = _build_posix_shell_command(argv, cwd=cwd, wait=wait)
    script = (
        'do shell script "'
        + _escape_applescript_string(shell_command)
        + '" with administrator privileges'
    )
    process = subprocess.Popen([osascript, "-e", script])
    if wait:
        return LaunchResult(elevated=True, returncode=process.wait(), pid=process.pid)
    return LaunchResult(elevated=True, pid=process.pid)


def _launch_sudo(
    argv: Sequence[str],
    *,
    cwd: Optional[str],
    wait: bool,
) -> LaunchResult:
    sudo = shutil.which("sudo")
    if sudo is None:
        raise AdminLaunchError("neither osascript nor sudo was found")

    process = subprocess.Popen(["sudo", *argv], cwd=cwd)
    if wait:
        return LaunchResult(elevated=True, returncode=process.wait(), pid=process.pid)
    return LaunchResult(elevated=True, pid=process.pid)


def _build_posix_shell_command(
    argv: Sequence[str],
    *,
    cwd: Optional[str],
    wait: bool,
) -> str:
    command = " ".join(shlex.quote(part) for part in argv)
    if cwd is not None:
        command = f"cd {shlex.quote(cwd)} && exec {command}"
    else:
        command = f"exec {command}"

    if wait:
        return command
    return f"({command}) >/dev/null 2>&1 &"


def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


_LINUX_GUI_ENV_VARS = (
    "DISPLAY",
    "XAUTHORITY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)


__all__ = [
    "AdminLaunchError",
    "LaunchResult",
    "is_admin",
    "launch",
    "main",
]
