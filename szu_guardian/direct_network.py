"""绑定物理网卡的直连传输，Windows 与 macOS 双平台。

直连 DNS 只决定 TCP 连接目标，不再改写 requests 中的 URL。HTTPS 的
SNI、证书链和主机名验证始终使用原始域名；公共 Internet 与校园门户
使用独立连接池和不同的目标地址策略。
"""

from __future__ import annotations

import ctypes
import ipaddress
import secrets
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3 import PoolManager
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError
from urllib3.util.connection import create_connection


FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
PREFERRED_CAMPUS_NETWORK = ipaddress.ip_network("172.30.0.0/16")
DIRECT_DNS_SERVERS = (
    "192.168.247.6",
    "223.5.5.5",
    "119.29.29.29",
    "114.114.114.114",
)

# macOS netinet/in.h：源地址绑定不足以绕过 TUN，必须同时钉定接口。
IP_BOUND_IF = 25
# Windows ws2ipdef.h 的等价选项；值要求网络字节序的接口索引。
IP_UNICAST_IF = 31

WINDOWS_NO_ERROR = 0
WINDOWS_ERROR_BUFFER_OVERFLOW = 111
WINDOWS_IF_OPER_STATUS_UP = 1
WINDOWS_IF_TYPE_IEEE80211 = 71
WINDOWS_GAA_FLAGS = 0x0002 | 0x0004

SKIPPED_INTERFACE_PREFIXES = (
    "lo",
    "utun",
    "awdl",
    "llw",
    "bridge",
    "anpi",
    "ap",
    "gif",
    "stf",
    "vlan",
    "tap",
    "tun",
)


class DirectNetworkError(ConnectionError):
    pass


@dataclass(frozen=True, slots=True)
class AdapterDnsConfig:
    interface: str
    source_ip: str
    dns_servers: tuple[str, ...]
    if_index: int = 0
    is_wlan: bool = False


