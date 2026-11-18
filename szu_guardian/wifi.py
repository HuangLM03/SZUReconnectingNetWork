"""Wi-Fi 链路恢复（Windows + macOS）。

本模块只回答“接口是否有 IPv4、链路是否实际重建”，不提供互联网在线
证据。任何 ready=True 都必须再由 NetworkClient 的公网探针重新验证。
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

from .direct_network import _parse_ifconfig, discover_adapter_configs


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess]
CancelCheck = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class WifiResult:
    ready: bool
    changed: bool
    interface: str = ""
    message: str = ""


def _default_runner(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=15)


def find_wifi_interface(runner: CommandRunner = _default_runner) -> str | None:
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
    """尽力读取 SSID；读不到时不能把已有 IP 说成已加入目标网络。"""
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
    if sys.platform == "win32":
        return any(
            config.interface == interface for config in discover_adapter_configs()
        )
    result = runner(["ifconfig", interface])
    if result.returncode != 0:
        return False
    return any(name == interface for name, _ in _parse_ifconfig(result.stdout))


def wifi_power_is_on(interface: str, runner: CommandRunner = _default_runner) -> bool:
    if sys.platform == "win32":
        return True
    result = runner(["networksetup", "-getairportpower", interface])
    if result.returncode != 0:
        return True
    return "off" not in result.stdout.split(":")[-1].strip().lower()


def _join_win(interface: str, ssid: str, runner: CommandRunner = _default_runner) -> bool:
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


def join_network(interface: str, ssid: str, runner: CommandRunner = _default_runner) -> bool:
    if sys.platform == "win32":
        return _join_win(interface, ssid, runner)
    result = runner(["networksetup", "-setairportnetwork", interface, ssid])
    return result.returncode == 0


def set_wifi_power(
    interface: str,
    on: bool,
    runner: CommandRunner = _default_runner,
) -> bool:
    if sys.platform == "win32":
        return True
    result = runner(["networksetup", "-setairportpower", interface, "on" if on else "off"])
    return result.returncode == 0


def _wait_for_ipv4(
    interface: str,
    timeout: float = 20.0,
    poll_interval: float = 2.0,
    sleeper: Callable[[float], None] = time.sleep,
    runner: CommandRunner = _default_runner,
    cancel_check: CancelCheck | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel_check and cancel_check():
            return False
        if interface_has_ipv4(interface, runner):
            return True
        sleeper(poll_interval)
    return False


def _wait_for_ipv4_absence(
    interface: str,
    timeout: float = 8.0,
    poll_interval: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
    runner: CommandRunner = _default_runner,
    cancel_check: CancelCheck | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel_check and cancel_check():
            return False
        if not interface_has_ipv4(interface, runner):
            return True
        sleeper(poll_interval)
    return False


def recover_wifi(
    ssid: str,
    progress_callback: Callable[[str], None] | None = None,
    interface: str | None = None,
    runner: CommandRunner = _default_runner,
    sleeper: Callable[[float], None] = time.sleep,
    cancel_check: CancelCheck | None = None,
) -> WifiResult:
    """仅在接口没有 IPv4 时重连；已有 IP 时不抢用户当前网络。"""
    if not ssid:
        return WifiResult(False, False, message="未配置受管 Wi-Fi SSID")
    wifi_interface = interface or find_wifi_interface(runner)
    if not wifi_interface:
        message = "未找到 Wi-Fi 网卡，无法自动重连 Wi-Fi"
        if progress_callback:
            progress_callback(message)
        return WifiResult(False, False, message=message)

    def report(message: str) -> None:
        if progress_callback:
            progress_callback(message)

    if interface_has_ipv4(wifi_interface, runner):
        return WifiResult(
            True,
            False,
            wifi_interface,
            "Wi-Fi 接口已有 IPv4，无需重建链路",
        )

    if sys.platform == "win32":
        ready = _recover_win(
            wifi_interface,
            ssid,
            report,
            runner,
            sleeper,
            cancel_check,
        )
    else:
        ready = _recover_mac(
            wifi_interface,
            ssid,
            report,
            runner,
            sleeper,
            cancel_check,
        )
    return WifiResult(
        ready,
        ready,
        wifi_interface,
        "Wi-Fi 链路已重新获得 IPv4" if ready else "Wi-Fi 链路未能恢复",
    )


def _recover_mac(interface, ssid, report, runner, sleeper, cancel_check) -> bool:
    report(f"Wi-Fi 接口没有 IPv4，正在重新加入 {ssid}…")
    if not wifi_power_is_on(interface, runner):
        report("Wi-Fi 处于关闭状态，正在开启…")
        if not set_wifi_power(interface, True, runner):
            report("无法开启 Wi-Fi 电源")
            return False
        sleeper(3)

    if not join_network(interface, ssid, runner):
        report("直接加入失败，正在重启 Wi-Fi 后重试…")
        if not set_wifi_power(interface, False, runner):
            report("无法关闭 Wi-Fi 电源，停止接口重置")
            return False
        sleeper(2)
        power_restored = set_wifi_power(interface, True, runner)
        if not power_restored:
            report("Wi-Fi 电源未能重新开启，请手动检查")
            return False
        sleeper(5)
        if not join_network(interface, ssid, runner):
            report(f"无法加入 {ssid}；请先手动连接一次让系统记住该网络")
            return False

    if _wait_for_ipv4(
        interface,
        sleeper=sleeper,
        runner=runner,
        cancel_check=cancel_check,
    ):
        report("Wi-Fi 接口已获得 IPv4；这不代表互联网已恢复，正在重新验证…")
        return True
    report("Wi-Fi 已发起连接但迟迟未获得 IP 地址")
    return False


def _recover_win(interface, ssid, report, runner, sleeper, cancel_check) -> bool:
    report(f"WLAN 接口没有 IPv4，正在通过 netsh 重新连接 {ssid}…")
    for attempt in (1, 2):
        if _join_win(interface, ssid, runner):
            if _wait_for_ipv4(
                interface,
                sleeper=sleeper,
                runner=runner,
                cancel_check=cancel_check,
            ):
                report("WLAN 接口已获得 IPv4；这不代表互联网已恢复，正在重新验证…")
                return True
            report("WLAN 已发出连接请求但迟迟未获得 IP 地址")
            return False
        if attempt == 1:
            report("连接失败，稍后重试…")
            sleeper(5)
    report(f"无法连接 {ssid}；请先手动连接一次以保存系统配置文件")
    return False


def reset_wifi_interface(
    ssid: str = "",
    progress_callback: Callable[[str], None] | None = None,
    interface: str | None = None,
    runner: CommandRunner = _default_runner,
    sleeper: Callable[[float], None] = time.sleep,
    cancel_check: CancelCheck | None = None,
) -> WifiResult:
    """强制重建链路，必须观察到旧 IP 离开并重新获得后才算 changed。"""
    def report(message: str) -> None:
        if progress_callback:
            progress_callback(message)

    wifi_interface = interface or find_wifi_interface(runner)
    if not wifi_interface:
        message = "未找到 Wi-Fi 网卡，无法自动重置网络接口"
        report(message)
        return WifiResult(False, False, message=message)

    report(f"正在重置 Wi-Fi 接口 {wifi_interface}；重置本身不代表互联网恢复…")
    if sys.platform == "win32":
        disconnected = runner(
            ["netsh", "wlan", "disconnect", f'interface="{wifi_interface}"']
        ).returncode == 0
        if not disconnected:
            report("netsh 未能断开 WLAN，取消本次接口重置")
            return WifiResult(False, False, wifi_interface, "断开命令失败")
        disappeared = _wait_for_ipv4_absence(
            wifi_interface,
            sleeper=sleeper,
            runner=runner,
            cancel_check=cancel_check,
        )
        if not disappeared:
            report("旧 IPv4 未离开，不能声称接口已重置")
            return WifiResult(False, False, wifi_interface, "旧链路未离开")
        if ssid and not _join_win(wifi_interface, ssid, runner):
            report("WLAN 重新加入命令失败")
            return WifiResult(False, False, wifi_interface, "重新加入失败")
    else:
        if not set_wifi_power(wifi_interface, False, runner):
            report("Wi-Fi 关闭命令失败，未对接口进行重置")
            return WifiResult(False, False, wifi_interface, "关闭命令失败")
        disappeared = _wait_for_ipv4_absence(
            wifi_interface,
            sleeper=sleeper,
            runner=runner,
            cancel_check=cancel_check,
        )
        # 无论等待结果如何都尝试恢复电源，避免把用户 Wi-Fi 留在关闭状态。
        power_restored = set_wifi_power(wifi_interface, True, runner)
        if not power_restored:
            report("Wi-Fi 电源未能重新开启，请手动检查")
            return WifiResult(False, False, wifi_interface, "电源未恢复")
        if not disappeared:
            report("旧 IPv4 未离开，不能声称接口已重置")
            return WifiResult(False, False, wifi_interface, "旧链路未离开")
        sleeper(3)
        if ssid and not interface_has_ipv4(wifi_interface, runner):
            if not join_network(wifi_interface, ssid, runner):
                report("Wi-Fi 重新加入命令失败")
                return WifiResult(False, False, wifi_interface, "重新加入失败")

    ready = _wait_for_ipv4(
        wifi_interface,
        timeout=30.0,
        sleeper=sleeper,
        runner=runner,
        cancel_check=cancel_check,
    )
    if ready:
        report("Wi-Fi 接口已重新获得 IPv4，立即重新验证公共互联网…")
        return WifiResult(True, True, wifi_interface, "链路已重建")
    report("Wi-Fi 接口已重置但未在 30 秒内重新获得 IP 地址")
    return WifiResult(False, False, wifi_interface, "重置后未获得 IPv4")
