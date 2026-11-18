import socket
import struct
import unittest
from unittest.mock import Mock, patch

from szu_guardian.direct_network import (
    DIRECT_DNS_SERVERS,
    IP_BOUND_IF,
    IP_UNICAST_IF,
    AdapterDnsConfig,
    DirectNetworkError,
    DirectRoute,
    DirectHTTPSConnectionPool,
    _adapter_preference,
    _parse_ifconfig,
    _parse_scutil_dns,
    _pin_value,
    _valid_target_ipv4,
    macos_adapter_dns_configs,
    query_a_record,
)


IFCONFIG_SAMPLE = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
	inet 127.0.0.1 netmask 0xff000000
en5: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	inet 172.30.8.9 netmask 0xffff0000 broadcast 172.30.255.255
	status: active
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
	inet 198.18.0.1 --> 198.18.0.1 netmask 0xffffffff
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	inet 192.168.1.8 netmask 0xffffff00 broadcast 192.168.1.255
	status: active
en8: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	inet 172.30.9.1 netmask 0xffff0000
	status: inactive
"""


class AdapterTests(unittest.TestCase):
    def test_only_active_physical_ipv4_interfaces_are_discovered(self):
        self.assertEqual(
            _parse_ifconfig(IFCONFIG_SAMPLE),
            [("en5", "172.30.8.9"), ("en0", "192.168.1.8")],
        )

    def test_system_dns_is_deduplicated_and_fake_ip_excluded(self):
        output = """resolver #1
 nameserver[0] : 172.30.255.6
 nameserver[1] : 8.8.8.8
 nameserver[2] : 198.18.0.2