def _valid_source_ipv4(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (
        parsed.version == 4
        and parsed.is_private
        and not parsed.is_loopback
        and not parsed.is_link_local
        and parsed not in FAKE_IP_NETWORK
    )


def _valid_dns_ipv4(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (
        parsed.version == 4
        and not parsed.is_unspecified
        and not parsed.is_loopback
        and not parsed.is_link_local
        and not parsed.is_multicast
        and parsed not in FAKE_IP_NETWORK
    )


def _valid_target_ipv4(address: str, *, allow_private: bool) -> bool:
    """连接前再次检查目标；DNS 缓存命中也不能绕过此策略。"""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if (
        parsed.version != 4
        or parsed.is_unspecified
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed in FAKE_IP_NETWORK
    ):
        return False
    if allow_private:
        return parsed.is_global or parsed.is_private
    return parsed.is_global


def _merge_dns_servers(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(server for group in groups for server in group))


def _run_command(args: list[str]) -> str:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise OSError(result.returncode, f"{' '.join(args)} 执行失败")
    return result.stdout


# ---------------------------------------------------------------------------
# 网卡枚举：macOS


def _parse_ifconfig(output: str) -> list[tuple[str, str]]:
    """先读完接口块再判断活跃状态，兼容 inet 位于 status 前面的输出。"""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in output.splitlines():
        if line and not line[0].isspace():
            if current:
                blocks.append(current)
            current = [line]
        elif current and line.strip():
            current.append(line)
    if current:
        blocks.append(current)

    candidates: list[tuple[str, str]] = []
    for block in blocks:
        interface = block[0].split(":", 1)[0]
        if interface.startswith(SKIPPED_INTERFACE_PREFIXES):
            continue
        active = "RUNNING" in block[0]
        addresses: list[str] = []
        for line in block[1:]:
            stripped = line.strip()
            if stripped in ("status: active", "status: inactive"):
                active = stripped == "status: active"
            elif stripped.startswith("inet "):
                address = stripped.split()[1]
                if _valid_source_ipv4(address):
                    addresses.append(address)
        if active:
            candidates.extend((interface, address) for address in addresses)

    return list(dict.fromkeys(candidates))


def _parse_scutil_dns(output: str) -> tuple[str, ...]:
    servers: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if "nameserver[" in stripped and "] :" in stripped:
            address = stripped.split("]")[-1].split(":", 1)[-1].strip()
            if _valid_dns_ipv4(address):
                servers.append(address)
    return tuple(dict.fromkeys(servers))


def macos_adapter_dns_configs(
    command_runner=_run_command,
) -> list[AdapterDnsConfig]:
    try:
        interface_output = command_runner(["ifconfig", "-a"])
    except (OSError, subprocess.SubprocessError):
        interface_output = ""

    try:
        dns_servers = _parse_scutil_dns(command_runner(["scutil", "--dns"]))
    except (OSError, subprocess.SubprocessError):
        dns_servers = ()

    configs = [
        AdapterDnsConfig(interface, address, dns_servers)
        for interface, address in _parse_ifconfig(interface_output)
    ]

    def preference(config: AdapterDnsConfig) -> tuple[bool, bool]:
        return (
            ipaddress.ip_address(config.source_ip) not in PREFERRED_CAMPUS_NETWORK,
            not config.interface.startswith("en"),
        )

    configs.sort(key=preference)
    return configs


# ---------------------------------------------------------------------------
# 网卡枚举：Windows


def windows_adapter_dns_configs() -> list[AdapterDnsConfig]:
    if sys.platform != "win32":
        return []

    from ctypes import wintypes

    class SocketAddress(ctypes.Structure):
        _fields_ = [
            ("lpSockaddr", ctypes.c_void_p),
            ("iSockaddrLength", ctypes.c_int),
        ]

    class AdapterUnicastAddress(ctypes.Structure):
        pass

    class AdapterDnsServerAddress(ctypes.Structure):
        pass

    AdapterUnicastAddress._fields_ = [
        ("Alignment", ctypes.c_ulonglong),
        ("Next", ctypes.POINTER(AdapterUnicastAddress)),
        ("Address", SocketAddress),
    ]
    AdapterDnsServerAddress._fields_ = [
        ("Alignment", ctypes.c_ulonglong),
        ("Next", ctypes.POINTER(AdapterDnsServerAddress)),
        ("Address", SocketAddress),
    ]

    class AdapterAddresses(ctypes.Structure):
        pass

    AdapterAddresses._fields_ = [
        ("Length", wintypes.ULONG),
        ("IfIndex", wintypes.DWORD),
        ("Next", ctypes.POINTER(AdapterAddresses)),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.POINTER(AdapterUnicastAddress)),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.POINTER(AdapterDnsServerAddress)),
        ("DnsSuffix", wintypes.LPWSTR),
        ("Description", wintypes.LPWSTR),
        ("FriendlyName", wintypes.LPWSTR),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", wintypes.DWORD),
        ("Flags", wintypes.DWORD),
        ("Mtu", wintypes.DWORD),
        ("IfType", wintypes.DWORD),
        ("OperStatus", ctypes.c_int),
    ]

    def ipv4_from_socket_address(value: SocketAddress) -> str | None:
        if not value.lpSockaddr or value.iSockaddrLength < 8:
            return None
        raw = ctypes.string_at(value.lpSockaddr, value.iSockaddrLength)
        family = struct.unpack_from("H", raw)[0]
        if family != socket.AF_INET:
            return None
        return socket.inet_ntoa(raw[4:8])

    get_adapters_addresses = ctypes.windll.iphlpapi.GetAdaptersAddresses
    get_adapters_addresses.argtypes = [
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        ctypes.POINTER(AdapterAddresses),
        ctypes.POINTER(wintypes.ULONG),
    ]
    get_adapters_addresses.restype = wintypes.ULONG

    size = wintypes.ULONG(15_000)
    while True:
        buffer = ctypes.create_string_buffer(size.value)
        addresses = ctypes.cast(buffer, ctypes.POINTER(AdapterAddresses))
        result = get_adapters_addresses(
            socket.AF_INET,
            WINDOWS_GAA_FLAGS,
            None,
            addresses,
            ctypes.byref(size),
        )
        if result == WINDOWS_ERROR_BUFFER_OVERFLOW:
            continue
        if result != WINDOWS_NO_ERROR:
            raise OSError(result, "GetAdaptersAddresses failed")
        break

    configs: list[AdapterDnsConfig] = []
    adapter = addresses
    while adapter:
        current = adapter.contents
        if current.OperStatus == WINDOWS_IF_OPER_STATUS_UP:
            dns_servers: list[str] = []
            dns_address = current.FirstDnsServerAddress
            while dns_address:
                address = ipv4_from_socket_address(dns_address.contents.Address)
                if address and _valid_dns_ipv4(address):
                    dns_servers.append(address)
                dns_address = dns_address.contents.Next

            friendly_name = current.FriendlyName or ""
            is_wlan = current.IfType == WINDOWS_IF_TYPE_IEEE80211
            unicast_address = current.FirstUnicastAddress
            while unicast_address:
                address = ipv4_from_socket_address(unicast_address.contents.Address)
                if address and _valid_source_ipv4(address):
                    configs.append(
                        AdapterDnsConfig(
                            interface=friendly_name,
                            source_ip=address,
                            dns_servers=tuple(dict.fromkeys(dns_servers)),
                            if_index=current.IfIndex,
                            is_wlan=is_wlan,
                        )
                    )
                unicast_address = unicast_address.contents.Next
        adapter = current.Next

    configs.sort(
        key=lambda config: (
            ipaddress.ip_address(config.source_ip) not in PREFERRED_CAMPUS_NETWORK,
            not bool(config.dns_servers),
        )
    )
    return configs


