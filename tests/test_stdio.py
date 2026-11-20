import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_launch_fixture.py")
WINDOWS = os.name == "nt"


@unittest.skipUnless(WINDOWS or sys.platform.startswith("linux"), "Windows/Linux launch paths")
class StdioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not WINDOWS and os.geteuid() != 0:
            sudo = shutil.which("sudo")
            if not sudo or subprocess.call([sudo, "-n", "--", sys.executable, "-c", "pass"],
                                           stderr=subprocess.DEVNULL) != 0:
                raise unittest.SkipTest("requires root or noninteractive sudo for this Python")

    def start(self, args, env=None):
        environment = os.environ.copy()
        environment.pop("DISPLAY", None)
        environment.pop("WAYLAND_DISPLAY", None)
        environment.update(env or {})
        options = {"start_new_session": True}
        if WINDOWS:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            options = {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": startup}
        process = subprocess.Popen([sys.executable, FIXTURE] + args, env=environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, **options)
        self.addCleanup(self.cleanup, process)
        return process

    @staticmethod
    def cleanup(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()

    def test_binary_streams_cwd_arguments_and_status(self):
        payload = bytes(range(256)) * 4096
        arguments = ["two words", 'a"b', "", "trailing space \\", "\u732b"]
        check_elevation = "assert os.geteuid()==0;"
        if WINDOWS:
            check_elevation = ("import ctypes;assert ctypes.windll.shell32.IsUserAnAdmin();"
                               if os.getenv("PAL_TEST_UAC") else "")
        with tempfile.TemporaryDirectory() as cwd:
            code = ("import os,sys; assert os.path.samefile(os.getcwd(),sys.argv[1]);"
                    "assert sys.argv[2:]=={!r};".format(arguments) +
                    check_elevation +
                    "data=sys.stdin.buffer.read();sys.stderr.buffer.write(data[::-1]);"
                    "sys.stdout.buffer.write(data);sys.exit(37)")
            process = self.start(["--cwd", cwd, "--", sys.executable, "-c", code, cwd] + arguments)
            stdout, stderr = process.communicate(payload, timeout=30)
            self.assertEqual(process.returncode, 37, stderr[:1024])
            self.assertEqual(stdout, payload)
            self.assertEqual(stderr, payload[::-1])

    def test_interrupt_reaches_child_before_launcher_returns(self):
        self.check_interrupt(False)

    @unittest.skipUnless(WINDOWS, "native shared Windows console")
    def test_interrupt_shared_console_with_redirected_outputs(self):
        self.check_interrupt(True)

    @unittest.skipUnless(WINDOWS, "native Windows console input")
    def test_console_keyboard_input_with_redirected_output(self):
        text = "keyboard \u732b"
        code = "import sys;sys.stdout.buffer.write(input().encode('utf-8'))"
        process = self.start(["--", sys.executable, "-c", code],
                             {"PAL_CONSOLE_STDIN": "1", "PAL_CONSOLE_LINE": text})
        self.assertEqual(process.communicate(timeout=15), (text.encode("utf-8"), b""))
        self.assertEqual(process.returncode, 0)

    def check_interrupt(self, console):
        with tempfile.TemporaryDirectory() as directory:
            ready = os.path.join(directory, "ready")
            code = ("import os,sys,signal,time\n"
                    "def on_int(*args):\n print('interrupted',file=sys.stderr,flush=True);sys.exit(0)\n"
                    "signal.signal(signal.SIGINT,on_int)\n"
                    "assert sys.stdin.isatty()=={}\n".format(console) +
                    "open(sys.argv[1],'w').close()\nwhile True: time.sleep(.05)")
            env = {"PAL_INTERRUPT_WHEN": ready}
            if console:
                env["PAL_CONSOLE_STDIN"] = "1"
            process = self.start(["--", sys.executable, "-c", code, ready], env)
            if not WINDOWS:
                deadline = time.monotonic() + 10
                while not os.path.exists(ready) and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(.02)
                self.assertTrue(os.path.exists(ready))
                os.kill(process.pid, signal.SIGINT)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(stderr.splitlines(), [b"interrupted"])
            self.assertEqual(stdout, b"")
            self.assertEqual(process.returncode, 130)

    @unittest.skipUnless(WINDOWS, "UAC startup acknowledgement")
    def test_no_wait_handoff_retains_io(self):
        code = "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"
        process = self.start(["--no-wait", "--", sys.executable, "-c", code])
        self.assertEqual(process.wait(timeout=15), 0)
        self.assertEqual(process.communicate(b"after handoff", timeout=10), (b"after handoff", b""))

    def test_launch_failure_reported_on_stderr(self):
        process = self.start(["--", "nonexistent-command-pal-fixture"])
        stdout, stderr = process.communicate(timeout=15)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(stdout, b"")
        self.assertTrue(stderr)

    @unittest.skipUnless(WINDOWS, "Windows job cleanup")
    def test_interrupt_stops_descendants_that_ignore_it(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = os.path.join(directory, "ready")
            stubborn = ("import signal,sys,time;signal.signal(signal.SIGINT,signal.SIG_IGN);"
                        "open(sys.argv[1],'w').close();time.sleep(25)")
            code = ("import signal,subprocess,sys,time;signal.signal(signal.SIGINT,signal.SIG_IGN);"
                    "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);time.sleep(25)")
            process = self.start(["--", sys.executable, "-c", code, stubborn, ready],
                                 {"PAL_INTERRUPT_WHEN": ready})
            self.assertEqual(process.communicate(timeout=15), (b"", b""))
            self.assertEqual(process.returncode, 130)

    @unittest.skipIf(WINDOWS, "POSIX pseudo-terminal")
    def test_terminal_input_and_keyboard_interrupt(self):
        import fcntl
        import pty
        import select
        import termios

        def controlling_tty():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        with tempfile.TemporaryDirectory() as directory:
            ready = os.path.join(directory, "ready")
            code = ("import sys,time,signal\n"
                    "def stop(*args):\n print('keyboard',file=sys.stderr,flush=True);sys.exit(130)\n"
                    "signal.signal(signal.SIGINT,stop)\n"
                    "print('prompt> ',end='',flush=True)\nprint('read:'+input(),flush=True)\n"
                    "open(sys.argv[1],'w').close()\nwhile True:time.sleep(.05)")
            master, slave = pty.openpty()
            self.addCleanup(os.close, master)
            try:
                proc = subprocess.Popen([sys.executable, FIXTURE, "--", sys.executable, "-c", code, ready],
                                        stdin=slave, stdout=slave, stderr=subprocess.PIPE,
                                        env=dict(os.environ, DISPLAY="", WAYLAND_DISPLAY=""),
                                        preexec_fn=controlling_tty)
            finally:
                os.close(slave)
            try:
                self.assertTrue(select.select([master], [], [], 10)[0], "no prompt")
                self.assertIn(b"prompt> ", os.read(master, 1024))
                os.write(master, b"hello terminal\n")
                deadline = time.monotonic() + 5
                while not os.path.exists(ready) and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(os.path.exists(ready))
                self.assertIn(b"read:hello terminal", os.read(master, 1024))
                os.write(master, b"\x03")
                self.assertEqual(proc.communicate(timeout=10)[1].splitlines(), [b"keyboard"])
                self.assertEqual(proc.returncode, 130)
            finally:
                proc.kill()
                proc.wait()
                proc.stderr.close()
