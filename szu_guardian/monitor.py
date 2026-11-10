from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from .models import AppConfig
from .network import NetworkClient


# 失败周期后的复查间隔上限（分钟）
FAILED_CYCLE_RETRY_MINUTES = 5
# Wi-Fi 链路重建后、首次恢复重试前的稳定等待（秒）
RECOVERY_SETTLE_SECONDS = 5
# 恢复重试总次数与两次之间的间隔（秒）
RECOVERY_ATTEMPTS = 2
RECOVERY_RETRY_DELAY = 15
# 连续多少轮「认证成功但外网不恢复」后自动重置网络接口。
# 该僵局卡在 DHCP 绑定 / 网关放行层，重复认证无法解开；
# 按失败复查节奏（5 分钟）计，6 轮 ≈ 30 分钟升级一次
AUTH_DEADLOCK_ESCALATE_AFTER = 6


@dataclass(frozen=True, slots=True)
class MonitorEvent:
    state: str
    message: str
    level: str = "info"
    latency_ms: int | None = None


class MonitorController:
    """Background thread that keeps the campus network connected.

    与 Windows 原版不同，监控线程每一轮都会通过 ``config_provider``
    重新读取配置：设置窗口是独立进程，保存后无需重启守护即可生效。
    """

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
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._auth_deadlock = 0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def auth_deadlock_count(self) -> int:
        """当前连续「认证成功但外网不恢复」的轮数（诊断用）。"""
        return self._auth_deadlock

    def start(self) -> bool:
        with self._lock:
            if self.running:
                self._wake_event.set()
                return True
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="szu-network-monitor",
                daemon=True,
            )
            self._thread.start()
            return True

    def check_now(self) -> None:
        if self.running:
            self._wake_event.set()

    def restart(self) -> None:
        """Stop and start again so a fresh client picks up new settings."""
        self.stop()
        self.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            if thread is threading.current_thread():
                # 从监控线程内部请求停止（如测试或回调）：直接退出循环即可
                return
            thread.join(timeout=2)
        else:
            self.event_callback(MonitorEvent("stopped", "监控已停止"))

    def _run_loop(self) -> None:
        self._emit(MonitorEvent("checking", "正在检测网络状态…"))

        while not self._stop_event.is_set():
            self._wake_event.clear()
            config = self.config_provider()
            interval_minutes = config.interval_minutes
            # 每轮使用全新客户端：Wi-Fi 重连后本机 IP 可能变化，
            # 直连会话若绑定了旧源地址会持续失败
            client = self.client_factory()
            progress = lambda message: self._emit(  # noqa: E731
                MonitorEvent("reconnecting", message, "warning")
            )
            result = None
            try:
                result = client.ensure_connected(config, progress_callback=progress)
            except Exception as exc:
                # 常见如 Errno 49（源地址/路由失效）、DNS 失败等；同样进入恢复重试
                self._emit(
                    MonitorEvent(
                        "offline",
                        f"检测或重连失败：{self._brief_error(exc)}",
                        "error",
                    )
                )

            if result is not None and result.connected:
                self._emit(
                    MonitorEvent(
                        "online",
                        result.message,
                        "success",
                        result.latency_ms,
                    )
                )
            else:
                # 检测失败或过程异常：恢复 Wi-Fi 链路（如已配置）后，
                # 换新客户端立即重试一轮，而不是干等下个周期
                result = self._recover_and_retry(config, progress)
                if result.connected:
                    self._emit(
                        MonitorEvent(
                            "online",
                            result.message,
                            "success",
                            result.latency_ms,
                        )
                    )
                else:
                    self._emit(MonitorEvent("offline", result.message, "warning"))

            if self._stop_event.is_set():
                break

            # 僵局识别：连续 N 轮「认证成功但外网不恢复」说明卡住的
            # 是链路绑定层而非认证层 —— 升级为重置网络接口
            if result.connected:
                self._auth_deadlock = 0
            elif result.auth_succeeded:
                self._auth_deadlock += 1
            else:
                self._auth_deadlock = 0
            if self._auth_deadlock >= AUTH_DEADLOCK_ESCALATE_AFTER:
                self._escalate_auth_deadlock(config, progress)
                self._auth_deadlock = 0

            if result is not None and result.connected:
                wait_minutes = interval_minutes
            else:
                # 上一轮失败：最多 5 分钟后复查，而不是干等完整间隔
                wait_minutes = min(interval_minutes, FAILED_CYCLE_RETRY_MINUTES)
            self._emit(
                MonitorEvent(
                    "waiting",
                    f"下次检测将在 {wait_minutes} 分钟内进行",
                )
            )
            self._wake_event.wait(wait_minutes * 60)
            if not self._stop_event.is_set():
                self._emit(MonitorEvent("checking", "正在检测网络状态…"))
        self._emit(MonitorEvent("stopped", "监控已停止"))

    def _pause(self, seconds: float) -> None:
        """可被停止信号打断的等待。"""
        self._stop_event.wait(seconds)

    @staticmethod
    def _brief_error(exc: BaseException) -> str:
        text = str(exc)
        return text if len(text) <= 300 else text[:300] + "…"

    def _recover_and_retry(self, config: AppConfig, progress):
        from .network import ConnectionResult

        if config.wifi_ssid:
            from .wifi import recover_wifi

            try:
                recover_wifi(config.wifi_ssid, progress_callback=progress)
            except Exception as exc:
                self._emit(
                    MonitorEvent(
                        "reconnecting",
                        f"Wi-Fi 恢复过程出错：{self._brief_error(exc)}",
                        "warning",
                    )
                )

        # 链路刚重建时 DHCP 虽已拿到地址，但 DNS / 路由 / 门户通路
        # 可能还要几秒才就绪，立即重试容易误报失败
        self._pause(RECOVERY_SETTLE_SECONDS)

        last_error = "恢复重试仍失败；请查看本地日志"
        auth_ok = False
        for attempt in range(1, RECOVERY_ATTEMPTS + 1):
            if self._stop_event.is_set():
                return ConnectionResult(False, "监控已停止")
            client = self.client_factory()
            try:
                result = client.ensure_connected(config, progress_callback=progress)
                if result.connected:
                    return result
                if result.auth_succeeded:
                    auth_ok = True
                last_error = result.message
            except Exception as exc:
                last_error = f"恢复重试仍失败：{self._brief_error(exc)}"
            if attempt < RECOVERY_ATTEMPTS:
                progress(
                    f"恢复未成功，{RECOVERY_RETRY_DELAY:g} 秒后再重试一轮…"
                )
                self._pause(RECOVERY_RETRY_DELAY)
        return ConnectionResult(False, last_error, auth_succeeded=auth_ok)

    def _escalate_auth_deadlock(self, config: AppConfig, progress) -> None:
        from .wifi import reset_wifi_interface

        self._emit(
            MonitorEvent(
                "reconnecting",
                f"已连续 {AUTH_DEADLOCK_ESCALATE_AFTER} 轮认证成功但外网不放行，"
                "正在重置 Wi-Fi 接口以重建链路绑定（等价于手动断开重连）…",
                "warning",
            )
        )
        try:
            reset_wifi_interface(
                ssid=config.wifi_ssid,
                progress_callback=progress,
                sleeper=self._pause,
            )
        except Exception as exc:
            self._emit(
                MonitorEvent(
                    "reconnecting",
                    f"重置网络接口出错：{self._brief_error(exc)}",
                    "warning",
                )
            )

    def _emit(self, event: MonitorEvent) -> None:
        self.event_callback(event)
