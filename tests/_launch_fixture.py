"""Exercise the source checkout in a fresh, independently interruptible console."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import py_admin_launch


def run():
    if os.name == "nt":
        import ctypes
        import threading
        import time
        from py_admin_launch import _windows

        if not os.getenv("PAL_TEST_UAC"):
            native = _windows._shell_execute

            def same_token(info):
                fields = ctypes.cast(info, ctypes.POINTER(_windows._ShellExecuteInfo)).contents
                assert fields.lpVerb == "runas" and fields.nShow == 0
                fields.lpVerb = "open"
                return native(info)

            _windows._shell_execute = same_token
            py_admin_launch.is_admin = lambda: False

        ready = os.getenv("PAL_INTERRUPT_WHEN")
        if ready:
            def interrupt():
                while not os.path.exists(ready):
                    time.sleep(0.02)
                assert ctypes.windll.kernel32.GenerateConsoleCtrlEvent(0, 0)

            threading.Thread(target=interrupt, daemon=True).start()

        if os.getenv("PAL_CONSOLE_STDIN"):
            with open("CONIN$", "rb", buffering=0) as console:
                os.dup2(console.fileno(), 0)
            text = os.getenv("PAL_CONSOLE_LINE")
            if text:
                import msvcrt
                import struct
                records = b"".join(struct.pack("<H2xiHHHHI", 1, 1, 1, 13 if char == "\r" else 0,
                                               0, ord(char), 0) for char in text + "\r")
                with open("CONIN$", "r+b", buffering=0) as keyboard:
                    count = ctypes.c_uint32()
                    assert ctypes.windll.kernel32.WriteConsoleInputW(
                        ctypes.c_void_p(msvcrt.get_osfhandle(keyboard.fileno())), records,
                        len(text) + 1, ctypes.byref(count))

    return py_admin_launch.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(run())
