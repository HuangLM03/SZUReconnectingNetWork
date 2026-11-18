"""严格的公共互联网连通性探测。

这里判断的是公共 Internet，不是校园门户或校内网站。所有探针均使用
HTTPS、原主机名和默认 CA 校验；认证成功、Wi-Fi 有 IP、校内网页可达
都不会进入本模块的成功条件。
"""

from __future__ import annotations

import http.client
import socket
import time
from dataclasses import dataclass
from enum import Enum
from typing import Iterable
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from .direct_network import DirectNetworkError, DirectRoute


MAX_RESPONSE_BYTES = 64 * 1024
REQUIRED_SUCCESSES = 2


class InternetStatus(str, Enum):
    ONLINE = "online"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProbeSpec:
    name: str
    url: str
    exact_text: str | None = None
    text_marker: str | None = None
    content_type: str | None = None
    expected_status: int = 200

    @property
    def hostname(self) -> str:
        return urlsplit(self.url).hostname or ""


# 不使用明文 NCSI，也不使用“页面回显随机参数即成功”的判断。
# 三个目标分属不同运营方；至少两个严格通过才判定 ONLINE。
PUBLIC_PROBES = (
    ProbeSpec(
        name="Apple",
        url="https://captive.apple.com/hotspot-detect.html",
        exact_text="<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>",
        content_type="text/html",
    ),
    ProbeSpec(
        name="Baidu",
        url="https://www.baidu.com/",
        text_marker="<title>百度一下，你就知道</title>",
        content_type="text/html",
    ),
    ProbeSpec(
        name="Taobao",
        url="https://www.taobao.com/",
        text_marker="<html",
        content_type="text/html",
    ),
)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    url: str
    success: bool
    detail: str
    status_code: int | None = None
    remote_ip: str = ""
    latency_ms: int | None = None

    def diagnostic(self) -> str:
        parts = [self.name, "通过" if self.success else "失败"]
        if self.status_code is not None:
            parts.append(f"HTTP {self.status_code}")
        if self.remote_ip:
            parts.append(f"目标 {self.remote_ip}")
        if self.latency_ms is not None:
            parts.append(f"{self.latency_ms} ms")
        parts.append(self.detail)
        return "：".join((parts[0], "，".join(parts[1:])))


@dataclass(frozen=True, slots=True)
class PathReport:
    path: str
    status: InternetStatus
    probes: tuple[ProbeResult, ...]
    latency_ms: int
    interface: str = ""
    source_ip: str = ""
    setup_error: str = ""

    @property
    def succeeded(self) -> int:
        return sum(probe.success for probe in self.probes)

    def diagnostic(self) -> str:
        location = self.interface or self.path
        if self.source_ip:
            location += f"/{self.source_ip}"
        details = "；".join(probe.diagnostic() for probe in self.probes)
        if self.setup_error:
            details = self.setup_error
        return f"{location}：{self.status.value}（{details or '无探针结果'}）"


def _response_remote_ip(response: requests.Response) -> str:
    """尽力读取已连接 socket 的对端地址，不影响探测结论。"""
    try:
        connection = getattr(response.raw, "_connection", None)
        sock = getattr(connection, "sock", None)
        if sock is None:
            return ""
        peer = sock.getpeername()
        return str(peer[0]) if peer else ""
    except (AttributeError, TypeError, ValueError, OSError, socket.error):
        return ""


def _read_bounded(response: requests.Response) -> tuple[bytes, bool]:
    """最多读取限制 + 1 字节，避免把大型首页完整载入内存。"""
    raw = getattr(response, "raw", None)
    if raw is not None and hasattr(raw, "read"):
        try:
            data = raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
        except TypeError:  # 测试替身或不带 decode_content 的 file-like
            data = raw.read(MAX_RESPONSE_BYTES + 1)
    else:
        data = response.content[: MAX_RESPONSE_BYTES + 1]
    return bytes(data[:MAX_RESPONSE_BYTES]), len(data) > MAX_RESPONSE_BYTES


def _validate_response(
    spec: ProbeSpec,
    response: requests.Response,
) -> tuple[bool, str]:
    final = urlsplit(response.url or spec.url)
    if final.scheme.lower() != "https" or (final.hostname or "").lower() != spec.hostname.lower():
        return False, "响应来源不是原始 HTTPS 主机"
    if response.history:
        return False, "探测请求发生了重定向"
    if response.status_code != spec.expected_status:
        return False, f"预期 HTTP {spec.expected_status}"

    content_type = response.headers.get("Content-Type", "").lower()
    if spec.content_type and spec.content_type not in content_type:
        return False, f"内容类型不符（{content_type or '缺失'}）"

    body, truncated = _read_bounded(response)
    if truncated and spec.exact_text is not None:
        return False, f"固定响应超过 {MAX_RESPONSE_BYTES} 字节上限"
    text = body.decode("utf-8", errors="replace").strip()
    if spec.exact_text is not None and text != spec.exact_text:
        return False, "固定响应正文不一致"
    if spec.text_marker is not None and spec.text_marker.lower() not in text.lower():
        return False, "响应正文缺少预期标记"
    return True, "TLS、来源与响应内容均通过校验"


def run_probe(
    session: requests.Session,
    spec: ProbeSpec,
    timeout: tuple[float, float] = (3.0, 5.0),
) -> ProbeResult:
    started = time.perf_counter()
    response: requests.Response | None = None
    try:
        response = session.get(
            spec.url,
            timeout=timeout,
            allow_redirects=False,
            verify=True,
            stream=True,
        )
        remote_ip = _response_remote_ip(response)
        valid, detail = _validate_response(spec, response)
        return ProbeResult(
            spec.name,
            spec.url,
            valid,
            detail,
            status_code=response.status_code,
            remote_ip=remote_ip,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
    except (
        requests.RequestException,
        Urllib3HTTPError,
        http.client.HTTPException,
        DirectNetworkError,
        OSError,
    ) as exc:
        return ProbeResult(
            spec.name,
            spec.url,
            False,
            f"{type(exc).__name__}: {str(exc)[:180]}",
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
    finally:
        if response is not None:
            response.close()


def check_path(
    session: requests.Session,
    path: str,
    *,
    route: DirectRoute | None = None,
    probes: Iterable[ProbeSpec] = PUBLIC_PROBES,
) -> PathReport:
    started = time.perf_counter()
    results = tuple(run_probe(session, spec) for spec in probes)
    succeeded = sum(result.success for result in results)
    if succeeded >= REQUIRED_SUCCESSES:
        status = InternetStatus.ONLINE
    elif succeeded == 1:
        status = InternetStatus.DEGRADED
    elif any(result.status_code is not None for result in results):
        status = InternetStatus.BLOCKED
    else:
        status = InternetStatus.UNKNOWN
    return PathReport(
        path=path,
        status=status,
        probes=results,
        latency_ms=round((time.perf_counter() - started) * 1000),
        interface=route.interface if route else "",
        source_ip=route.source_ip if route else "",
    )


def unavailable_path(path: str, error: BaseException | str) -> PathReport:
    return PathReport(
        path=path,
        status=InternetStatus.UNKNOWN,
        probes=(),
        latency_ms=0,
        setup_error=str(error),
    )
