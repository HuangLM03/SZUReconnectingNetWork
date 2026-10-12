import subprocess
import unittest
import unittest.mock

from szu_guardian.wifi import (
    current_ssid,
    find_wifi_interface,
    interface_has_ipv4,
    recover_wifi,
)


HARDWARE_PORTS = """Hardware Port: Ethernet Adapter (en3)
Device: en3
Ethernet Address: 3e:07:c5:fa:90:f3

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: c0:95:6d:34:f8:63
"""

IFCONFIG_WITH_IP = """en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	ether c0:95:6d:34:f8:63
	inet 172.30.8.9 netmask 0xffff0000 broadcast 172.30.255.255
	media: autoselect
	status: active
"""


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def fail(stderr="error"):
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


class FakeWifiSystem:
    """按状态机响应 networksetup / ifconfig 命令的假执行器。

    ``join_failures``: 加入 SSID 前多少次会失败（0 = 一直成功，99 = 一直失败）
    """

    def __init__(
        self,
        *,
        has_ip=False,
        powered=True,
        join_failures=0,
        wifi_device="en0",
    ):
        self.commands: list[list[str]] = []
        self.has_ip = has_ip
        self.powered = powered
        self.join_failures = join_failures
        self.join_calls = 0
        self.wifi_device = wifi_device

    def __call__(self, args):
        self.commands.append(list(args))
        program = args[0]
        if program == "networksetup":
            subcommand = args[1]
            if subcommand == "-listallhardwareports":
                return ok(HARDWARE_PORTS if self.wifi_device else "")
            if subcommand == "-getairportpower":
                state = "On" if self.powered else "Off"
                return ok(f"Wi-Fi Power ({args[2]}): {state}\n")
            if subcommand == "-setairportpower":
                self.powered = args[3] == "on"
                return ok("")
            if subcommand == "-setairportnetwork":
                self.join_calls += 1
                if self.join_calls <= self.join_failures:
                    return fail("could not associate to network")
                self.has_ip = True
                return ok("")
            return fail(f"unexpected networksetup call: {args}")
        if program == "ifconfig":
            interface = args[1]
            if interface == self.wifi_device and self.has_ip:
                return ok(IFCONFIG_WITH_IP)
            return ok("")
        return fail(f"unexpected command: {args}")


class DiscoveryTests(unittest.TestCase):
    def test_finds_wifi_device_from_hardware_ports(self):
        self.assertEqual(find_wifi_interface(FakeWifiSystem()), "en0")

    def test_returns_none_without_wifi_device(self):
        self.assertIsNone(find_wifi_interface(FakeWifiSystem(wifi_device=None)))

    def test_current_ssid_parsed_from_english_output(self):
        system = FakeWifiSystem()
        result = current_ssid(
            "en0", lambda args: ok("Current Wi-Fi Network: SZU_WLAN\n")
        )
        self.assertEqual(result, "SZU_WLAN")

    def test_current_ssid_none_when_not_associated(self):
        result = current_ssid(
            "en0", lambda args: ok("You are not associated with an AirPort network.\n")
        )
        self.assertIsNone(result)

    def test_interface_has_ipv4_reflects_link_state(self):
        self.assertTrue(interface_has_ipv4("en0", FakeWifiSystem(has_ip=True)))
        self.assertFalse(interface_has_ipv4("en0", FakeWifiSystem(has_ip=False)))