def discover_adapter_configs() -> list[AdapterDnsConfig]:
    if sys.platform == "win32":
        return windows_adapter_dns_configs()
    return macos_adapter_dns_configs()


def _adapter_preference(config: AdapterDnsConfig) -> tuple[bool, bool]:
    return (
        ipaddress.ip_address(config.source_ip) not in PREFERRED_CAMPUS_NETWORK,
        not config.interface.startswith("en") if sys.platform != "win32" else False,
    )


# ---------------------------------------------------------------------------
# 接口钉定


def _pin_value(
    interface: str,
    if_index: int,
    platform: str | None = None,
) -> tuple[int, int, int] | None:
    platform = platform or sys.platform
    if platform == "win32":
        if not if_index:
            return None
        return (socket.IPPROTO_IP, IP_UNICAST_IF, socket.ntohl(if_index))
    if interface:
        return (socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(interface))
    return None


def _pin_socket(sock: socket.socket, interface: str, if_index: int) -> None:
    pin = _pin_value(interface, if_index)
    if pin is None:
        raise DirectNetworkError("无法钉定物理网卡，已停止不安全的直连回退")
    sock.setsockopt(*pin)


# ---------------------------------------------------------------------------
# 严格直连 DNS


def _read_dns_name(
    packet: bytes,
    offset: int,
    *,
    _visited: set[int] | None = None,
) -> tuple[str, int]:
    """读取 DNS 名称并返回名称与下一个字段位置，拒绝压缩指针环路。"""
    visited = set() if _visited is None else _visited
    labels: list[str] = []
    next_offset: int | None = None
    while True:
        if offset >= len(packet):
            raise DirectNetworkError("直连 DNS 名称越界")
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 2 > len(packet):
                raise DirectNetworkError("直连 DNS 压缩指针越界")
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            if next_offset is None:
                next_offset = offset + 2
            if pointer in visited or pointer >= len(packet):
                raise DirectNetworkError("直连 DNS 压缩指针无效")
            visited.add(pointer)
            offset = pointer
            continue
        if length & 0xC0:
            raise DirectNetworkError("直连 DNS 名称标签无效")
        offset += 1
        if length == 0:
            return ".".join(labels).lower(), next_offset if next_offset is not None else offset
        if length > 63 or offset + length > len(packet):
            raise DirectNetworkError("直连 DNS 名称标签越界")
        try:
            labels.append(packet[offset : offset + length].decode("ascii").lower())
        except UnicodeDecodeError as exc:
            raise DirectNetworkError("直连 DNS 名称不是有效 ASCII") from exc
        offset += length


