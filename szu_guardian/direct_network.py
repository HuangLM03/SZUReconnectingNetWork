"""直连（绑定物理网卡）网络层，Windows 与 macOS 双平台。

目的：当系统运行 VPN / 代理（TUN fake-IP）时，让检测与认证流量走真实
校园网网卡。

- 网卡枚举：macOS 解析 ``ifconfig -a`` + ``scutil --dns``；
  Windows 通过 ``GetAdaptersAddresses``（ctypes，移植自上游原版，
  额外捕获 FriendlyName / IfIndex / IfType）
- 接口钉定：macOS 用 ``IP_BOUND_IF``；Windows 用 ``IP_UNICAST_IF``
  （值需网络字节序，即 ``socket.ntohl(if_index)``，历史性坑）
- 直连 DNS 查询与源地址绑定的 requests 会话为双平台共用实现
"""

from __future__ import annotations

import ipaddress
import random
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3 import PoolManager


FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
PREFERRED_CAMPUS_NETWORK = ipaddress.ip_network("172.30.0.0/16")
DIRECT_DNS_SERVERS = (
    "192.168.247.6",
    "223.5.5.5",
    "119.29.29.29",
    "114.114.114.114",
)

# macOS netinet/in.h：把 socket 钉在指定接口上（接口作用域路由）。
# 仅绑定源地址不足以绕过 VPN/TUN：默认路由指向 utun 时，内核会因
# 源地址不属于出接口而报 Errno 49（Can't assign requested address）。
IP_BOUND_IF = 25
# Windows ws2ipdef.h 的等价选项；值要求网络字节序的接口索引。
IP_UNICAST_IF = 31

# Windows GetAdaptersAddresses 相关常量
WINDOWS_NO_ERROR = 0
WINDOWS_ERROR_BUFFER_OVERFLOW = 111
WINDOWS_IF_OPER_STATUS_UP = 1
WINDOWS_IF_TYPE_IEEE80211 = 71  # WLAN
WINDOWS_GAA_FLAGS = 0x0002 | 0x0004  # Skip anycast and multicast addresses.

# macOS 虚拟 / 本地回环接口：直连时不应绑定到这些网卡
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
    if_index: int = 0  # Windows: IfIndex；macOS 运行时用 if_nametoindex
    is_wlan: bool = False  # Windows: IfType==71；macOS 不区分


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
    """Return ``(interface, ipv4)`` pairs for active, physical interfaces."""
    candidates: list[tuple[str, str]] = []
    current_interface = ""
    interface_active = False

    for line in output.splitlines():
        stripped = line.strip()
        if not line:
            continue
        is_header = not line[0].isspace()
        if is_header:
            current_interface = line.split(":", 1)[0]
            interface_active = "RUNNING" in line
            continue
        if not current_interface:
            continue

        if stripped.startswith("status:"):
            interface_active = stripped == "status: active" or interface_active
            continue
        if not interface_active:
            continue

        if stripped.startswith("inet "):
            address = stripped.split()[1]
            if (
                _valid_source_ipv4(address)
                and not current_interface.startswith(SKIPPED_INTERFACE_PREFIXES)
            ):
                candidates.append((current_interface, address))

    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for item in candidates:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


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
    """Discover active IPv4 interfaces and the system DNS servers."""
    try:
        interface_output = command_runner(["ifconfig", "-a"])
    except (OSError, subprocess.SubprocessError):
        interface_output = ""

    dns_servers: tuple[str, ...] = ()
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
    """Read active IPv4 addresses and their DNS servers via GetAdaptersAddresses.

    移植自上游 Windows 原版，额外捕获 FriendlyName / IfIndex / IfType，
    供接口钉定与 Wi-Fi 模块使用。
    """
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
    """平台分派：枚举可用的物理网卡直连配置。"""
    if sys.platform == "win32":
        return windows_adapter_dns_configs()
    return macos_adapter_dns_configs()


def _adapter_preference(config: AdapterDnsConfig) -> tuple[bool, bool]:
    return (
        ipaddress.ip_address(config.source_ip) not in PREFERRED_CAMPUS_NETWORK,
        not config.interface.startswith("en") if sys.platform != "win32" else False,
    )


# ---------------------------------------------------------------------------
# 接口钉定（双平台）


def _pin_value(interface: str, if_index: int, platform: str | None = None) -> tuple[int, int, int] | None:
    """返回 (level, optname, value)；无法钉定时返回 None。"""
    platform = platform or sys.platform
    if platform == "win32":
        if not if_index:
            return None
        # IP_UNICAST_IF 要求接口索引为网络字节序
        return (socket.IPPROTO_IP, IP_UNICAST_IF, socket.ntohl(if_index))
    if interface:
        return (socket.IPPROTO_IP, IP_BOUND_IF, socket.if_nametoindex(interface))
    return None


def _pin_socket(sock: socket.socket, interface: str, if_index: int) -> None:
    pin = _pin_value(interface, if_index)
    if pin:
        sock.setsockopt(*pin)


# ---------------------------------------------------------------------------
# 直连 DNS


