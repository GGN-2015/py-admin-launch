import os
import unittest
from unittest import mock

import py_admin_launch


class AdminLaunchTests(unittest.TestCase):
    def test_launch_direct_when_already_admin(self):
        with mock.patch("py_admin_launch.is_admin", return_value=True), mock.patch(
            "py_admin_launch.subprocess.Popen"
        ) as popen:
            popen.return_value.pid = 123

            result = py_admin_launch.launch(["tool", "arg"])

        popen.assert_called_once_with(["tool", "arg"], cwd=None)
        self.assertTrue(result.elevated)
        self.assertEqual(result.pid, 123)

    @mock.patch("py_admin_launch._has_controlling_tty", return_value=True)
    def test_linux_prefers_sudo_in_terminal(self, tty):
        with mock.patch("py_admin_launch.is_admin", return_value=False), mock.patch(
            "py_admin_launch.platform.system", return_value="Linux"
        ), mock.patch.dict("py_admin_launch.os.environ", {}, clear=True), mock.patch(
            "py_admin_launch.shutil.which",
            side_effect=["/usr/bin/sudo", "/usr/bin/pkexec", "/usr/local/bin/tool"],
        ), mock.patch(
            "py_admin_launch.subprocess.Popen"
        ) as popen:
            popen.return_value.pid = 123

            result = py_admin_launch.launch(["tool", "arg"])

        popen.assert_called_once_with(["/usr/bin/sudo", "--", os.path.abspath("/usr/local/bin/tool"), "arg"], cwd=None)
        self.assertTrue(result.elevated)

    @mock.patch("py_admin_launch._has_controlling_tty", return_value=False)
    def test_linux_passes_gui_environment_through_elevation(self, tty):
        environ = {
            "DISPLAY": ":0",
            "XAUTHORITY": "/home/neko/.Xauthority",
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "PATH": "/usr/bin",
        }
        with mock.patch("py_admin_launch.is_admin", return_value=False), mock.patch(
            "py_admin_launch.platform.system", return_value="Linux"
        ), mock.patch.dict("py_admin_launch.os.environ", environ, clear=True), mock.patch(
            "py_admin_launch.shutil.which",
            side_effect=["/usr/bin/sudo", "/usr/bin/pkexec", "/opt/conda/bin/tool"],
        ), mock.patch(
            "py_admin_launch.subprocess.Popen"
        ) as popen:
            popen.return_value.pid = 123

            result = py_admin_launch.launch(["tool", "arg"], cwd="work")

        popen.assert_called_once_with(
            [
                "/usr/bin/pkexec",
                "--keep-cwd",
                "--disable-internal-agent",
                "/usr/bin/env",
                "DISPLAY=:0",
                "XAUTHORITY=/home/neko/.Xauthority",
                "WAYLAND_DISPLAY=wayland-0",
                "XDG_RUNTIME_DIR=/run/user/1000",
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                os.path.abspath("/opt/conda/bin/tool"),
                "arg",
            ],
            cwd="work",
        )
        self.assertTrue(result.elevated)

    @mock.patch("py_admin_launch._has_controlling_tty", return_value=False)
    def test_linux_headless_sudo_never_reads_password_from_stdin(self, tty):
        with mock.patch("py_admin_launch.is_admin", return_value=False), mock.patch(
            "py_admin_launch.platform.system", return_value="Linux"
        ), mock.patch.dict("py_admin_launch.os.environ", {}, clear=True), mock.patch(
            "py_admin_launch.shutil.which",
            side_effect=["/usr/bin/sudo", "/usr/bin/pkexec", "/usr/local/bin/tool"],
        ), mock.patch(
            "py_admin_launch.subprocess.Popen"
        ) as popen:
            popen.return_value.pid = 123

            result = py_admin_launch.launch(["tool", "arg"])

        popen.assert_called_once_with(["/usr/bin/sudo", "-n", "--", os.path.abspath("/usr/local/bin/tool"), "arg"], cwd=None)
        self.assertTrue(result.elevated)

    def test_macos_uses_osascript(self):
        with mock.patch("py_admin_launch.is_admin", return_value=False), mock.patch(
            "py_admin_launch.platform.system", return_value="Darwin"
        ), mock.patch("py_admin_launch.shutil.which", return_value="/usr/bin/osascript"), mock.patch(
            "py_admin_launch.subprocess.Popen"
        ) as popen:
            popen.return_value.pid = 123

            result = py_admin_launch.launch(["tool", "two words"])

        popen.assert_called_once()
        called_argv = popen.call_args[0][0]
        self.assertEqual(called_argv[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn("with administrator privileges", called_argv[2])
        self.assertIn("'two words'", called_argv[2])
        self.assertTrue(result.elevated)

    def test_cli_interrupt_exit_code(self):
        with mock.patch("py_admin_launch.launch", side_effect=KeyboardInterrupt):
            self.assertEqual(py_admin_launch.main(["tool"]), 130)

    def test_cli_waits_by_default_and_returns_child_exit_code(self):
        result = py_admin_launch.LaunchResult(elevated=True, returncode=7, pid=123)
        with mock.patch("py_admin_launch.launch", return_value=result) as launch:
            exit_code = py_admin_launch.main(["tool", "arg"])

        launch.assert_called_once_with(["tool", "arg"], cwd=None, wait=True)
        self.assertEqual(exit_code, 7)

    def test_cli_no_wait_hands_off_and_returns_zero(self):
        result = py_admin_launch.LaunchResult(elevated=True, returncode=None, pid=123)
        with mock.patch("py_admin_launch.launch", return_value=result) as launch:
            exit_code = py_admin_launch.main(["--no-wait", "tool", "arg"])

        launch.assert_called_once_with(["tool", "arg"], cwd=None, wait=False)
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
