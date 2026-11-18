import subprocess
import unittest
from unittest.mock import patch

from szu_guardian.wifi import (
    current_ssid,
    find_wifi_interface,
    interface_has_ipv4,
    recover_wifi,
    reset_wifi_interface,
)


HARDWARE_PORTS = """Hardware Port: Ethernet Adapter (en3)
Device: en3

Hardware Port: Wi-Fi
Device: en0
"""
IFCONFIG_WITH_IP = """en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	inet 172.30.8.9 netmask 0xffff0000 broadcast 172.30.255.255
	status: active
"""


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def fail(stderr="error"):
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


class FakeWifiSystem:
    def __init__(self, *, has_ip=False, powered=True, join_failures=0, wifi_device="en0"):
        self.commands = []
        self.has_ip = has_ip
        self.powered = powered
        self.join_failures = join_failures
        self.join_calls = 0
        self.wifi_device = wifi_device
        self.allow_power_off = True

    def __call__(self, args):
        self.commands.append(list(args))
        if args[0] == "networksetup":
            subcommand = args[1]
            if subcommand == "-listallhardwareports":
                return ok(HARDWARE_PORTS if self.wifi_device else "")
            if subcommand == "-getairportpower":
                return ok(f"Wi-Fi Power ({args[2]}): {'On' if self.powered else 'Off'}\n")
            if subcommand == "-setairportpower":
                if args[3] == "off" and not self.allow_power_off:
                    return fail("denied")
                self.powered = args[3] == "on"
                if not self.powered:
                    self.has_ip = False
                return ok()
            if subcommand == "-setairportnetwork":
                self.join_calls += 1
                if self.join_calls <= self.join_failures:
                    return fail("could not associate")
                self.has_ip = True
                return ok()
        if args[0] == "ifconfig":
            if args[1] == self.wifi_device and self.has_ip:
                return ok(IFCONFIG_WITH_IP)
            return ok("")
        return fail(f"unexpected command: {args}")


class WifiRecoveryTests(unittest.TestCase):
    def test_existing_ip_does_not_claim_internet_or_rejoin(self):
        system = FakeWifiSystem(has_ip=True)

        result = recover_wifi("SZU_WLAN", runner=system, sleeper=lambda _: None)

        self.assertTrue(result.ready)
        self.assertFalse(result.changed)
        self.assertEqual(system.join_calls, 0)

    def test_successful_rejoin_is_only_link_evidence(self):
        system = FakeWifiSystem()
        messages = []

        result = recover_wifi(
            "SZU_WLAN", messages.append, runner=system, sleeper=lambda _: None
        )

        self.assertTrue(result.ready)
        self.assertTrue(result.changed)
        self.assertEqual(system.join_calls, 1)
        self.assertIn("不代表互联网已恢复", messages[-1])

    def test_wifi_power_is_enabled_before_join(self):
        system = FakeWifiSystem(powered=False)
        result = recover_wifi("SZU_WLAN", runner=system, sleeper=lambda _: None)
        self.assertTrue(result.ready)
        self.assertIn(["networksetup", "-setairportpower", "en0", "on"], system.commands)

    def test_join_failure_power_cycles_and_retries(self):
        system = FakeWifiSystem(join_failures=1)
        result = recover_wifi("SZU_WLAN", runner=system, sleeper=lambda _: None)
        self.assertTrue(result.ready)
        self.assertEqual(system.join_calls, 2)

    def test_failed_join_does_not_return_ready(self):
        system = FakeWifiSystem(join_failures=99)
        result = recover_wifi("SZU_WLAN", runner=system, sleeper=lambda _: None)
        self.assertFalse(result.ready)
        self.assertFalse(result.changed)

    def test_empty_ssid_disables_wifi_management(self):
        system = FakeWifiSystem()
        result = recover_wifi("", runner=system, sleeper=lambda _: None)
        self.assertFalse(result.ready)
        self.assertEqual(system.commands, [])

    def test_windows_recovery_uses_netsh(self):
        from szu_guardian.wifi import _recover_win

        runner_calls = []

        def runner(args):
            runner_calls.append(args)
            return ok()

        with patch("szu_guardian.wifi._wait_for_ipv4", return_value=True):
            ready = _recover_win(
                "WLAN", "SZU_WLAN", lambda _: None, runner, lambda _: None, None
            )

        self.assertTrue(ready)
        self.assertEqual(
            runner_calls,
            [["netsh", "wlan", "connect", 'name="SZU_WLAN"', 'interface="WLAN"']],
        )


class WifiResetTests(unittest.TestCase):
    def test_reset_must_observe_old_ip_leave_and_new_ip_arrive(self):
        system = FakeWifiSystem(has_ip=True)
        result = reset_wifi_interface("SZU_WLAN", runner=system, sleeper=lambda _: None)
        self.assertTrue(result.ready)
        self.assertTrue(result.changed)
        self.assertEqual(system.join_calls, 1)

    def test_power_off_failure_cannot_claim_reset_success(self):
        system = FakeWifiSystem(has_ip=True)
        system.allow_power_off = False
        messages = []

        result = reset_wifi_interface(
            "SZU_WLAN", messages.append, runner=system, sleeper=lambda _: None
        )

        self.assertFalse(result.ready)
        self.assertFalse(result.changed)
        self.assertTrue(any("关闭命令失败" in message for message in messages))

    def test_missing_device_returns_structured_failure(self):
        system = FakeWifiSystem(wifi_device=None)
        result = reset_wifi_interface("SZU_WLAN", runner=system, sleeper=lambda _: None)
        self.assertFalse(result.ready)
        self.assertFalse(result.changed)

    def test_cancellation_during_reset_does_not_leave_power_off(self):
        system = FakeWifiSystem(has_ip=True)
        result = reset_wifi_interface(
            "SZU_WLAN",
            runner=system,
            sleeper=lambda _: None,
            cancel_check=lambda: True,
        )
        self.assertFalse(result.ready)
        self.assertTrue(system.powered)


class WifiDiscoveryTests(unittest.TestCase):
    def test_finds_device_and_parses_ssid(self):
        self.assertEqual(find_wifi_interface(FakeWifiSystem()), "en0")
        self.assertEqual(
            current_ssid("en0", lambda _args: ok("Current Wi-Fi Network: SZU_WLAN\n")),
            "SZU_WLAN",
        )

    def test_interface_ip_check_does_not_check_ssid(self):
        self.assertTrue(interface_has_ipv4("en0", FakeWifiSystem(has_ip=True)))
        self.assertFalse(interface_has_ipv4("en0", FakeWifiSystem()))


if __name__ == "__main__":
    unittest.main()