def query_a_record(
    hostname: str,
    dns_server: str,
    source_ip: str,
    timeout: float = 2.0,
    interface: str = "",
    if_index: int = 0,
) -> list[str]:
    transaction_id = secrets.randbits(16)
    labels = hostname.rstrip(".").split(".")
    question_name = b"".join(
        bytes((len(label.encode("idna")),)) + label.encode("idna")
        for label in labels
    ) + b"\x00"
    packet = (
        struct.pack("!HHHHHH", transaction_id, 0x0100, 1, 0, 0, 0)
        + question_name
        + struct.pack("!HH", 1, 1)
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.bind((source_ip, 0))
        _pin_socket(sock, interface, if_index)
        # connected UDP 只接收指定 DNS 服务器发来的数据报。
        sock.connect((dns_server, 53))
        sock.send(packet)
        response = sock.recv(4096)
    finally:
        sock.close()

    if len(response) < 12:
        raise DirectNetworkError("直连 DNS 响应过短")
    response_id, flags, question_count, answer_count, _authority, _extra = (
        struct.unpack("!HHHHHH", response[:12])
    )
    if (
        response_id != transaction_id
        or not flags & 0x8000
        or (flags >> 11) & 0xF
        or flags & 0x0200
        or flags & 0x000F
        or question_count != 1
    ):
        raise DirectNetworkError("直连 DNS 响应头或返回码无效")

    offset = 12
    question, offset = _read_dns_name(response, offset)
    if offset + 4 > len(response):
        raise DirectNetworkError("直连 DNS 问题区不完整")
    query_type, query_class = struct.unpack("!HH", response[offset : offset + 4])
    offset += 4
    expected_name = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if question != expected_name or query_type != 1 or query_class != 1:
        raise DirectNetworkError("直连 DNS 响应与查询问题不一致")

    aliases: dict[str, str] = {}
    answers: dict[str, list[str]] = {}
    for _ in range(answer_count):
        owner, offset = _read_dns_name(response, offset)
        if offset + 10 > len(response):
            raise DirectNetworkError("直连 DNS 答案区不完整")
        record_type, record_class, _ttl, length = struct.unpack(
            "!HHIH", response[offset : offset + 10]
        )
        offset += 10
        if offset + length > len(response):
            raise DirectNetworkError("直连 DNS 记录数据越界")
        value_offset = offset
        offset += length
        if record_class != 1:
            continue
        if record_type == 5 and length:
            target, _ = _read_dns_name(response, value_offset)
            aliases[owner] = target
        elif record_type == 1 and length == 4:
            address = socket.inet_ntoa(response[value_offset : offset])
            if ipaddress.ip_address(address) not in FAKE_IP_NETWORK:
                answers.setdefault(owner, []).append(address)

    # 只沿查询名的 CNAME 链接受 A 记录；authority/additional 不参与答案。
    addresses: list[str] = []
    current = expected_name
    visited_names: set[str] = set()
    while current not in visited_names:
        visited_names.add(current)
        addresses.extend(answers.get(current, ()))
        if current not in aliases:
            break
        current = aliases[current]
    return list(dict.fromkeys(addresses))


# ---------------------------------------------------------------------------
# 保留原 URL/TLS 主机名的自定义直连连接


class DirectConnectionMixin:
    """只替换 TCP 目标，不触碰 self.host、SNI 或证书校验。"""

    def _new_conn(self):
        try:
            addresses = self.direct_route.resolve(self._dns_host)
        except (OSError, DirectNetworkError) as exc:
            raise NewConnectionError(self, f"直连解析失败：{exc}") from exc

        errors: list[str] = []
        for address in addresses:
            if not _valid_target_ipv4(address, allow_private=self.allow_private):
                errors.append(f"拒绝目标 {address}")
                continue
            try:
                return create_connection(
                    (address, self.port),
                    timeout=self.timeout,
                    source_address=self.source_address,
                    socket_options=self.socket_options,
                )
            except OSError as exc:
                errors.append(f"{address}: {exc}")
        detail = "；".join(errors) or "没有符合策略的 IPv4 地址"
        raise NewConnectionError(self, f"无法建立直连：{detail}")


class DirectHTTPConnection(DirectConnectionMixin, HTTPConnection):
    pass


class DirectHTTPSConnection(DirectConnectionMixin, HTTPSConnection):
    pass


class DirectPoolMixin:
    def _new_conn(self):
        # 必须在 HTTPSConnectionPool._validate_conn() 调用 connect() 之前
        # 注入路由，不能只在 super() 返回后再补属性。
        connection = self.ConnectionCls(host=self.host, port=self.port, **self.conn_kw)
        connection.direct_route = self.direct_route
        connection.allow_private = self.allow_private
        return connection


class DirectHTTPConnectionPool(DirectPoolMixin, HTTPConnectionPool):
    ConnectionCls = DirectHTTPConnection


class DirectHTTPSConnectionPool(DirectPoolMixin, HTTPSConnectionPool):
    ConnectionCls = DirectHTTPSConnection


class DirectPoolManager(PoolManager):
    def __init__(
        self,
        num_pools: int,
        maxsize: int,
        block: bool = False,
        *,
        direct_route: DirectRoute,
        allow_private: bool,
        **connection_pool_kw,
    ):
        self.direct_route = direct_route
        self.allow_private = allow_private
        super().__init__(
            num_pools=num_pools,
            maxsize=maxsize,
            block=block,
            **connection_pool_kw,
        )
        self.pool_classes_by_scheme = self.pool_classes_by_scheme.copy()
        self.pool_classes_by_scheme.update(
            {
                "http": DirectHTTPConnectionPool,
                "https": DirectHTTPSConnectionPool,
            }
        )

    def _new_pool(self, scheme, host, port, request_context):
        pool = super()._new_pool(scheme, host, port, request_context)
        pool.direct_route = self.direct_route
        pool.allow_private = self.allow_private
        return pool


class DirectAdapter(HTTPAdapter):
    def __init__(self, route: DirectRoute, *, allow_private: bool, **kwargs):
        self.direct_route = route
        self.allow_private = allow_private
        self.source_ip = route.source_ip
        self.interface = route.interface
        self.if_index = route.if_index
        super().__init__(**kwargs)

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs,
    ) -> None:
        pin = _pin_value(self.interface, self.if_index)
        if pin is None:
            raise DirectNetworkError("无法钉定物理网卡，直连会话已拒绝启动")
        pool_kwargs["source_address"] = (self.source_ip, 0)
        pool_kwargs["socket_options"] = [
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            pin,
        ]
        self.poolmanager = DirectPoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            direct_route=self.direct_route,
            allow_private=self.allow_private,
            **pool_kwargs,
        )

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        raise DirectNetworkError("直连会话不允许显式代理")


