"""sudo-compatible command line entry point."""

import getopt
import os
import re
import shutil
import sys

from . import AdminLaunchError, is_admin, launch
from ._version import __version__


_WINDOWS_HELP = """usage: py-sudo [-bn] [-D directory] [-u root] [--] command [arg ...]
       py-sudo [-n] [-D directory] [-u root] -s
       py-sudo -h | -V

Run a command as administrator, waiting for its exit unless -b is given.
  -b, --background       return after the command starts
  -D, --chdir=directory  run the command in this directory
  -n, --non-interactive  fail instead of requesting UAC elevation
  -s, --shell            open an interactive cmd.exe
  -u, --user=user        accept root or #0 as aliases for UAC elevation
  -h, --help             display this help
  -V, --version          display the py-admin-launch version
  --                     end options; arguments after command are not parsed"""


def _system_shell():
    import ctypes

    directory = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(directory, len(directory))
    if not 0 < length < len(directory):
        raise ctypes.WinError()
    return [os.path.join(directory.value, "cmd.exe"), "/d"]


def _windows_main(argv):
    options, command = getopt.getopt(argv, "bD:hnsu:V", [
        "background", "chdir=", "help", "non-interactive", "shell", "user=", "version",
    ])
    cwd = None
    background = non_interactive = shell = False
    for option, value in options:
        if option in ("-h", "--help"):
            print(_WINDOWS_HELP)
            return 0
        if option in ("-V", "--version"):
            print("py-sudo (py-admin-launch) " + __version__)
            return 0
        if option in ("-b", "--background"):
            background = True
        elif option in ("-D", "--chdir"):
            if not value:
                raise ValueError("--chdir requires a non-empty directory")
            cwd = os.path.abspath(value)
        elif option in ("-n", "--non-interactive"):
            non_interactive = True
        elif option in ("-s", "--shell"):
            shell = True
        elif option in ("-u", "--user") and value not in ("root", "#0"):
            raise ValueError("Windows UAC cannot switch users; use -u root or -u '#0'")

    if shell:
        if command or background:
            raise ValueError("-s opens an interactive shell; for shell commands use cmd.exe /d /c")
        command = _system_shell()
    if not command or not command[0]:
        raise ValueError("missing command; use py-sudo --help for usage")
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", command[0]):
        raise ValueError("leading NAME=value assignments are not supported on Windows")
    if non_interactive and not is_admin():
        raise AdminLaunchError("elevation is required; -n forbids a UAC prompt")
    result = launch(command, cwd=cwd, wait=not background)
    return 0 if background else result.returncode


def main(argv=None):
    """Run native sudo on POSIX or its UAC-backed subset on Windows."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if os.name == "nt":
            return _windows_main(argv)
        sudo = shutil.which("sudo", path=os.defpath)
        if sudo is None:
            raise AdminLaunchError("system sudo not found; install sudo to use py-sudo")
        os.execv(sudo, [sudo] + argv)
    except KeyboardInterrupt:
        return 130
    except (getopt.GetoptError, AdminLaunchError, OSError, ValueError) as exc:
        print("py-sudo: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
