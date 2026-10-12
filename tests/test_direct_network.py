import socket
import unittest
from unittest.mock import patch

from szu_guardian.direct_network import (
    DIRECT_DNS_SERVERS,
    IP_BOUND_IF,
    IP_UNICAST_IF,
    AdapterDnsConfig,
    DirectRoute,
    _parse_ifconfig,
    _parse_scutil_dns,
    _pin_value,
    local_ipv4_candidates,
    macos_adapter_dns_configs,
)


IFCONFIG_SAMPLE = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
	options=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
	inet 127.0.0.1 netmask 0xff000000
	inet6 ::1 prefixlen 128
	inet6 fe80::1%lo0 prefixlen 64 scopeid 0x1
	nd6 options=201<PERFORMNUD,DAD>
gif0: flags=8010<POINTOPOINT,MULTICAST> mtu 1280
	stf0: flags=0<> mtu 1280
en5: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	ether 00:e0:4c:68:00:01
	nd6 options=201<PERFORMNUD,DAD>
	inet 172.30.8.9 netmask 0xffff0000 broadcast 172.30.255.255
	media: 1000baseT <full-duplex>
	status: active
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
	inet 198.18.0.1 --> 198.18.0.1 netmask 0xffffffff
	inet6 fe80::5:1:1:1%utun4 prefixlen 64 scopeid 0x12
awdl0: flags=8943<UP,BROADCAST,RUNNING,PROMISC,SIMPLEX,MULTICAST> mtu 1484
	inet6 fe80::7c:1c:2b:9a:1e:2d%awdl0 prefixlen 64 scopeid 0x10
	nd6 options=201<PERFORMNUD,DAD>
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	ether 3c:22:fb:11:22:33
	inet6 fe80::1%en0 prefixlen 64 scopeid 0x4
	inet 192.168.1.8 netmask 0xffffff00 broadcast 192.168.1.255
	nd6 options=201<PERFORMNUD,DAD>
	status: active
en8: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	ether aa:bb:cc:dd:ee:ff
	media: autoselect
	status: inactive
"""

SCUTIL_DNS_SAMPLE = """resolver domain: local
options mdns

resolver #1
	search domain[0] : szu.edu.cn
	nameserver[0] : 172.30.255.6
	nameserver[1] : 8.8.8.8
	reach: 0x00020002 (Reachable,Direct Connection)

resolver #2
	domain : com.cn
	nameserver[0] : 198.18.0.2
"""

FULL_SCUTIL_SAMPLE = """DNS configuration

resolver #1
	nameserver[0] : 172.30.255.6
	nameserver[1] : 8.8.8.8