"""
        self.assertEqual(_parse_scutil_dns(output), ("172.30.255.6", "8.8.8.8"))

    def test_command_failures_yield_no_adapters(self):
        def failing(_args):
            raise OSError("denied")

        self.assertEqual(macos_adapter_dns_configs(command_runner=failing), [])

    def test_campus_network_sorts_before_hotspot(self):
        campus = AdapterDnsConfig("en5", "172.30.8.9", ())
        hotspot = AdapterDnsConfig("en0", "192.168.1.8", ())
        self.assertLess(_adapter_preference(campus), _adapter_preference(hotspot))

    def test_windows_pin_uses_network_byte_order(self):
        pin = _pin_value("", 5, platform="win32")
        self.assertEqual(pin, (socket.IPPROTO_IP, IP_UNICAST_IF, socket.ntohl(5)))

    @patch("szu_guardian.direct_network.socket.if_nametoindex", return_value=13)
    def test_macos_pin_uses_interface_index(self, _index):
        self.assertEqual(
            _pin_value("en0", 0),
            (socket.IPPROTO_IP, IP_BOUND_IF, 13),
        )


class RouteTests(unittest.TestCase):
    @patch("szu_guardian.direct_network.query_a_record", return_value=["172.31.63.36"])
    @patch("szu_guardian.direct_network.discover_adapter_configs")
    @patch("szu_guardian.direct_network._pin_value", return_value=(0, 25, 1))
    def test_discovery_requires_pinned_adapter_and_campus_dns(
        self, _pin, adapters, _query
    ):
        adapters.return_value = [
            AdapterDnsConfig("en0", "192.168.1.8", ("202.96.134.133",)),
            AdapterDnsConfig("en5", "172.30.230.97", ("202.96.134.133",)),
        ]
        route = DirectRoute.discover()
        self.assertEqual(route.interface, "en5")
        self.assertEqual(route.source_ip, "172.30.230.97")
        self.assertIn("223.5.5.5", route.dns_servers)

    @patch("szu_guardian.direct_network.discover_adapter_configs", return_value=[])
    def test_discovery_never_falls_back_to_unpinned_source_only(self, _adapters):
        with self.assertRaises(DirectNetworkError):
            DirectRoute.discover()

    def test_no_rewrite_url_api_remains(self):
        self.assertFalse(hasattr(DirectRoute("172.30.8.9"), "rewrite_url"))

    def test_public_and_portal_target_policies_are_separate(self):
        self.assertTrue(_valid_target_ipv4("8.8.8.8", allow_private=False))
        self.assertFalse(_valid_target_ipv4("172.30.1.1", allow_private=False))
        self.assertTrue(_valid_target_ipv4("172.30.1.1", allow_private=True))
        self.assertFalse(_valid_target_ipv4("198.18.0.1", allow_private=True))
        self.assertFalse(_valid_target_ipv4("127.0.0.1", allow_private=True))

    @patch("szu_guardian.direct_network.create_connection", return_value=object())
    def test_tcp_target_changes_but_tls_hostname_does_not(self, connect):
        route = Mock()
        route.resolve.return_value = ["8.8.8.8"]
        pool = DirectHTTPSConnectionPool("www.baidu.com", 443)
        pool.direct_route = route
        pool.allow_private = False

        connection = pool._new_conn()
        raw_socket = connection._new_conn()

        self.assertIsNotNone(raw_socket)
        self.assertEqual(connection.host, "www.baidu.com")
        self.assertEqual(connection._dns_host, "www.baidu.com")
        self.assertEqual(connect.call_args.args[0], ("8.8.8.8", 443))
        route.resolve.assert_called_once_with("www.baidu.com")

    @patch("szu_guardian.direct_network.create_connection")
    def test_public_connection_refuses_private_dns_answer(self, connect):
        route = Mock()
        route.resolve.return_value = ["172.30.1.1"]
        pool = DirectHTTPSConnectionPool("www.baidu.com", 443)
        pool.direct_route = route
        pool.allow_private = False
        connection = pool._new_conn()

        from urllib3.exceptions import NewConnectionError

        with self.assertRaises(NewConnectionError):
            connection._new_conn()
        connect.assert_not_called()

    def test_first_https_validation_also_has_direct_route(self):
        route = Mock()
        route.resolve.return_value = ["8.8.8.8"]
        pool = DirectHTTPSConnectionPool("www.baidu.com", 443)
        pool.direct_route = route
        pool.allow_private = False
        created = Mock()
        created.host = "www.baidu.com"
        created.sock = None
        created.is_closed = True
        created.is_verified = True
        created.proxy_is_verified = True

        with patch.object(pool, "ConnectionCls", return_value=created) as connection_cls:
            connection = pool._new_conn()

        self.assertIs(connection, created)
        self.assertIs(connection.direct_route, route)
        self.assertFalse(connection.allow_private)
        self.assertEqual(connection_cls.call_args.kwargs["host"], "www.baidu.com")
        pool._validate_conn(connection)  # 第一次请求即走直连，不能等复用时才注入
        connection.connect.assert_called_once()

    def test_missing_pin_fails_closed(self):
        with self.assertRaises(DirectNetworkError):
            DirectRoute("172.30.8.9").session()


# DNS 报文工具：answer/additional 分开构造，防止误把额外记录当答案。
def name_wire(name):
    return b"".join(bytes([len(label)]) + label.encode() for label in name.split(".")) + b"\0"


def dns_packet(*, answers, additionals=b"", question="example.com", flags=0x8180):
    header = struct.pack("!HHHHHH", 1234, flags, 1, len(answers), 0, 1 if additionals else 0)
    body = name_wire(question) + struct.pack("!HH", 1, 1)
    for answer_name, answer_type, value in answers:
        body += name_wire(answer_name) + struct.pack("!HHIH", answer_type, 1, 60, len(value)) + value
    return header + body + additionals


class DnsTests(unittest.TestCase):
    def query(self, packet):
        udp = Mock()
        udp.recv.return_value = packet
        with (
            patch("szu_guardian.direct_network.socket.socket", return_value=udp),
            patch("szu_guardian.direct_network._pin_socket"),
            patch("szu_guardian.direct_network.secrets.randbits", return_value=1234),
        ):
            result = query_a_record("example.com", "8.8.8.8", "172.30.1.2", interface="en0")
        udp.connect.assert_called_once_with(("8.8.8.8", 53))
        udp.send.assert_called_once()
        return result

    def test_only_answer_a_record_is_returned(self):
        extra_a = name_wire("example.com") + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("172.30.1.1")
        packet = dns_packet(
            answers=[("example.com", 1, socket.inet_aton("8.8.8.8"))],
            additionals=extra_a,
        )
        self.assertEqual(self.query(packet), ["8.8.8.8"])

    def test_cname_chain_is_followed_but_unrelated_a_is_not(self):
        packet = dns_packet(
            answers=[
                ("example.com", 5, name_wire("cdn.example.net")),
                ("cdn.example.net", 1, socket.inet_aton("8.8.8.8")),
                ("unrelated.example", 1, socket.inet_aton("172.30.1.1")),
            ]
        )
        self.assertEqual(self.query(packet), ["8.8.8.8"])

    def test_non_response_packet_is_rejected(self):
        packet = dns_packet(
            answers=[("example.com", 1, socket.inet_aton("8.8.8.8"))],
            flags=0x0100,
        )
        with self.assertRaises(DirectNetworkError):
            self.query(packet)

    def test_mismatched_question_is_rejected(self):
        packet = dns_packet(
            answers=[("example.com", 1, socket.inet_aton("8.8.8.8"))],
            question="other.example",
        )
        with self.assertRaises(DirectNetworkError):
            self.query(packet)


if __name__ == "__main__":
    unittest.main()
