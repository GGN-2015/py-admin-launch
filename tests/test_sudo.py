from contextlib import redirect_stderr, redirect_stdout
import io
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from py_admin_launch import sudo


class WindowsArguments(unittest.TestCase):
    def setUp(self):
        for name in ("launch", "is_admin", "os"):
            patcher = mock.patch.object(sudo, name, wraps=os if name == "os" else None)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.os.name = "nt"
        self.is_admin.return_value = False
        self.launch.return_value.returncode = 37

    def test_foreground_preserves_child_argv_and_exit(self):
        command = ["tool", "--help", "-n", "-D", "--", "", "a b", 'a"b']
        for user in (["-uroot"], ["--user=#0"]):
            self.assertEqual(sudo.main(user + ["-D", "some dir", "--"] + command), 37)
            self.launch.assert_called_with(command, cwd=os.path.abspath("some dir"), wait=True)

    def test_noninteractive_never_prompts(self):
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(sudo.main(["-nb", "tool"]), 1)
        self.assertIn("-n", error.getvalue())
        self.launch.assert_not_called()
        self.is_admin.return_value = True
        self.assertEqual(sudo.main(["-nb", "tool"]), 0)
        self.launch.assert_called_once_with(["tool"], cwd=None, wait=False)

    def test_interactive_shell(self):
        with mock.patch.object(sudo, "_system_shell", return_value=["cmd.exe", "/d"]):
            self.assertEqual(sudo.main(["--shell"]), 37)
        self.launch.assert_called_once_with(["cmd.exe", "/d"], cwd=None, wait=True)

    def test_bad_requests_fail_before_elevation(self):
        for args in ([], ["--"], ["-D"], ["--chdir=", "tool"], ["-s", "echo"],
                     ["-u", "nobody", "tool"], ["-S", "tool"], ["-v"], ["FOO=1", "tool"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()) as error:
                self.assertNotEqual(sudo.main(args), 0)
                self.assertTrue(error.getvalue().startswith("py-sudo: "))
        self.launch.assert_not_called()

    def test_information_options_need_no_command_or_elevation(self):
        for option, expected in (("--help", "usage: py-sudo"), ("-V", sudo.__version__)):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(sudo.main([option]), 0)
            self.assertIn(expected, output.getvalue())
        self.launch.assert_not_called()


class PosixSudo(unittest.TestCase):
    def test_exec_native_sudo_without_parsing_or_root_shortcut(self):
        args = ["-S", "-u", "nobody", "A=two words", "--", "tool", "--help", ""]
        with mock.patch.object(sudo, "os", name="os") as system, mock.patch.object(
                sudo.shutil, "which", return_value="/bin/sudo") as which:
            system.name, system.defpath = "posix", "/bin:/usr/bin"
            sudo.main(args)
            which.assert_called_once_with("sudo", path=system.defpath)
            system.execv.assert_called_once_with("/bin/sudo", ["/bin/sudo"] + args)

    def test_missing_native_sudo_fails_without_fallback(self):
        with mock.patch.object(sudo, "os") as system, mock.patch.object(
                sudo.shutil, "which", return_value=None), redirect_stderr(io.StringIO()) as error:
            system.name = "posix"
            self.assertEqual(sudo.main(["tool"]), 1)
            self.assertIn("sudo not found", error.getvalue())
            system.execv.assert_not_called()

    @unittest.skipUnless(os.name == "posix" and shutil.which("sudo", path=os.defpath),
                         "requires system sudo")
    def test_native_cli_parity(self):
        native = shutil.which("sudo", path=os.defpath)
        for args in (["--help"], ["-n", "-u", "nobody", "--", "id", "-u"],
                     ["-n", "PY_SUDO_TEST=ok", sys.executable, "-c",
                      "import os,sys;print(os.getenv('PY_SUDO_TEST'),sys.argv[1:]);"
                      "sys.stderr.write('stderr');sys.exit(37)", "--help", ""]):
            def run(prefix):
                proc = subprocess.run(prefix + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      stdin=subprocess.DEVNULL, timeout=15)
                return proc.returncode, proc.stdout, proc.stderr

            with self.subTest(args=args):
                self.assertEqual(run([sys.executable, "-m", "py_admin_launch.sudo"]), run([native]))
