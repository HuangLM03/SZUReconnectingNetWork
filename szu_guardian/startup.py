"""开机自启（跨平台）。

- macOS：``~/Library/LaunchAgents/`` 下的 LaunchAgent plist + launchctl
- Windows：HKCU ``Software\\Microsoft\\Windows\\CurrentVersion\\Run`` 注册表键
  （移植自上游原版；脚本模式优先 pythonw.exe，避免弹出控制台窗口）
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path


LAUNCH_AGENT_LABEL = "com.szu.network-guardian"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_VALUE_NAME = "SZUNetworkGuardian"


# ---------------------------------------------------------------------------
# macOS LaunchAgent


def default_agents_directory() -> Path:
    override = os.environ.get("SZU_GUARDIAN_LAUNCH_AGENTS_DIR")
    if override:
        return Path(override)
    return Path.home() / "Library" / "LaunchAgents"


def launch_agent_path(agents_directory: Path | None = None) -> Path:
    directory = agents_directory or default_agents_directory()
    return directory / f"{LAUNCH_AGENT_LABEL}.plist"


def _program_arguments() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--autostart"]
    if sys.platform == "win32":
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        executable = pythonw if pythonw.exists() else python
        entry = Path(__file__).resolve().parents[1] / "main.py"
        return [str(executable), str(entry), "--autostart"]
    entry = Path(__file__).resolve().parents[1] / "main.py"
    return [sys.executable, str(entry), "--autostart"]


def _windows_startup_command() -> str:
    return subprocess.list2cmdline(_program_arguments())


def build_launch_agent(arguments: list[str] | None = None) -> dict:
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": arguments or _program_arguments(),
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }


def _launchctl(args: list[str]) -> None:
    try:
        subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _bootstrap_agent(plist: Path) -> None:
    uid = os.getuid()
    gui_domain = f"gui/{uid}"
    _launchctl(["bootout", f"{gui_domain}/{LAUNCH_AGENT_LABEL}"])
    _launchctl(["bootstrap", gui_domain, str(plist)])
    # 旧版 macOS 无 bootstrap 时退回 load
    if not _is_agent_loaded(gui_domain):
        _launchctl(["load", str(plist)])


def _is_agent_loaded(gui_domain: str) -> bool:
    try:
        result = subprocess.run(
            ["launchctl", "print", f"{gui_domain}/{LAUNCH_AGENT_LABEL}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _macos_is_autostart_enabled(agents_directory: Path | None) -> bool:
    return launch_agent_path(agents_directory).exists()


def _macos_set_autostart(
    enabled: bool,
    agents_directory: Path | None,
    bootstrapper,
) -> None:
    plist = launch_agent_path(agents_directory)

    if not enabled:
        uid = os.getuid()
        _launchctl(["bootout", f"gui/{uid}/{LAUNCH_AGENT_LABEL}"])
        _launchctl(["unload", str(plist)])
        try:
            plist.unlink(missing_ok=True)
        except OSError:
            pass
        return

    plist.parent.mkdir(parents=True, exist_ok=True)
    agent = build_launch_agent()
    temporary = plist.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        plistlib.dump(agent, stream)
    temporary.replace(plist)
    bootstrapper(plist)


# ---------------------------------------------------------------------------
# Windows 注册表 Run 键


def _windows_is_autostart_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            WINDOWS_RUN_KEY,
            0,
            winreg.KEY_READ,
        ) as key:
            value, _ = winreg.QueryValueEx(key, WINDOWS_VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False


def _windows_set_autostart(enabled: bool) -> None:
    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        WINDOWS_RUN_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(
                key,
                WINDOWS_VALUE_NAME,
                0,
                winreg.REG_SZ,
                _windows_startup_command(),
            )
        else:
            try:
                winreg.DeleteValue(key, WINDOWS_VALUE_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# 平台分派


def is_autostart_enabled(agents_directory: Path | None = None) -> bool:
    if sys.platform == "win32":
        return _windows_is_autostart_enabled()
    return _macos_is_autostart_enabled(agents_directory)


def set_autostart(
    enabled: bool,
    agents_directory: Path | None = None,
    bootstrapper=_bootstrap_agent,
) -> None:
    if sys.platform == "win32":
        _windows_set_autostart(enabled)
        return
    _macos_set_autostart(enabled, agents_directory, bootstrapper)
