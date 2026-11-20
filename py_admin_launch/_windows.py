"""One-shot UAC launcher, also executed by absolute path with python -I -S.

Only the command line specifies what to execute. The broker duplicates unnamed
handles from the requesting process; there is no listener, command socket or
temporary file. Console devices are reopened after joining the caller's
console; file and pipe handles keep their original identity and byte semantics.
"""

import _winapi
import ctypes
from ctypes import wintypes
from contextlib import ExitStack
import json
import msvcrt
import os
import subprocess
import sys


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE), ("lpIDList", wintypes.LPVOID),
        ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def _checked(result, func, args):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def _api(library, name, restype, *argtypes):
    func = getattr(library, name)
    func.argtypes, func.restype, func.errcheck = argtypes, restype, _checked
    return func


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_handle = wintypes.HANDLE
_SYNCHRONIZE = 0x00100000
_SEE_MASK_NOCLOSEPROCESS = 0x40
_SEE_MASK_NOASYNC = 0x100
_SEE_MASK_FLAG_NO_UI = 0x400
_bool = wintypes.BOOL
_ctrl_handler = ctypes.WINFUNCTYPE(_bool, wintypes.DWORD)
_create_event = _api(_kernel, "CreateEventW", _handle, wintypes.LPVOID, _bool, _bool, wintypes.LPCWSTR)
_set_event = _api(_kernel, "SetEvent", _bool, _handle)
_attach_console = _api(_kernel, "AttachConsole", _bool, wintypes.DWORD)
_set_handler = _api(_kernel, "SetConsoleCtrlHandler", _bool, _ctrl_handler, _bool)
_send_ctrl = _api(_kernel, "GenerateConsoleCtrlEvent", _bool, wintypes.DWORD, wintypes.DWORD)
_create_job = _api(_kernel, "CreateJobObjectW", _handle, wintypes.LPVOID, wintypes.LPCWSTR)
_join_job = _api(_kernel, "AssignProcessToJobObject", _bool, _handle, _handle)
_stop_job = _api(_kernel, "TerminateJobObject", _bool, _handle, wintypes.UINT)
_shell_execute = _api(ctypes.WinDLL("shell32", use_last_error=True), "ShellExecuteExW",
                      _bool, ctypes.POINTER(_ShellExecuteInfo))


def _runas(executable, arguments):
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC | _SEE_MASK_FLAG_NO_UI
    info.lpVerb, info.lpFile = "runas", executable
    info.lpParameters = subprocess.list2cmdline(arguments)
    info.lpDirectory = os.path.dirname(executable)
    info.nShow = 0
    _shell_execute(ctypes.byref(info))
    return info.hProcess


def _own_handle(stack, handle):
    stack.callback(_winapi.CloseHandle, handle)
    return handle


def _stream_spec(fd, stack):
    try:
        original = msvcrt.get_osfhandle(fd)
    except OSError:
        return "null"
    mode = wintypes.DWORD()
    if _kernel.GetConsoleMode(_handle(original), ctypes.byref(mode)):
        return "console"
    if original in (-1, -2):
        return "null"
    return str(_own_handle(stack, _duplicate(_winapi.GetCurrentProcess(), original)))


def _duplicate(source, handle):
    return _winapi.DuplicateHandle(source, handle, _winapi.GetCurrentProcess(),
                                   0, False, _winapi.DUPLICATE_SAME_ACCESS)


def _wait_helper(helper):
    while _winapi.WaitForSingleObject(helper, 50) == _winapi.WAIT_TIMEOUT:
        pass


def _started(helper, reader):
    from . import AdminLaunchError

    handle = msvcrt.get_osfhandle(reader)
    while not _winapi.PeekNamedPipe(handle, 0)[0]:
        if _winapi.WaitForSingleObject(helper, 50) == _winapi.WAIT_OBJECT_0:
            if not _winapi.PeekNamedPipe(handle, 0)[0]:
                raise AdminLaunchError("UAC helper exited before starting the command (code {})".format(
                    _winapi.GetExitCodeProcess(helper)))
    status = json.loads(os.read(reader, 4096).decode("ascii"))
    if isinstance(status, str):
        raise AdminLaunchError(status)
    return status


def _pipe(stack):
    reader, writer = os.pipe()
    stack.callback(os.close, reader)
    stack.callback(os.close, writer)
    return reader, writer