class RecoverWifiTests(unittest.TestCase):
    def collect(self):
        return []

    def test_no_action_when_link_already_usable(self):
        system = FakeWifiSystem(has_ip=True)
        messages: list[str] = []

        self.assertTrue(
            recover_wifi("SZU_WLAN", messages.append, runner=system, sleeper=lambda _: None)
        )
        self.assertEqual(system.join_calls, 0)
        self.assertEqual(messages, [])

    def test_joins_directly_when_powered(self):
        system = FakeWifiSystem()
        messages: list[str] = []

        self.assertTrue(
            recover_wifi("SZU_WLAN", messages.append, runner=system, sleeper=lambda _: None)
        )
        self.assertEqual(system.join_calls, 1)
        self.assertTrue(system.has_ip)
        joined = [
            c for c in system.commands
            if c[:1] == ["networksetup"] and c[1] == "-setairportnetwork"
        ]
        self.assertEqual(joined, [["networksetup", "-setairportnetwork", "en0", "SZU_WLAN"]])
        self.assertIn("Wi-Fi 链路已断开，正在重新加入 SZU_WLAN…", messages)

    def test_powers_on_before_joining_when_wifi_off(self):
        system = FakeWifiSystem(powered=False)

        self.assertTrue(
            recover_wifi("SZU_WLAN", None, runner=system, sleeper=lambda _: None)
        )
        power_commands = [
            c for c in system.commands if c[:2] == ["networksetup", "-setairportpower"]
        ]
        self.assertEqual(power_commands, [["networksetup", "-setairportpower", "en0", "on"]])
        self.assertEqual(system.join_calls, 1)

    def test_power_cycles_and_retries_when_join_fails_once(self):
        system = FakeWifiSystem(join_failures=1)
        messages: list[str] = []

        self.assertTrue(
            recover_wifi("SZU_WLAN", messages.append, runner=system, sleeper=lambda _: None)
        )
        self.assertEqual(system.join_calls, 2)
        power_sequence = [
            c[3] for c in system.commands if c[:2] == ["networksetup", "-setairportpower"]
        ]
        self.assertEqual(power_sequence, ["off", "on"])
        self.assertIn("直接加入失败，正在重启 Wi-Fi 后重试…", messages)

    def test_reports_failure_when_join_always_fails(self):
        system = FakeWifiSystem(join_failures=99)
        messages: list[str] = []

        self.assertFalse(
            recover_wifi("SZU_WLAN", messages.append, runner=system, sleeper=lambda _: None)
        )
        self.assertIn("请先手动连接一次让系统记住", messages[-1])

    def test_empty_ssid_disables_recovery(self):
        system = FakeWifiSystem()
        self.assertFalse(
            recover_wifi("", None, runner=system, sleeper=lambda _: None)
        )
        self.assertEqual(system.commands, [])


class WindowsRecoveryTests(unittest.TestCase):
    """直接测 _recover_win 的 netsh 命令序列（不依赖 sys.platform）。"""

    def recover(self, join_failures: int, ip_after_join: bool):
        from szu_guardian.wifi import _recover_win

        commands: list[list[str]] = []
        joins: list[int] = [0]
        messages: list[str] = []

        def runner(args):
            commands.append(list(args))
            if args[:3] == ["netsh", "wlan", "connect"]:
                joins[0] += 1
                success = joins[0] > join_failures
                return ok("") if success else fail("The network is not found")
            raise AssertionError(f"unexpected command: {args}")

        import szu_guardian.wifi as wifi_module

        with unittest.mock.patch.object(
            wifi_module, "_wait_for_ipv4", return_value=ip_after_join
        ):
            result = _recover_win(
                "WLAN", "SZU_WLAN", messages.append, runner, lambda _: None
            )
        return result, commands, messages

    def test_happy_path_issues_netsh_connect(self):
        result, commands, messages = self.recover(0, True)

        self.assertTrue(result)
        self.assertEqual(
            commands,
            [["netsh", "wlan", "connect", 'name="SZU_WLAN"', 'interface="WLAN"']],
        )
        self.assertIn("WLAN 已重新连接 SZU_WLAN", messages)

    def test_retries_once_then_fails_with_hint(self):
        result, commands, messages = self.recover(2, True)

        self.assertFalse(result)
        connect_calls = [c for c in commands if c[:3] == ["netsh", "wlan", "connect"]]
        self.assertEqual(len(connect_calls), 2)
        self.assertIn("请先在系统设置中连接一次该网络以保存配置文件", messages[-1])

    def test_connected_but_no_ip_reports_timeout(self):
        result, _commands, messages = self.recover(0, False)

        self.assertFalse(result)
        self.assertIn("迟迟未获得 IP 地址", messages[-1])


if __name__ == "__main__":
    unittest.main()
