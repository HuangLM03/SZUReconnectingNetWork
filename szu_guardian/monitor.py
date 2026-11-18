from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable

from .connectivity import InternetStatus, unavailable_path
from .models import AppConfig
from .network import AuthStatus, ConnectionResult, NetworkClient


FAILED_CYCLE_RETRY_MINUTES = 5
AUTH_REJECTED_RETRY_MINUTES = 30
RECOVERY_SETTLE_SECONDS = 5
RECOVERY_ATTEMPTS = 2
RECOVERY_RETRY_DELAY = 15
AUTH_DEADLOCK_ESCALATE_AFTER = 6


@dataclass(frozen=True, slots=True)
class MonitorEvent:
    state: str
    message: str
    level: str = "info"
    latency_ms: int | None = None
    observed_at: float = field(default_factory=time.time)
    details: tuple[str, ...] = ()
    generation: int = 0
    recovered: bool = False


@dataclass(slots=True)
class _RunContext:
    generation: int
    stop_event: threading.Event = field(default_factory=threading.Event)
    wake_event: threading.Event = field(default_factory=threading.Event)


class MonitorController:
    """每个监控代次都有独立停止/唤醒事件，重启不会复活旧线程。"""

    def __init__(
        self,
        event_callback: Callable[[MonitorEvent], None],
        config_provider: Callable[[], AppConfig],
        client_factory: Callable[[], NetworkClient] = NetworkClient,
    ):
        self.event_callback = event_callback
        self.config_provider = config_provider
        self.client_factory = client_factory
        self._thread: threading.Thread | None = None
        self._context: _RunContext | None = None
        self._lock = threading.Lock()
        self._desired_running = False
        self._generation = 0
        self._auth_deadlock = 0
        self._deadlock_context: tuple[object, ...] | None = None

    @property
    def running(self) -> bool:
        return bool(
            self._desired_running
            and self._thread
            and self._thread.is_alive()
        )

    @property
    def auth_deadlock_count(self) -> int:
        return self._auth_deadlock

    def start(self) -> bool:
        with self._lock:
            if self.running:
                assert self._context is not None
                self._context.wake_event.set()
                return True
            self._desired_running = True
            self._generation += 1
            context = _RunContext(self._generation)
            self._context = context
            self._auth_deadlock = 0
            self._deadlock_context = None
            self._thread = threading.Thread(
                target=self._run_loop,
                args=(context,),
                name=f"szu-network-monitor-{context.generation}",
                daemon=True,
            )
            self._thread.start()
            return True

    def check_now(self) -> None:
        context = self._context
        if self.running and context:
            context.wake_event.set()

    def restart(self) -> None:
        self.stop()
        self.start()

    def stop(self) -> None:
        with self._lock:
            self._desired_running = False
            context = self._context
            thread = self._thread
        if context:
            context.stop_event.set()
            context.wake_event.set()
        if thread and thread.is_alive():
            if thread is threading.current_thread():
                return
            thread.join(timeout=2)
        elif not thread:
            self.event_callback(MonitorEvent("stopped", "监控已停止"))

    def _run_loop(self, context: _RunContext) -> None:
        try:
            self._emit(context, MonitorEvent("started", "监控线程已启动"))
            while not context.stop_event.is_set():
                context.wake_event.clear()
                self._emit(context, MonitorEvent("checking", "正在检测公共互联网…"))
                config = self.config_provider()
                result = self._run_cycle(context, config)
                if context.stop_event.is_set():
                    break

                result = self._handle_auth_deadlock(context, config, result)
                if context.stop_event.is_set():
                    break
                self._emit(context, self._result_event(result))

                if result.connected:
                    wait_minutes = config.interval_minutes
                elif result.auth_status == AuthStatus.REJECTED:
                    wait_minutes = max(
                        config.interval_minutes,
                        AUTH_REJECTED_RETRY_MINUTES,
                    )
                else:
                    wait_minutes = min(
                        config.interval_minutes,
                        FAILED_CYCLE_RETRY_MINUTES,
                    )
                self._emit(
                    context,
                    MonitorEvent(
                        "waiting",
                        f"上次结论：{result.message}；下次检测将在 {wait_minutes} 分钟内进行",
                    ),
                )
                context.wake_event.wait(wait_minutes * 60)
        except Exception as exc:
            self._emit(
                context,
                MonitorEvent(
                    "offline",
                    f"监控线程异常退出：{self._brief_error(exc)}",
                    "error",
                ),
            )
        finally:
            if self._context is context:
                self._desired_running = False
            self._emit(context, MonitorEvent("stopped", "监控已停止"), force=True)

    def _run_cycle(self, context: _RunContext, config: AppConfig) -> ConnectionResult:
        progress = lambda message: self._emit(  # noqa: E731
            context,
            MonitorEvent("reconnecting", message, "warning"),
        )
        try:
            client = self.client_factory()
            initial = client.ensure_connected(config, progress_callback=progress)
        except Exception as exc:
            # 网络/解析异常是本轮失败，不是监控线程退出的理由。
            initial = self._exception_result(config, exc)
        if (
            initial.connected
            or initial.status == InternetStatus.DEGRADED
            or initial.auth_status == AuthStatus.REJECTED
        ):
            return initial
        return self._recover_and_retry(context, config, initial, progress)

    def _recover_and_retry(
        self,
        context: _RunContext,
        config: AppConfig,
        initial: ConnectionResult,
        progress,
    ) -> ConnectionResult:
        from .wifi import recover_wifi

        latest = initial
        for attempt in range(1, RECOVERY_ATTEMPTS + 1):
            if context.stop_event.is_set():
                return latest
            link_changed = False
            if config.wifi_ssid:
                try:
                    wifi = recover_wifi(
                        config.wifi_ssid,
                        progress_callback=progress,
                        sleeper=lambda seconds: self._pause(context, seconds),
                        cancel_check=context.stop_event.is_set,
                    )
                    link_changed = wifi.changed
                except Exception as exc:
                    progress(f"Wi-Fi 恢复过程出错：{self._brief_error(exc)}")

            self._pause(context, RECOVERY_SETTLE_SECONDS)
            if context.stop_event.is_set():
                return latest
            client = None
            try:
                client = self.client_factory()
                if latest.auth_succeeded and not link_changed:
                    # 链路未重建时重复登录没有意义，只重新验证公共互联网。
                    verified = client.check_connection(config)
                    current = replace(
                        verified,
                        auth_status=AuthStatus.ACCEPTED,
                        auth_client_ip=latest.auth_client_ip,
                    )
                else:
                    current = client.ensure_connected(
                        config,
                        progress_callback=progress,
                    )
            except Exception as exc:
                current = replace(
                    latest,
                    message=f"恢复重试仍失败：{self._brief_error(exc)}",
                )
            latest = self._merge_results(latest, current)
            if (
                latest.connected
                or latest.status == InternetStatus.DEGRADED
                or latest.auth_status == AuthStatus.REJECTED
            ):
                return latest
            if attempt < RECOVERY_ATTEMPTS:
                progress(f"公共互联网尚未恢复，{RECOVERY_RETRY_DELAY:g} 秒后再重试…")
                self._pause(context, RECOVERY_RETRY_DELAY)
        return latest

    def _exception_result(
        self,
        config: AppConfig,
        exc: BaseException,
    ) -> ConnectionResult:
        message = f"检测或恢复异常：{self._brief_error(exc)}"
        return ConnectionResult(
            InternetStatus.UNKNOWN,
            message,
            direct_report=(
                unavailable_path("direct", exc) if config.direct_mode else None
            ),
            system_report=unavailable_path("system", exc),
        )

    @staticmethod
    def _merge_results(
        previous: ConnectionResult,
        current: ConnectionResult,
    ) -> ConnectionResult:
        auth_status = current.auth_status
        if previous.auth_succeeded or current.auth_succeeded:
            auth_status = AuthStatus.ACCEPTED
        recovered = current.connected and not previous.connected
        message = current.message
        if recovered:
            message = "互联网已恢复（新一轮严格公网验证通过）"
        return replace(
            current,
            message=message,
            auth_status=auth_status,
            auth_client_ip=current.auth_client_ip or previous.auth_client_ip,
            recovered=recovered or current.recovered,
        )

    def _handle_auth_deadlock(
        self,
        context: _RunContext,
        config: AppConfig,
        result: ConnectionResult,
    ) -> ConnectionResult:
        fingerprint = (
            config.username,
            config.password,
            config.zone,
            config.direct_mode,
            config.wifi_ssid,
        )
        if fingerprint != self._deadlock_context:
            self._deadlock_context = fingerprint
            self._auth_deadlock = 0

        system_online = bool(
            result.system_report
            and result.system_report.status == InternetStatus.ONLINE
        )
        if (
            result.auth_succeeded
            and not result.connected
            and result.status in (InternetStatus.BLOCKED, InternetStatus.UNKNOWN)
            and not system_online
        ):
            self._auth_deadlock += 1
        else:
            self._auth_deadlock = 0

        if (
            self._auth_deadlock < AUTH_DEADLOCK_ESCALATE_AFTER
            or not config.wifi_ssid
            or context.stop_event.is_set()
        ):
            return result

        self._emit(
            context,
            MonitorEvent(
                "reconnecting",
                f"已连续 {AUTH_DEADLOCK_ESCALATE_AFTER} 轮认证已接受但公共互联网不放行，"
                "正在重置 Wi-Fi 接口；重置后仍需重新验证…",
                "warning",
            ),
        )
        from .wifi import reset_wifi_interface

        try:
            reset = reset_wifi_interface(
                ssid=config.wifi_ssid,
                progress_callback=lambda message: self._emit(
                    context,
                    MonitorEvent("reconnecting", message, "warning"),
                ),
                sleeper=lambda seconds: self._pause(context, seconds),
                cancel_check=context.stop_event.is_set,
            )
        except Exception as exc:
            self._auth_deadlock = 0
            return replace(result, message=f"接口重置失败：{self._brief_error(exc)}")
        self._auth_deadlock = 0
        if not reset.changed or context.stop_event.is_set():
            return replace(
                result,
                message=result.message + f"；接口重置未完成：{reset.message}",
            )

        # 接口重置不是结果。立即创建新客户端并重新执行完整认证/验证。
        try:
            client = self.client_factory()
            verified = client.ensure_connected(
                config,
                progress_callback=lambda message: self._emit(
                    context,
                    MonitorEvent("reconnecting", message, "warning"),
                ),
            )
        except Exception as exc:
            return self._merge_results(
                result,
                replace(
                    result,
                    message=f"接口重置后的互联网验证失败：{self._brief_error(exc)}",
                ),
            )
        return self._merge_results(result, verified)

    @staticmethod
    def _result_event(result: ConnectionResult) -> MonitorEvent:
        if result.connected:
            state, level = "online", "success"
        elif result.status == InternetStatus.DEGRADED:
            state, level = "degraded", "warning"
        else:
            state, level = "offline", "warning"
        details = result.diagnostics
        if result.auth_client_ip:
            details += (f"校园认证客户端 IP：{result.auth_client_ip}",)
        return MonitorEvent(
            state,
            result.message,
            level,
            result.latency_ms,
            details=details,
            recovered=result.recovered,
        )

    @staticmethod
    def _pause(context: _RunContext, seconds: float) -> None:
        """可被停止或“立即检测”打断，但不并发启动第二轮。"""
        if seconds > 0:
            context.wake_event.wait(seconds)

    @staticmethod
    def _brief_error(exc: BaseException) -> str:
        text = str(exc)
        return text if len(text) <= 300 else text[:300] + "…"

    def _emit(
        self,
        context: _RunContext,
        event: MonitorEvent,
        *,
        force: bool = False,
    ) -> None:
        if context.stop_event.is_set() and not force:
            return
        self.event_callback(replace(event, generation=context.generation))