"""


class IfconfigParsingTests(unittest.TestCase):
    def test_active_physical_interfaces_are_discovered(self):
        result = _parse_ifconfig(IFCONFIG_SAMPLE)

        self.assertEqual(result, [("en5", "172.30.8.9"), ("en0", "192.168.1.8")])

    def test_fake_ip_vpn_tunnels_and_loopback_are_excluded(self):
        interfaces = {name for name, _ in _parse_ifconfig(IFCONFIG_SAMPLE)}

        self.assertNotIn("utun4", interfaces)
        self.assertNotIn("lo0", interfaces)
        self.assertNotIn("awdl0", interfaces)
        self.assertNotIn("en8", interfaces)  # status: inactive

    def test_blank_lines_do_not_crash(self):
        self.assertEqual(_parse_ifconfig("\n\n" + IFCONFIG_SAMPLE + "\n"), _parse_ifconfig(IFCONFIG_SAMPLE))


class ScutilParsingTests(unittest.TestCase):
    def test_nameservers_collected_and_deduplicated(self):
        self.assertEqual(
            _parse_scutil_dns(SCUTIL_DNS_SAMPLE),
            ("172.30.255.6", "8.8.8.8"),
        )


class AdapterDiscoveryTests(unittest.TestCase):
    def test_campus_interface_preferred_over_home_network(self):
        def fake_runner(args):
            if args[0] == "ifconfig":
                return IFCONFIG_SAMPLE
            return FULL_SCUTIL_SAMPLE

        configs = macos_adapter_dns_configs(command_runner=fake_runner)

        self.assertEqual(configs[0].interface, "en5")
        self.assertEqual(configs[0].source_ip, "172.30.8.9")
        self.assertEqual(configs[0].dns_servers, ("172.30.255.6", "8.8.8.8"))
        self.assertEqual(configs[1].source_ip, "192.168.1.8")

    def test_command_failures_yield_empty_list(self):
        def failing_runner(args):
            raise OSError("denied")

        self.assertEqual(macos_adapter_dns_configs(command_runner=failing_runner), [])


class DirectRouteTests(unittest.TestCase):
    def test_local_candidates_exclude_fake_ip_and_link_local(self):
        with (
            patch("szu_guardian.direct_network.macos_adapter_dns_configs", return_value=[]),
            patch.object(
                socket,
                "gethostbyname_ex",
                return_value=("macbook", [], ["198.18.0.1", "169.254.1.2", "172.30.8.9", "192.168.1.8"]),
            ),
        ):
            result = local_ipv4_candidates()

        self.assertEqual(result, ["172.30.8.9", "192.168.1.8"])

    def test_rewrite_url_uses_direct_dns_result_and_preserves_host(self):
        route = DirectRoute("172.30.8.9")
        route._cache["net.szu.edu.cn"] = ["172.31.63.36"]

        url, host = route.rewrite_url(
            "https://net.szu.edu.cn/cgi-bin/get_challenge?callback=_"
        )

        self.assertEqual(host, "net.szu.edu.cn")
        self.assertEqual(
            url,
            "https://172.31.63.36/cgi-bin/get_challenge?callback=_",
        )

    @patch("szu_guardian.direct_network.query_a_record")
    @patch("szu_guardian.direct_network.macos_adapter_dns_configs")
    def test_discover_prefers_campus_adapter(
        self,
        adapter_configs,
        query_a_record,
    ):
        adapter_configs.return_value = [
            AdapterDnsConfig("en0", "192.168.1.8", ("202.96.134.133",)),
            AdapterDnsConfig("en5", "172.30.230.97", ("202.96.134.133", "202.96.128.166")),
        ]
        query_a_record.return_value = ["172.31.63.36"]

        route = DirectRoute.discover()

        self.assertEqual(route.source_ip, "172.30.230.97")
        self.assertEqual(route.interface, "en5")
        self.assertEqual(
            route.dns_servers,
            ("202.96.134.133", "202.96.128.166", *DIRECT_DNS_SERVERS),
        )
        query_a_record.assert_called_once_with(
            "net.szu.edu.cn",
            "202.96.134.133",
            "172.30.230.97",
            interface="en5",
            if_index=0,
        )

    @patch("szu_guardian.direct_network.query_a_record")
    @patch("szu_guardian.direct_network.macos_adapter_dns_configs")
    def test_bound_session_pins_sockets_to_interface(
        self,
        adapter_configs,
        query_a_record,
    ):
        adapter_configs.return_value = [
            AdapterDnsConfig("en5", "172.30.230.97", ("202.96.134.133",)),
        ]
        query_a_record.return_value = ["172.31.63.36"]
        with patch(
            "szu_guardian.direct_network.socket.if_nametoindex",
            return_value=13,
        ):
            route = DirectRoute.discover()
            pin = _pin_value(route.interface, route.if_index)
            session = route.session()

        self.assertEqual(pin, (socket.IPPROTO_IP, IP_BOUND_IF, 13))
        adapter = session.get_adapter("https://net.szu.edu.cn/")
        self.assertEqual(adapter.source_ip, "172.30.230.97")
        self.assertEqual(adapter.interface, "en5")

    def test_windows_pin_uses_unicast_if_with_network_byte_order(self):
        """IP_UNICAST_IF 的值必须是 ntohl(if_index) —— Windows 历史性坑。"""
        pin = _pin_value("", 5, platform="win32")
        self.assertEqual(
            pin,
            (socket.IPPROTO_IP, IP_UNICAST_IF, socket.ntohl(5)),
        )
        self.assertNotEqual(pin[2], 5)  # 小端机器上 ntohl 后不再等于原值

        self.assertIsNone(_pin_value("", 0, platform="win32"))

    def test_bound_session_without_interface_uses_source_only(self):
        session = DirectRoute("172.30.8.9").session()
        adapter = session.get_adapter("https://example.com/")
        self.assertEqual(adapter.interface, "")

    @patch("szu_guardian.direct_network.macos_adapter_dns_configs")
    def test_local_candidates_use_adapters_when_available(self, adapter_configs):
        adapter_configs.return_value = [
            AdapterDnsConfig("en5", "172.30.230.97", ("202.96.134.133",)),
        ]

        self.assertEqual(local_ipv4_candidates(), ["172.30.230.97"])


if __name__ == "__main__":
    unittest.main()
