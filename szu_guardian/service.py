"""GuardianService：双平台共享的桌面服务层。

监控生命周期、设置子进程管理、事件分发（本地日志 + 恢复通知）、
开机自启开关都收敛在这里；menubar（macOS）与 wintray（Windows）
只做 UI 映射并轮询 ``drain_events`` / ``drain_ui_actions``。
"""

from __future__ import annotations

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
        self._notified_offline = False
        # 显示状态跟踪：waiting 等事件不应冲掉上一次检测的结果颜色
        self._net_state: str | None = None  # 最近一次检测结论：online/offline
        self._transient_state: str | None = None  # checking/reconnecting
        # 前端注入：notify(message: str) -> None
        self.notify: Callable[[str], None] = lambda message: None

    # ------------------------------------------------------------------
    # 状态读取（供前端动态菜单文本）

    @property
    def last_event(self) -> MonitorEvent | None:
        return self._last_event

    @property
    def display_state(self) -> str:
        """前端应展示的状态：瞬时状态优先，否则回落到最近一次检测结论。"""
        if self._transient_state:
            return self._transient_state
        return self._net_state or "stopped"

    @property
    def monitoring(self) -> bool:
        return self.monitor.running

    def autostart_enabled(self) -> bool:
        try:
            return is_autostart_enabled()
        except OSError:
            return False

    # ------------------------------------------------------------------
    # 事件泵：前端定时调用；写本地日志并在恢复时发通知

    def drain_events(self) -> list[MonitorEvent]:
        events: list[MonitorEvent] = []
        try:
            while True:
                events.append(self.events.get_nowait())
        except queue.Empty:
            pass
        for event in events:
            self._last_event = event
            self.local_log.write(event.message, event.level)
            if event.state in ("online", "offline"):
                self._net_state = event.state
                self._transient_state = None
            elif event.state in ("checking", "reconnecting"):
                self._transient_state = event.state
            elif event.state == "stopped":
                self._net_state = None
                self._transient_state = None
            else:
                # waiting 等「空档」事件：清掉瞬时状态，回落到最近一次检测结论，
                # 但不改变 online/offline 的结论颜色
                self._transient_state = None

            if event.state == "online":
                if self._notified_offline:
                    self._notified_offline = False
                    self._notify_user(event.message)
            elif event.state == "offline":
                self._notified_offline = True
        return events

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

    # ------------------------------------------------------------------
    # 监控控制

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
            self.local_log.write("用户暂停了守护")

    def check_now(self) -> None:
        if self.monitor.running:
            self.monitor.check_now()
            self.local_log.write("用户请求立即检测")
        else:
            self.start_monitoring()

    # ------------------------------------------------------------------
    # 设置子进程

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

    # ------------------------------------------------------------------
    # 其他动作

    def open_logs(self) -> None:
        try:
            self.local_log.open_directory()
        except (OSError, subprocess.SubprocessError) as exc:
            self._notify_user(f"无法打开日志目录：{exc}")

    def toggle_autostart(self) -> bool:
        target = not self.autostart_enabled()
        try:
            set_autostart(target)
        except OSError as exc:
            self._notify_user(f"设置开机自启失败：{exc}")
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