@dataclass(slots=True)
class DirectRoute:
    source_ip: str
    dns_servers: tuple[str, ...] = DIRECT_DNS_SERVERS
    interface: str = ""
    if_index: int = 0
    _cache: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def discover(cls) -> "DirectRoute":
        try:
            adapter_configs = discover_adapter_configs()
        except (OSError, subprocess.SubprocessError) as exc:
            raise DirectNetworkError(f"无法枚举物理网卡：{exc}") from exc

        adapter_configs.sort(key=_adapter_preference)
        routes = [
            cls(
                config.source_ip,
                _merge_dns_servers(config.dns_servers, DIRECT_DNS_SERVERS),
                config.interface,
                config.if_index,
            )
            for config in adapter_configs
        ]
        if not routes:
            raise DirectNetworkError(
                "未找到可钉定的校园网物理网卡；请确认已连接校园有线网络或 SZU_WLAN"
            )

        errors: list[str] = []
        for route in routes:
            try:
                if _pin_value(route.interface, route.if_index) is None:
                    raise DirectNetworkError("接口索引不可用")
                route.resolve("net.szu.edu.cn")
                return route
            except (OSError, DirectNetworkError) as exc:
                errors.append(f"{route.interface}: {exc}")
        raise DirectNetworkError(
            "无法通过物理网卡进行直连 DNS 查询（" + "；".join(errors) + "）"
        )

    def resolve(self, hostname: str) -> list[str]:
        hostname = hostname.rstrip(".").lower()
        try:
            ipaddress.ip_address(hostname)
            return [hostname]
        except ValueError:
            pass
        if hostname in self._cache:
            return self._cache[hostname]

        errors: list[str] = []
        for server in self.dns_servers:
            try:
                addresses = query_a_record(
                    hostname,
                    server,
                    self.source_ip,
                    interface=self.interface,
                    if_index=self.if_index,
                )
                if addresses:
                    self._cache[hostname] = addresses
                    return addresses
            except (OSError, DirectNetworkError) as exc:
                errors.append(type(exc).__name__)
        detail = "/".join(errors) or "无属于查询名的 A 记录"
        raise DirectNetworkError(f"无法直连解析 {hostname}（{detail}）")

    def session(self, *, allow_private: bool = False) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        adapter = DirectAdapter(self, allow_private=allow_private)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session
