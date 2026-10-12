"""Windows 系统托盘前端（pystray + PIL）。业务逻辑全部在 GuardianService 中。

仅在 Windows 上导入（依赖 pystray / Pillow，均为 win32 环境依赖）。
菜单项文本用 callable 动态生成，轮询线程刷新状态与通知。
"""

from __future__ import annotations

import threading

import pystray
from PIL import Image, ImageDraw

from .service import GuardianService


STATE_LABELS = {
    "online": "网络正常",
    "checking": "正在检测",
    "reconnecting": "正在重连",
    "offline": "网络异常",
    "stopped": "已停止",
}

# 图标颜色只反映健康语义；waiting 不是独立的视觉状态
#（由 service.display_state 回落到最近一次检测结论）。
STATE_COLORS = {
    "online": "#16A34A",
    "checking": "#2563EB",
    "reconnecting": "#D97706",
    "offline": "#DC2626",
    "stopped": "#94A3B8",
}


def _icon_image(color: str = "#2563EB") -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((5, 5, 59, 59), radius=15, fill=color)
    draw.arc((15, 14, 49, 45), 205, 335, fill="white", width=5)
    draw.arc((21, 22, 43, 44), 205, 335, fill="white", width=5)
    draw.ellipse((29, 39, 35, 45), fill="white")
    return image


class GuardianTrayApp:
    def __init__(self, service: GuardianService):
        self.service = service
        self.service.notify = self._notify
        self._stopped = threading.Event()

        self.icon = pystray.Icon(
            "szu_network_guardian",
            _icon_image(),
            "SZU 网络守护",
            menu=pystray.Menu(
                pystray.MenuItem(self._status_text, None),
                pystray.MenuItem(self._toggle_text, self._on_toggle),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("立即检测", self._on_check),
                pystray.MenuItem("设置…", self._on_settings, default=True),
                pystray.MenuItem("打开日志目录", self._on_logs),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "开机自动启动",
                    self._on_autostart,
                    checked=lambda _item: self.service.autostart_enabled(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出守护", self._on_quit),
            ),
        )
        self._poller = threading.Thread(
            target=self._poll, name="tray-poller", daemon=True
        )

    # ------------------------------------------------------------------
    # 动态文本（pystray 支持 callable 菜单文本）

    def _status_text(self, _sender=None) -> str:
        text = STATE_LABELS.get(self.service.display_state, "运行中")
        event = self.service.last_event
        if event is not None and event.latency_ms is not None:
            text = f"{text} · {event.latency_ms} ms"
        return f"状态：{text}"

    def _toggle_text(self, _sender=None) -> str:
        return "暂停守护" if self.service.monitoring else "开始守护"

    def _notify(self, message: str) -> None:
        try:
            self.icon.notify(message, "SZU 网络守护")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 轮询：消费服务事件并刷新托盘

    def _poll(self) -> None:
        while not self._stopped.is_set():
            events = self.service.drain_events()
            if events:
                # 图标颜色由持久化检测结论决定：waiting 不会把失败状态冲成绿色
                self.icon.icon = _icon_image(
                    STATE_COLORS.get(self.service.display_state, "#2563EB")
                )
                self.icon.title = f"SZU 网络守护 - {self._status_text()[3:]}"
                try:
                    self.icon.update_menu()
                except Exception:
                    pass
            for action in self.service.drain_ui_actions():
                if action == "settings_closed":
                    self.service.on_settings_closed()
                    try:
                        self.icon.update_menu()
                    except Exception:
                        pass
            self._stopped.wait(0.5)

    # ------------------------------------------------------------------
    # 菜单动作

    def _on_toggle(self, icon, _item):
        if self.service.monitoring:
            self.service.stop_monitoring()
        else:
            self.service.start_monitoring()

    def _on_check(self, _icon, _item):
        self.service.check_now()

    def _on_settings(self, _icon, _item):
        self.service.open_settings()

    def _on_logs(self, _icon, _item):
        self.service.open_logs()

    def _on_autostart(self, _icon, _item):
        self.service.toggle_autostart()

    def _on_quit(self, _icon, _item):
        self.service.quit()
        self._stopped.set()
        self.icon.stop()

    # ------------------------------------------------------------------

    def run(self) -> None:
        self.service.local_log.write("程序已启动（Windows 托盘模式）")
        self._poller.start()
        threading.Timer(0.2, self.service.handle_initial_start).start()
        self.icon.run()  # 阻塞直到 icon.stop()