def launch_windows(argv, *, cwd, wait):
    from . import LaunchResult

    executable = getattr(sys, "_base_executable", None) or sys.executable
    if os.path.basename(executable).lower() == "pythonw.exe":
        executable = os.path.join(os.path.dirname(executable), "python.exe")
    with ExitStack() as lifetime, ExitStack() as startup:
        reader, writer = _pipe(startup)
        gate = _own_handle(startup, _create_event(None, True, False, None))
        cancel = _own_handle(lifetime, _create_event(None, True, False, None))
        arguments = ["-I", "-S", os.path.abspath(__file__), str(os.getpid()),
                     str(msvcrt.get_osfhandle(writer)), str(gate),
                     str(cancel), str(int(wait))]
        arguments.extend(_stream_spec(fd, startup) for fd in range(3))
        arguments.extend([os.path.abspath(cwd or os.curdir)] + list(argv))
        helper = None
        try:
            helper = _own_handle(lifetime, _runas(executable, arguments))
            _set_event(gate)
            pid = _started(helper, reader)
            startup.close()
            if not wait:
                return LaunchResult(elevated=True, pid=pid)
            _wait_helper(helper)
            return LaunchResult(elevated=True, pid=pid, returncode=_winapi.GetExitCodeProcess(helper))
        except KeyboardInterrupt:
            _set_event(cancel)
            while helper is not None:
                try:
                    _wait_helper(helper)
                    break
                except KeyboardInterrupt:
                    continue
            raise


def _stream_fd(parent, spec, index, stack):
    flags = os.O_BINARY | (os.O_RDONLY if index == 0 else os.O_WRONLY)
    if spec == "console":
        fd = os.open("CONIN$" if index == 0 else "CONOUT$", os.O_BINARY | os.O_RDWR)
    elif spec == "null":
        fd = os.open(os.devnull, flags)
    else:
        fd = msvcrt.open_osfhandle(_duplicate(parent, int(spec)), flags)
    stack.callback(os.close, fd)
    return fd


def _serve(arguments):
    parent_pid, writer, gate, cancel, wait = map(int, arguments[:5])
    streams, cwd, argv = arguments[5:8], arguments[8], arguments[9:]
    with ExitStack() as stack:
        parent = _own_handle(stack, _winapi.OpenProcess(
            _winapi.PROCESS_DUP_HANDLE | _SYNCHRONIZE, False, parent_pid))
        reporter = stack.enter_context(os.fdopen(msvcrt.open_osfhandle(
            _duplicate(parent, writer), os.O_BINARY | os.O_WRONLY), "wb", buffering=0))
        try:
            cancel = _own_handle(stack, _duplicate(parent, cancel))
            gate = _own_handle(stack, _duplicate(parent, gate))
            if _winapi.WaitForMultipleObjects([cancel, parent, gate], False, _winapi.INFINITE) != 2:
                return 1
            shared = "console" in streams
            if shared:
                _kernel.FreeConsole()
                _attach_console(parent_pid)
            else:
                members = (wintypes.DWORD * 1)()
                if _kernel.GetConsoleProcessList(members, 1) != 1:
                    raise RuntimeError("UAC helper did not receive a private console")

            received = _own_handle(stack, _create_event(None, True, False, None))

            @_ctrl_handler
            def on_ctrl(event):
                if event == 0:
                    _set_event(received)
                    return True
                return False

            _set_handler(_ctrl_handler(), False)
            _set_handler(on_ctrl, True)
            stack.callback(_set_handler, on_ctrl, False)
            job = _own_handle(stack, _create_job(None, None))
            _join_job(job, _winapi.GetCurrentProcess())
            if _winapi.WaitForSingleObject(cancel, 0) == _winapi.WAIT_OBJECT_0:
                raise RuntimeError("launch cancelled")
            with ExitStack() as handles:
                stdin, stdout, stderr = [_stream_fd(parent, spec, index, handles)
                                         for index, spec in enumerate(streams)]
                os.chdir(cwd)
                child = subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=stderr)
            try:
                reporter.write(str(child.pid).encode("ascii"))
            except OSError:
                _stop_job(job, 130)
                raise
        except Exception as exc:
            reporter.write(json.dumps(str(exc)[:300]).encode("ascii"))
            return 1
        reporter.close()
        if not wait:
            return 0
        reason = _winapi.WaitForMultipleObjects(
            [received, cancel, parent, int(child._handle)], False, _winapi.INFINITE)
        interrupt, _, disconnected, finished = range(4)
        if reason == finished:
            return child.wait()
        if reason != disconnected or not shared:
            if reason != interrupt and (not shared or _winapi.WaitForSingleObject(received, 200) != 0):
                _send_ctrl(0, 0)
            _winapi.WaitForSingleObject(int(child._handle), 3000)
        _stop_job(job, 130)
        return 130


if __name__ == "__main__":
    _winapi.ExitProcess(_serve(sys.argv[1:]))
