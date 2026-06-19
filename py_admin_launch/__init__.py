"""Cross-platform helpers for launching a command as administrator/root."""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional, Sequence, Union


PathLike = Union[str, os.PathLike]
Command = Union[PathLike, Sequence[PathLike]]


class AdminLaunchError(RuntimeError):
    """Raised when a command cannot be launched with administrator privileges."""


@dataclass(frozen=True)
class LaunchResult:
    """Result returned by :func:`launch`.

    ``elevated`` means the command was launched with administrator/root rights.
    When the current process is already elevated, this is true even though no
    prompt is shown. ``returncode`` is available only when ``wait=True`` or when
    a direct launch exits before returning. Elevated Windows launches use
    ShellExecuteW and do not expose a child process handle.
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

    * Linux: ``pkexec`` (polkit GUI authentication), falling back to ``sudo``.
      Desktop session variables are passed explicitly so GUI programs can find
      the display after privilege elevation.
    * Windows: ``ShellExecuteW(..., "runas", ...)``.
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
        return _launch_windows(argv, cwd=normalized_cwd)
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
    except (AdminLaunchError, OSError) as exc:
        print(f"py-admin-launch: {exc}", file=sys.stderr)
        return 1

    return result.returncode if result.returncode is not None else 0


def _normalize_command(command: Command, args: Sequence[PathLike]) -> list[str]:
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
        return LaunchResult(elevated=elevated, returncode=process.wait(), pid=process.pid)
    return LaunchResult(elevated=elevated, pid=process.pid)


def _launch_linux(
    argv: Sequence[str],
    *,
    cwd: Optional[str],
    wait: bool,
) -> LaunchResult:
    elevator = shutil.which("pkexec")
    if elevator is None:
        elevator = shutil.which("sudo")
    if elevator is None:
        raise AdminLaunchError("neither pkexec nor sudo was found")

    elevated_argv = [elevator, *_linux_elevated_command(argv)]
    process = subprocess.Popen(elevated_argv, cwd=cwd)
    if wait:
        return LaunchResult(elevated=True, returncode=process.wait(), pid=process.pid)
    return LaunchResult(elevated=True, pid=process.pid)


def _linux_elevated_command(argv: Sequence[str]) -> list[str]:
    command_argv = _resolve_linux_executable(argv)
    gui_env = _linux_gui_env_assignments()
    if not gui_env:
        return command_argv

    env_executable = shutil.which("env") or "/usr/bin/env"
    return [env_executable, *gui_env, *command_argv]


def _resolve_linux_executable(argv: Sequence[str]) -> list[str]:
    command_argv = list(argv)
    executable = command_argv[0]
    if os.path.dirname(executable):
        return command_argv

    resolved = shutil.which(executable)
    if resolved is not None:
        command_argv[0] = resolved
    return command_argv


def _linux_gui_env_assignments() -> list[str]:
    assignments = [
        f"{name}={value}"
        for name in _LINUX_GUI_ENV_VARS
        if (value := os.environ.get(name))
    ]
    if "XAUTHORITY" not in os.environ:
        fallback_xauthority = os.path.expanduser("~/.Xauthority")
        if os.path.exists(fallback_xauthority):
            assignments.append(f"XAUTHORITY={fallback_xauthority}")
    return assignments


def _launch_windows(argv: Sequence[str], *, cwd: Optional[str]) -> LaunchResult:
    if platform.system() != "Windows":
        raise AdminLaunchError("ShellExecuteW is available only on Windows")

    executable = argv[0]
    parameters = subprocess.list2cmdline(list(argv[1:]))
    operation = "runas"
    show_normal = 1

    shell_execute = ctypes.windll.shell32.ShellExecuteW
    result = shell_execute(None, operation, executable, parameters, cwd, show_normal)
    result_code = int(result)
    if result_code <= 32:
        message = _WINDOWS_SHELLEXECUTE_ERRORS.get(
            result_code,
            f"ShellExecuteW failed with code {result_code}",
        )
        raise AdminLaunchError(message)

    return LaunchResult(elevated=True)


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


_WINDOWS_SHELLEXECUTE_ERRORS = {
    0: "the operating system is out of memory or resources",
    2: "file was not found",
    3: "path was not found",
    5: "access was denied",
    8: "the operating system is out of memory or resources",
    26: "a sharing violation occurred",
    27: "the file association is incomplete or invalid",
    28: "the DDE transaction timed out",
    29: "the DDE transaction failed",
    30: "the DDE transaction is busy",
    31: "there is no application associated with the specified file",
    32: "the specified DLL was not found",
}


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
