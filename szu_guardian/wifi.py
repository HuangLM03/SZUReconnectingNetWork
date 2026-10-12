"""Wi-Fi link recovery（Windows + macOS）。

门户认证（SRun / ePortal）只能工作在「Wi-Fi 已关联」的前提下。
本模块在检测确认断网后负责把 Wi-Fi 链路拉回来：

- macOS：``networksetup`` 找接口 / 加入 SSID（可断电重启）；
  以「接口上是否存在活跃 IPv4」判断链路（``-getairportnetwork``
  的 SSID 读数在 macOS 15 上不可靠，只作日志参考）
- Windows：WLAN 接口名与链路状态来自 GetAdaptersAddresses
  （IfType=71 即 WLAN，免 netsh 本地化输出问题）；
  动作用 ``netsh wlan connect``（使用系统已保存的网络配置文件；
  电源切换需要管理员权限，不做）

所有外部命令通过可注入的 runner 以完整 argv 执行，便于测试。
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Callable

from .direct_network import _parse_ifconfig, discover_adapter_configs


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess]


def _default_runner(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=15)


# ---------------------------------------------------------------------------
# 接口发现与状态（平台分派）


def find_wifi_interface(runner: CommandRunner = _default_runner) -> str | None:
    """返回 Wi-Fi 接口标识（macOS 设备名 / Windows FriendlyName）。"""
    if sys.platform == "win32":
        for config in discover_adapter_configs():
            if config.is_wlan:
                return config.interface
        return None
    result = runner(["networksetup", "-listallhardwareports"])
    if result.returncode != 0:
        return None
    device = ""
    current_is_wifi = False
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Hardware Port:"):
            current_is_wifi = stripped.endswith("Wi-Fi")
        elif stripped.startswith("Device:") and current_is_wifi:
            device = stripped.split(":", 1)[1].strip()
            break
    return device or None


def current_ssid(interface: str, runner: CommandRunner = _default_runner) -> str | None:
    """尽力读取当前关联的 SSID；读不到（已知不可靠）返回 None。"""
    if sys.platform == "win32":
        result = runner(["netsh", "wlan", "show", "interfaces"])
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if " SSID" in line and ":" in line and "BSSID" not in line:
                ssid = line.split(":", 1)[1].strip()
                if ssid:
                    return ssid
        return None
    result = runner(["networksetup", "-getairportnetwork", interface])
    if result.returncode != 0:
        return None
    output = result.stdout
    for marker in ("Current Wi-Fi Network:", "当前 Wi-Fi 网络："):
        if marker in output:
            ssid = output.split(marker, 1)[1].strip().strip("\n")
            return ssid or None
    return None


def interface_has_ipv4(
    interface: str,
    runner: CommandRunner = _default_runner,
) -> bool:
    """接口上是否存在活跃 IPv4 —— 判断链路是否可用的可靠信号。"""
    if sys.platform == "win32":
        return any(
            config.interface == interface for config in discover_adapter_configs()
        )
    result = runner(["ifconfig", interface])
    if result.returncode != 0:
        return False
    return any(
        name == interface for name, _ in _parse_ifconfig(result.stdout)
    )


# ---------------------------------------------------------------------------
# 动作命令（平台分派）


def wifi_power_is_on(interface: str, runner: CommandRunner = _default_runner) -> bool:
    if sys.platform == "win32":
        return True  # Windows 电源切换需管理员权限，不做；WLAN 服务常在
    result = runner(["networksetup", "-getairportpower", interface])
    if result.returncode != 0:
        return True  # 读不到就按已开启处理，避免误开关
    return "off" not in result.stdout.split(":")[-1].strip().lower()


def _join_win(
    interface: str,
    ssid: str,
    runner: CommandRunner = _default_runner,
) -> bool:
    result = runner(
        [
            "netsh",
            "wlan",
            "connect",
            f'name="{ssid}"',
            f'interface="{interface}"',
        ]
    )
    return result.returncode == 0


def join_network(
    interface: str,
    ssid: str,
    runner: CommandRunner = _default_runner,
) -> bool:
    """重新加入 SSID。开放网络或系统已记住密码 / 配置文件的网络可直接加入。"""
    if sys.platform == "win32":
        return _join_win(interface, ssid, runner)
    result = runner(["networksetup", "-setairportnetwork", interface, ssid])
    return result.returncode == 0


def set_wifi_power(
    interface: str,
    on: bool,
    runner: CommandRunner = _default_runner,
) -> None:
    if sys.platform == "win32":
        return  # 需要管理员权限；Windows 断链场景由重试 join 覆盖
    runner(["networksetup", "-setairportpower", interface, "on" if on else "off"])


# ---------------------------------------------------------------------------
# 恢复流程


def _wait_for_ipv4(
    interface: str,
    timeout: float = 20.0,
    poll_interval: float = 2.0,
    sleeper: Callable[[float], None] = time.sleep,
    runner: CommandRunner = _default_runner,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if interface_has_ipv4(interface, runner):
            return True
        sleeper(poll_interval)
    return False


def recover_wifi(
    ssid: str,
    progress_callback: Callable[[str], None] | None = None,
    interface: str | None = None,
    runner: CommandRunner = _default_runner,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """把 Wi-Fi 链路拉回目标 SSID 并等到拿到 IPv4。成功返回 True。

    仅在断网后调用：若链路已可用（例如用户手动切到了手机热点），
    这里不会强行切换，直接返回 True。
    """
    if not ssid:
        return False
    wifi_interface = interface or find_wifi_interface(runner)
    if not wifi_interface:
        if progress_callback:
            progress_callback("未找到 Wi-Fi 网卡，无法自动重连 Wi-Fi")
        return False

    def report(message: str) -> None:
        if progress_callback:
            progress_callback(message)

    if interface_has_ipv4(wifi_interface, runner):
        return True  # 链路已恢复（可能是别的网络），交给上层检测

    if sys.platform == "win32":
        return _recover_win(wifi_interface, ssid, report, runner, sleeper)
    return _recover_mac(wifi_interface, ssid, report, runner, sleeper)


def _recover_mac(interface, ssid, report, runner, sleeper) -> bool:
    report(f"Wi-Fi 链路已断开，正在重新加入 {ssid}…")
    if not wifi_power_is_on(interface, runner):
        report("Wi-Fi 处于关闭状态，正在开启…")
        set_wifi_power(interface, True, runner)
        sleeper(3)

    if not join_network(interface, ssid, runner):
        report("直接加入失败，正在重启 Wi-Fi 后重试…")
        set_wifi_power(interface, False, runner)
        sleeper(2)
        set_wifi_power(interface, True, runner)
        sleeper(5)
        if not join_network(interface, ssid, runner):
            report(
                f"无法加入 {ssid}；若该网络需要密码，请先手动连接一次让系统记住"
            )
            return False

    if _wait_for_ipv4(interface, sleeper=sleeper, runner=runner):
        report(f"Wi-Fi 已重新连接 {ssid}，等待网络就绪…")
        return True
    report("Wi-Fi 已关联但迟迟未获得 IP 地址")
    return False


def _recover_win(interface, ssid, report, runner, sleeper) -> bool:
    report(f"WLAN 链路已断开，正在通过 netsh 重新连接 {ssid}…")

    for attempt in (1, 2):
        if _join_win(interface, ssid, runner):
            if _wait_for_ipv4(interface, sleeper=sleeper, runner=runner):
                report(f"WLAN 已重新连接 {ssid}")
                return True
            report("WLAN 已发出连接请求但迟迟未获得 IP 地址")
            return False
        if attempt == 1:
            report("连接失败，稍后重试…")
            sleeper(5)

    report(
        f"无法连接 {ssid}；请先在系统设置中连接一次该网络以保存配置文件"
    )
    return False