def _skip_dns_name(packet: bytes, offset: int) -> int:
    while offset < len(packet):
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            return offset + 2
        if length == 0:
            return offset + 1
        offset += length + 1
    raise DirectNetworkError("直连 DNS 返回了无效数据")


def query_a_record(
    hostname: str,
    dns_server: str,
    source_ip: str,
    timeout: float = 2.0,
    interface: str = "",
    if_index: int = 0,
) -> list[str]:
    transaction_id = random.randint(0, 0xFFFF)
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
        if interface or if_index:
            # 强制从物理接口发出，避免 VPN/TUN 抢走 DNS 流量
            _pin_socket(sock, interface, if_index)
        sock.sendto(packet, (dns_server, 53))
        response, _ = sock.recvfrom(4096)
    finally:
        sock.close()

    if len(response) < 12:
        raise DirectNetworkError("直连 DNS 响应过短")
    response_id, flags, question_count, answer_count, authority_count, extra_count = (
        struct.unpack("!HHHHHH", response[:12])
    )
    if response_id != transaction_id or flags & 0x000F:
        raise DirectNetworkError("直连 DNS 查询失败")

    offset = 12
    for _ in range(question_count):
        offset = _skip_dns_name(response, offset) + 4

    addresses: list[str] = []
    for _ in range(answer_count + authority_count + extra_count):
        offset = _skip_dns_name(response, offset)
        if offset + 10 > len(response):
            break
        record_type, record_class, _ttl, length = struct.unpack(
            "!HHIH", response[offset : offset + 10]
        )
        offset += 10
        value = response[offset : offset + length]
        offset += length
        if record_type == 1 and record_class == 1 and length == 4:
            address = socket.inet_ntoa(value)
            if ipaddress.ip_address(address) not in FAKE_IP_NETWORK:
                addresses.append(address)
    return list(dict.fromkeys(addresses))


def local_ipv4_candidates() -> list[str]:
    try:
        adapter_addresses = [
            config.source_ip for config in discover_adapter_configs()
        ]
    except (OSError, subprocess.SubprocessError):
        adapter_addresses = []
    if adapter_addresses:
        return list(dict.fromkeys(adapter_addresses))

    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
    except socket.gaierror as exc:
        raise DirectNetworkError("无法读取本机网卡地址") from exc

    valid = [address for address in addresses if _valid_source_ipv4(address)]
    valid.sort(
        key=lambda value: ipaddress.ip_address(value) not in PREFERRED_CAMPUS_NETWORK
    )
    return valid


# ---------------------------------------------------------------------------
# 绑定接口的 requests 会话


class SourceAddressAdapter(HTTPAdapter):
    def __init__(
        self,
        source_ip: str,
        *args,
        interface: str = "",
        if_index: int = 0,
        **kwargs,
    ):
        self.source_ip = source_ip
        self.interface = interface
        self.if_index = if_index
        super().__init__(*args, **kwargs)

    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs,
    ) -> None:
        pool_kwargs["source_address"] = (self.source_ip, 0)
        pin = _pin_value(self.interface, self.if_index)
        if pin:
            pool_kwargs["socket_options"] = [
                (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
                pin,
            ]
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )


def create_bound_session(
    source_ip: str,
    interface: str = "",
    if_index: int = 0,
) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.mount(
        "http://", SourceAddressAdapter(source_ip, interface=interface, if_index=if_index)
    )
    session.mount(
        "https://", SourceAddressAdapter(source_ip, interface=interface, if_index=if_index)
    )
    return session


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
        except (OSError, subprocess.SubprocessError):
            adapter_configs = []

        adapter_configs.sort(key=_adapter_preference)
        if adapter_configs:
            routes = [
                cls(
                    config.source_ip,
                    _merge_dns_servers(config.dns_servers, DIRECT_DNS_SERVERS),
                    config.interface,
                    config.if_index,
                )
                for config in adapter_configs
            ]
        else:
            routes = [cls(source_ip) for source_ip in local_ipv4_candidates()]

        if not routes:
            raise DirectNetworkError(
                "未找到校园网物理网卡；请确认已连接校园有线网络或 SZU_WLAN"
            )

        for route in routes:
            try:
                route.resolve("net.szu.edu.cn")
                return route
            except DirectNetworkError:
                continue
        raise DirectNetworkError(
            "无法通过物理网卡进行直连 DNS 查询；请检查校园网连接"
        )

    def resolve(self, hostname: str) -> list[str]:
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
        detail = "/".join(errors) or "无 A 记录"
        raise DirectNetworkError(f"无法直连解析 {hostname}（{detail}）")

    def session(self) -> requests.Session:
        return create_bound_session(self.source_ip, self.interface, self.if_index)

    def rewrite_url(self, url: str) -> tuple[str, str]:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if not hostname:
            raise DirectNetworkError(f"无效的检测地址：{url}")
        try:
            ipaddress.ip_address(hostname)
            return url, hostname
        except ValueError:
            pass

        address = self.resolve(hostname)[0]
        port = f":{parsed.port}" if parsed.port else ""
        rewritten = urlunsplit(
            (parsed.scheme, f"{address}{port}", parsed.path, parsed.query, parsed.fragment)
        )
        return rewritten, hostname
