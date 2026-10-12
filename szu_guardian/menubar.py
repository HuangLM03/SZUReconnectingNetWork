"""macOS 菜单栏前端（rumps）。业务逻辑全部在 GuardianService 中。"""

from __future__ import annotations

import subprocess

import rumps

from .service import GuardianService


# 图标颜色只反映健康语义：online/offline 为持久结论，
# checking/reconnecting 为瞬时状态；waiting 不再是独立的视觉状态
#（由 service.display_state 回落到最近一次检测结论）。
STATE_TITLES = {
    "online": "🟢",
    "checking": "🟡",
    "reconnecting": "🟠",
    "offline": "🔴",
    "stopped": "⚪️",
}

STATE_LABELS = {
    "online": "网络正常",
    "checking": "正在检测",
    "reconnecting": "正在重连",
    "offline": "网络异常",
    "stopped": "已停止",
}


def notify_user(message: str) -> None:
    try:
        rumps.notification("SZU 网络守护", "", message)
        return
    except Exception:
        pass
    try:
        safe = message.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe}" with title "SZU 网络守护"',
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


class GuardianMenuBarApp(rumps.App):
    def __init__(self, service: GuardianService):
        super().__init__(
            "SZU 网络守护",
            title=STATE_TITLES["stopped"],
            quit_button=None,
        )
        self.service = service
        self.service.notify = notify_user

        self.status_item = rumps.MenuItem(self._status_text())
        self.toggle_item = rumps.MenuItem(
            self._toggle_text(), callback=self._toggle_monitoring
        )
        self.autostart_item = rumps.MenuItem(
            "开机自动启动",
            callback=self._toggle_autostart,
        )
        self.autostart_item.state = service.autostart_enabled()
        self.menu = [
            self.status_item,
            self.toggle_item,
            rumps.separator,
            rumps.MenuItem("立即检测", callback=self._check_now),
            rumps.MenuItem("设置…", callback=self._open_settings),
            rumps.MenuItem("打开日志目录", callback=self._open_logs),
            rumps.separator,
            self.autostart_item,
            rumps.separator,
            rumps.MenuItem("退出守护", callback=self._quit),
        ]

        self.event_timer = rumps.Timer(self._on_tick, 0.5)
        self.event_timer.start()
        self.service.local_log.write("程序已启动（macOS 菜单栏模式）")
        rumps.Timer(self._handle_initial_start, 0.2).start()

    # ------------------------------------------------------------------
    # 动态文本

    def _status_text(self) -> str:
        text = STATE_LABELS.get(self.service.display_state, "运行中")
        event = self.service.last_event
        if event is not None and event.latency_ms is not None:
            text = f"{text} · {event.latency_ms} ms"
        return f"状态：{text}"

    def _toggle_text(self) -> str:
        return "暂停守护" if self.service.monitoring else "开始守护"

    def _refresh_menu(self) -> None:
        self.status_item.title = self._status_text()
        self.toggle_item.title = self._toggle_text()

    # ------------------------------------------------------------------
    # 事件泵

    def _on_tick(self, _sender):
        events = self.service.drain_events()
        if events:
            # 图标颜色由持久化检测结论决定：waiting 不会把失败状态冲成绿色
            self.title = STATE_TITLES.get(self.service.display_state, "⚪️")
            self._refresh_menu()
        for action in self.service.drain_ui_actions():
            if action == "settings_closed":
                self.service.on_settings_closed()
                self.autostart_item.state = self.service.autostart_enabled()
                self._refresh_menu()

    def _handle_initial_start(self, sender):
        sender.stop()  # rumps.Timer 默认重复触发；首次启动逻辑只需执行一次
        self.service.handle_initial_start()
        self._refresh_menu()

    # ------------------------------------------------------------------
    # 菜单动作

    def _toggle_monitoring(self, _sender):
        if self.service.monitoring:
            self.service.stop_monitoring()
        else:
            self.service.start_monitoring()
        self._refresh_menu()

    def _check_now(self, _sender):
        self.service.check_now()

    def _open_settings(self, _sender=None):
        self.service.open_settings()

    def _open_logs(self, _sender):
        self.service.open_logs()

    def _toggle_autostart(self, _sender):
        self.autostart_item.state = self.service.toggle_autostart()

    def _quit(self, _sender):
        self.service.quit()
        rumps.quit()
