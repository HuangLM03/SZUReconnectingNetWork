"""双平台共享桌面服务：事件、日志、通知与监控生命周期。"""

from __future__ import annotations

import datetime as dt
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from .local_log import LocalLog
from .monitor import MonitorController, MonitorEvent
from .startup import is_autostart_enabled, set_autostart
from .storage import ConfigStore


class GuardianService:
    def __init__(self, store: ConfigStore, launched_by_autostart: bool = False):
        self.store = store
        self.launched_by_autostart = launched_by_autostart
        self.config = store.load()
        self.events: queue.Queue[MonitorEvent] = queue.Queue()
        self.ui_actions: queue.Queue[str] = queue.Queue()
        self.monitor = MonitorController(
            self.events.put,
            config_provider=store.load,
        )
        self.local_log = LocalLog()
        self._settings_process: subprocess.Popen | None = None
        self._last_event: MonitorEvent | None = None
        self._last_health_event: MonitorEvent | None = None
        self._notified_offline = False
        self._generation = 0
        self._net_state: str | None = None
        self._transient_state: str | None = None
        self.notify: Callable[[str], None] = lambda message: None

    @property
    def last_event(self) -> MonitorEvent | None:
        return self._last_event

    @property
    def last_health_event(self) -> MonitorEvent | None:
        return self._last_health_event

    @property
    def display_state(self) -> str:
        if self._transient_state:
            return self._transient_state
        return self._net_state or "stopped"

    @property
    def display_label(self) -> str:
        labels = {
            "online": "上次检测互联网正常",
            "degraded": "连接受限，未完全恢复",
            "offline": "互联网未通过检测",
            "checking": "正在严格检测",
            "reconnecting": "正在恢复与复查",
            "stopped": "已停止",
        }
        text = labels.get(self.display_state, "运行中")
        health = self._last_health_event
        if health and self.display_state in ("online", "offline", "degraded"):
            text += dt.datetime.fromtimestamp(health.observed_at).strftime(" · %H:%M:%S")
        if health and health.latency_ms is not None:
            text += f" · {health.latency_ms} ms"
        return text

    @property
    def monitoring(self) -> bool:
        return self.monitor.running

    def autostart_enabled(self) -> bool:
        try:
            return is_autostart_enabled()
        except OSError:
            return False

    def drain_events(self) -> list[MonitorEvent]:
        events: list[MonitorEvent] = []
        try:
            while True:
                events.append(self.events.get_nowait())
        except queue.Empty:
            pass

        processed: list[MonitorEvent] = []
        for event in events:
            # 旧代次的 stopped/offline 不得覆盖刚启动的新代次。
            if event.generation and event.generation < self._generation:
                continue
            if event.generation > self._generation:
                self._generation = event.generation
            processed.append(event)
            self._last_event = event
            self.local_log.write(
                event.message,
                event.level,
                observed_at=event.observed_at,
            )
            for detail in event.details:
                self.local_log.write(
                    f"  诊断 · {detail}",
                    "info",
                    observed_at=event.observed_at,
                )

            if event.state == "started":
                self._net_state = None
                self._transient_state = None
                self._last_health_event = None
                self._notified_offline = False
            elif event.state in ("online", "offline", "degraded"):
                self._net_state = event.state
                self._transient_state = None
                self._last_health_event = event
            elif event.state in ("checking", "reconnecting"):
                self._transient_state = event.state
            elif event.state == "stopped":
                self._net_state = None
                self._transient_state = None
                self._last_health_event = None
                self._notified_offline = False
            else:
                # waiting 只清瞬时状态，健康颜色仍代表上次结论及其时间。
                self._transient_state = None

            if event.state == "online":
                should_notify = self._notified_offline or event.recovered
                self._notified_offline = False
                if should_notify:
                    self._notify_user(event.message)
            elif event.state in ("offline", "degraded"):
                self._notified_offline = True
        return processed

    def drain_ui_actions(self) -> list[str]:
        actions: list[str] = []
        try:
            while True:
                actions.append(self.ui_actions.get_nowait())
        except queue.Empty:
            pass
        return actions

    def _notify_user(self, message: str) -> None:
        try:
            self.notify(message)
        except Exception:
            pass

    def start_monitoring(self, silent: bool = False) -> None:
        config = self.store.load()
        if not config.username or not config.password:
            if not silent:
                self._notify_user("请先在设置中填写校园网账号和密码")
            self.open_settings()
            return
        self.monitor.start()
        self.local_log.write(
            f"开始监控，间隔 {config.interval_minutes} 分钟"
            + ("（开机自启）" if self.launched_by_autostart else "")
        )

    def stop_monitoring(self) -> None:
        if self.monitor.running:
            self.monitor.stop()
            self.local_log.write("用户请求暂停守护")

    def check_now(self) -> None:
        if self.monitor.running:
            self.monitor.check_now()
            self.local_log.write("用户请求立即检测")
        else:
            self.start_monitoring()

    def open_settings(self) -> None:
        if self._settings_process and self._settings_process.poll() is None:
            return
        script = Path(__file__).resolve().parents[1] / "settings_window.py"
        try:
            self._settings_process = subprocess.Popen([sys.executable, str(script)])
        except OSError as exc:
            self._notify_user(f"无法打开设置窗口：{exc}")
            return
        threading.Thread(
            target=self._watch_settings_process,
            name="settings-watcher",
            daemon=True,
        ).start()

    def _watch_settings_process(self) -> None:
        process = self._settings_process
        if process is not None:
            process.wait()
        self.ui_actions.put("settings_closed")

    def on_settings_closed(self) -> None:
        previous_running = self.monitor.running
        self.config = self.store.load()
        if self.config.username and self.config.password:
            if previous_running or self.config.start_on_launch:
                if self.monitor.running:
                    self.monitor.restart()
                else:
                    self.start_monitoring(silent=True)
        elif previous_running:
            self.monitor.stop()

    def open_logs(self) -> None:
        try:
            self.local_log.open_directory()
        except (OSError, subprocess.SubprocessError) as exc:
            self._notify_user(f"无法打开日志目录：{exc}")

    def toggle_autostart(self) -> bool:
        target = not self.autostart_enabled()
        try:
            set_autostart(target)
        except OSError:
            self._notify_user("设置开机自启失败")
            return not target
        self.local_log.write("已开启开机自启" if target else "已关闭开机自启")
        return target

    def handle_initial_start(self) -> None:
        should_start = self.launched_by_autostart or self.config.start_on_launch
        if not should_start:
            return
        if self.config.username and self.config.password:
            self.start_monitoring(silent=True)
        else:
            self._notify_user("首次使用：请填写校园网账号和密码")
            self.open_settings()

    def quit(self) -> None:
        self.local_log.write("程序退出")
        if self.monitor.running:
            self.monitor.stop()
