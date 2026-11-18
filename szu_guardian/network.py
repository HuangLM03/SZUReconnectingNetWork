"""校园认证与双路径互联网健康检查。

校园认证、物理网卡直连、系统应用路径是三个不同事实。只有本轮严格
公网探测得到整体 ONLINE，才允许上层报告互联网恢复。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable
from urllib.parse import urlsplit

import requests

from .connectivity import (
    InternetStatus,
    PathReport,
    check_path,
    unavailable_path,
)
from .direct_network import DirectNetworkError, DirectRoute
from .models import AppConfig, ZONE_AUTO, ZONE_DORMITORY, ZONE_OFFICE
from .srun import SrunClient


DORMITORY_LOGIN_URL = "http://172.30.255.42:801/eportal/portal/login/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/124 Safari/537.36"
}

ProgressCallback = Callable[[str], None]


class AuthStatus(str, Enum):
    NOT_ATTEMPTED = "not_attempted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class PortalError(ConnectionError):
    pass


class PortalUnavailable(PortalError):
    """门户不可达或响应不可信，可能随链路恢复而解决。"""


class AuthenticationRejected(PortalError):
    """门户明确拒绝认证，不应通过重置 Wi-Fi 或机械重试处理。"""


@dataclass(frozen=True, slots=True)
class AuthEvidence:
    status: AuthStatus
    message: str
    client_ip: str = ""


@dataclass(frozen=True, slots=True)
class ConnectionResult:
    status: InternetStatus
    message: str
    direct_report: PathReport | None = None
    system_report: PathReport | None = None
    latency_ms: int | None = None
    auth_status: AuthStatus = AuthStatus.NOT_ATTEMPTED
    auth_client_ip: str = ""
    recovered: bool = False

    @property
    def connected(self) -> bool:
        return self.status == InternetStatus.ONLINE

    @property
    def auth_succeeded(self) -> bool:
        return self.auth_status == AuthStatus.ACCEPTED

    @property
    def diagnostics(self) -> tuple[str, ...]:
        return tuple(
            report.diagnostic()
            for report in (self.direct_report, self.system_report)
            if report is not None
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "connected": self.connected,
            "message": self.message,
            "latency_ms": self.latency_ms,
            "auth_status": self.auth_status.value,
            "auth_client_ip": self.auth_client_ip,
            "recovered": self.recovered,
            "diagnostics": list(self.diagnostics),
        }


def _new_session(*, trust_env: bool) -> requests.Session:
    session = requests.Session()
    session.trust_env = trust_env
    session.headers.update(HEADERS)
    return session


def _parse_jsonp_object(text: str) -> dict[str, object]:
    match = re.fullmatch(
        r"([A-Za-z_$][\w$]*)\((.*)\)\s*;?\s*",
        text.strip(),
        re.DOTALL,
    )
    if not match or match.group(1) != "jsonpReturn":
        raise PortalUnavailable("宿舍区认证服务器返回了来源不符的 JSONP")
    try:
        payload = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        raise PortalUnavailable("宿舍区认证服务器返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise PortalUnavailable("宿舍区认证响应格式不正确")
    return payload


def _combine_reports(
    direct: PathReport | None,
    system: PathReport,
    *,
    direct_enabled: bool,
) -> ConnectionResult:
    latency = max(direct.latency_ms if direct else 0, system.latency_ms)
    if not direct_enabled:
        messages = {
            InternetStatus.ONLINE: "互联网连接正常（系统应用路径通过严格验证）",
            InternetStatus.DEGRADED: "系统应用路径只有部分公网探针通过",
            InternetStatus.BLOCKED: "公共互联网不可用",
            InternetStatus.UNKNOWN: "公共互联网状态未知",
        }
        return ConnectionResult(
            system.status,
            messages[system.status],
            None,
            system,
            latency,
        )

    if direct is None:
        raise ValueError("启用直连时必须提供直连报告")
    if direct.status == InternetStatus.ONLINE and system.status == InternetStatus.ONLINE:
        return ConnectionResult(
            InternetStatus.ONLINE,
            "互联网连接正常（物理直连与系统应用路径均通过严格验证）",
            direct,
            system,
            latency,
        )
    if direct.status == InternetStatus.ONLINE:
        return ConnectionResult(
            InternetStatus.DEGRADED,
            "物理网卡已能访问公共互联网，但系统代理/TUN 应用路径未通过验证",
            direct,
            system,
            latency,
        )
    if system.status == InternetStatus.ONLINE:
        return ConnectionResult(
            InternetStatus.DEGRADED,
            "系统应用路径可访问互联网，但物理网卡直连未通过验证",
            direct,
            system,
            latency,
        )
    if InternetStatus.DEGRADED in (direct.status, system.status):
        return ConnectionResult(
            InternetStatus.DEGRADED,
            "公共互联网仅部分可达，不能报告为完全恢复",
            direct,
            system,
            latency,
        )
    if direct.status == InternetStatus.BLOCKED and system.status == InternetStatus.BLOCKED:
        status = InternetStatus.BLOCKED
        message = "公共互联网不可用（校内网或门户可达不代表互联网恢复）"
    else:
        status = InternetStatus.UNKNOWN
        message = "公共互联网状态未知，尚未取得足够的在线证据"
    return ConnectionResult(status, message, direct, system, latency)


class NetworkClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        portal_session: requests.Session | None = None,
        system_session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        verify_delays: tuple[float, ...] = (2.0, 4.0, 8.0),
        route_factory: Callable[[], DirectRoute] = DirectRoute.discover,
    ):
        self.session = session
        self.portal_session = portal_session
        self.system_session = system_session or _new_session(trust_env=True)
        self.sleeper = sleeper
        self.verify_delays = verify_delays
        self.route_factory = route_factory
        self.direct_route: DirectRoute | None = None
        self._allow_direct_setup = session is None and portal_session is None
        for active_session in (session, portal_session, self.system_session):
            if active_session is not None:
                active_session.headers.update(HEADERS)

    def _configure_direct(self, enabled: bool) -> None:
        if self.session is not None:
            if self.portal_session is None:
                self.portal_session = self.session
            return
        if enabled and self._allow_direct_setup:
            route = self.route_factory()
            self.direct_route = route
            # 公共 Internet 不允许私网目标；校园门户使用另一个连接池。
            self.session = route.session(allow_private=False)
            self.portal_session = route.session(allow_private=True)
        else:
            self.session = _new_session(trust_env=False)
            self.portal_session = self.portal_session or _new_session(trust_env=False)
        self.session.headers.update(HEADERS)
        self.portal_session.headers.update(HEADERS)

    def check_connection(self, config: AppConfig | None = None) -> ConnectionResult:
        """只检测，不认证；CLI 与后台监控共用这一入口。"""
        direct_enabled = config.direct_mode if config is not None else True
        direct_report: PathReport | None = None
        if direct_enabled:
            try:
                self._configure_direct(True)
            except (DirectNetworkError, OSError, requests.RequestException) as exc:
                direct_report = unavailable_path("direct", exc)
            else:
                direct_report = check_path(
                    self.session,
                    "direct",
                    route=self.direct_route,
                )
        else:
            self._configure_direct(False)

        system_report = check_path(self.system_session, "system")
        return _combine_reports(
            direct_report,
            system_report,
            direct_enabled=direct_enabled,
        )

    def _teaching_srun_client(self, config: AppConfig) -> SrunClient:
        if self.portal_session is None:
            raise PortalUnavailable("校园认证会话尚未初始化")
        return SrunClient(
            config.username,
            config.password,
            session=self.portal_session,
            expected_client_ip=self.direct_route.source_ip if self.direct_route else "",
        )

    def _teaching_online_status(self, config: AppConfig) -> bool | None:
        """校园会话诊断，不参与互联网在线判定。"""
        try:
            return self._teaching_srun_client(config).is_online()
        except (ConnectionError, requests.RequestException):
            return None

    def _login_dormitory(self, config: AppConfig) -> AuthEvidence:
        if self.portal_session is None:
            raise PortalUnavailable("校园认证会话尚未初始化")
        response = self.portal_session.get(
            DORMITORY_LOGIN_URL,
            params={
                "user_account": config.username,
                "user_password": config.password,
            },
            timeout=(4, 10),
            allow_redirects=False,
        )
        response.raise_for_status()
        expected = urlsplit(DORMITORY_LOGIN_URL)
        actual = urlsplit(response.url or DORMITORY_LOGIN_URL)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise PortalUnavailable("宿舍区认证响应来自非预期服务器")
        payload = _parse_jsonp_object(response.text)
        message = str(payload.get("msg", "")).strip()
        # result 是唯一成功依据；错误消息中出现“成功”不能覆盖失败字段。
        if payload.get("result") not in (1, "1", True):
            raise AuthenticationRejected(message or "宿舍区认证失败，请检查账号和密码")
        return AuthEvidence(AuthStatus.ACCEPTED, message or "宿舍区认证成功")

    def _login_teaching(self, config: AppConfig) -> AuthEvidence:
        try:
            result = self._teaching_srun_client(config).login()
        except (ConnectionError, requests.RequestException) as exc:
            raise PortalUnavailable(f"教学 / 办公区门户不可用：{exc}") from exc
        if not result.success:
            if result.rejected:
                raise AuthenticationRejected(
                    f"教学 / 办公区认证被拒绝：{result.message or '请检查账号和密码'}"
                )
            raise PortalUnavailable(
                f"教学 / 办公区认证未完成：{result.message or '门户响应不完整'}"
            )
        return AuthEvidence(AuthStatus.ACCEPTED, result.message, result.client_ip)

    def send_login(
        self,
        config: AppConfig,
        progress_callback: ProgressCallback | None = None,
    ) -> AuthEvidence:
        zone = config.zone
        if zone == ZONE_AUTO:
            if progress_callback:
                progress_callback("自动尝试：正在使用教学 / 办公区认证…")
            try:
                return self._login_teaching(config)
            except AuthenticationRejected:
                raise
            except (PortalUnavailable, requests.RequestException) as teaching_error:
                if progress_callback:
                    progress_callback("教学 / 办公区门户不可用，改试宿舍区认证…")
                try:
                    return self._login_dormitory(config)
                except AuthenticationRejected:
                    raise
                except (PortalUnavailable, requests.RequestException) as dormitory_error:
                    raise PortalUnavailable(
                        f"自动区域认证均未完成：教学 / 办公区：{teaching_error}；"
                        f"宿舍区：{dormitory_error}"
                    ) from dormitory_error

        if progress_callback:
            zone_name = "宿舍区" if zone == ZONE_DORMITORY else "教学 / 办公区"
            progress_callback(f"正在使用{zone_name}认证…")
        if zone == ZONE_DORMITORY:
            return self._login_dormitory(config)
        if zone == ZONE_OFFICE:
            return self._login_teaching(config)
        raise PortalUnavailable("无法确定校园网区域")

    @staticmethod
    def _should_authenticate(result: ConnectionResult) -> bool:
        if result.status in (InternetStatus.ONLINE, InternetStatus.DEGRADED):
            return False
        if result.direct_report is not None:
            if result.direct_report.setup_error:
                return False
            if result.system_report and result.system_report.status in (
                InternetStatus.ONLINE,
                InternetStatus.DEGRADED,
            ):
                return False
            return result.direct_report.status in (
                InternetStatus.BLOCKED,
                InternetStatus.UNKNOWN,
            )
        return bool(
            result.system_report
            and result.system_report.status in (
                InternetStatus.BLOCKED,
                InternetStatus.UNKNOWN,
            )
        )

    def ensure_connected(
        self,
        config: AppConfig,
        progress_callback: ProgressCallback | None = None,
    ) -> ConnectionResult:
        current = self.check_connection(config)
        if not self._should_authenticate(current):
            return current

        try:
            evidence = self.send_login(config, progress_callback)
        except AuthenticationRejected as exc:
            return replace(
                current,
                message=str(exc),
                auth_status=AuthStatus.REJECTED,
            )
        except (PortalUnavailable, requests.RequestException) as exc:
            return replace(
                current,
                message=f"校园认证未完成：{exc}",
                auth_status=AuthStatus.UNKNOWN,
            )

        if progress_callback:
            progress_callback(f"{evidence.message}，正在严格验证公共互联网…")
        latest = replace(
            current,
            auth_status=AuthStatus.ACCEPTED,
            auth_client_ip=evidence.client_ip,
        )
        for attempt, delay in enumerate(self.verify_delays, start=1):
            self.sleeper(delay)
            verified = self.check_connection(config)
            latest = replace(
                verified,
                auth_status=AuthStatus.ACCEPTED,
                auth_client_ip=evidence.client_ip,
            )
            if latest.connected:
                return replace(
                    latest,
                    message="互联网已恢复（物理直连与系统应用路径均通过严格验证）"
                    if config.direct_mode
                    else "互联网已恢复（系统应用路径通过严格验证）",
                    recovered=True,
                )
            if latest.status == InternetStatus.DEGRADED:
                return replace(
                    latest,
                    message=latest.message + "；认证已接受，但未报告为互联网完全恢复",
                )
            if progress_callback and attempt < len(self.verify_delays):
                progress_callback(
                    f"公共互联网尚未恢复，{self.verify_delays[attempt]:g} 秒后再次复查…"
                )

        return replace(
            latest,
            message="校园认证已被服务器接受，但公共互联网仍未通过严格验证",
        )
