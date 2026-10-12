import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from szu_guardian.startup import (
    LAUNCH_AGENT_LABEL,
    WINDOWS_VALUE_NAME,
    _windows_startup_command,
    build_launch_agent,
    is_autostart_enabled,
    launch_agent_path,
    set_autostart,
)


class LaunchAgentTests(unittest.TestCase):
    def test_built_agent_points_at_entrypoint_and_runs_at_login(self):
        agent = build_launch_agent(["/usr/bin/true", "--autostart"])

        self.assertEqual(agent["Label"], LAUNCH_AGENT_LABEL)
        self.assertEqual(agent["ProgramArguments"], ["/usr/bin/true", "--autostart"])
        self.assertTrue(agent["RunAtLoad"])
        self.assertEqual(agent["ProcessType"], "Interactive")

    def test_enable_writes_valid_plist(self):
        with tempfile.TemporaryDirectory() as directory:
            agents_dir = Path(directory)
            bootstrapped: list[Path] = []

            set_autostart(
                True,
                agents_directory=agents_dir,
                bootstrapper=bootstrapped.append,
            )

            plist = launch_agent_path(agents_dir)
            self.assertTrue(plist.exists())
            self.assertTrue(is_autostart_enabled(agents_dir))
            self.assertEqual(bootstrapped, [plist])
            with plist.open("rb") as stream:
                payload = plistlib.load(stream)
            self.assertEqual(payload["Label"], LAUNCH_AGENT_LABEL)
            self.assertIn("main.py", payload["ProgramArguments"][-2])
            self.assertEqual(payload["ProgramArguments"][-1], "--autostart")

    def test_disable_removes_plist(self):
        with tempfile.TemporaryDirectory() as directory:
            agents_dir = Path(directory)
            set_autostart(
                True,
                agents_directory=agents_dir,
                bootstrapper=lambda _plist: None,
            )
            self.assertTrue(is_autostart_enabled(agents_dir))

            set_autostart(
                False,
                agents_directory=agents_dir,
                bootstrapper=lambda _plist: None,
            )

            self.assertFalse(is_autostart_enabled(agents_dir))
            self.assertFalse(launch_agent_path(agents_dir).exists())


class WindowsStartupTests(unittest.TestCase):
    def _fake_windows_python(self, with_pythonw: bool):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (directory / "python.exe").write_bytes(b"")
        if with_pythonw:
            (directory / "pythonw.exe").write_bytes(b"")
        return str(directory / "python.exe")

    def test_startup_command_prefers_pythonw_to_avoid_console(self):
        executable = self._fake_windows_python(with_pythonw=True)
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "executable", executable),
        ):
            command = _windows_startup_command()

        self.assertIn("pythonw.exe", command)
        self.assertIn("main.py", command)
        self.assertIn("--autostart", command)

    def test_startup_command_falls_back_to_python(self):
        executable = self._fake_windows_python(with_pythonw=False)
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "executable", executable),
        ):
            command = _windows_startup_command()

        self.assertIn("python.exe", command)
        self.assertNotIn("pythonw.exe", command)

    @unittest.skipUnless(sys.platform == "win32", "winreg 仅 Windows")
    def test_registry_roundtrip(self):
        from szu_guardian.startup import set_autostart as set_auto

        set_auto(True)
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
            ) as key:
                value, _ = winreg.QueryValueEx(key, WINDOWS_VALUE_NAME)
            self.assertTrue(value)
        finally:
            set_auto(False)


if __name__ == "__main__":
    unittest.main()
