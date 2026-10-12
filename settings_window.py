"""SZU 网络守护 · 设置窗口（独立进程，Windows / macOS 通用）。

守护应用通过 subprocess 打开本脚本；保存后写入配置文件并按平台保护密码
（macOS 钥匙串 / Windows DPAPI），监控线程会在下一轮检测前重新读取配置，
无需重启守护。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tkinter as tk
from tkinter import messagebox, ttk

from szu_guardian.models import ZONE_AUTO, ZONE_DORMITORY, ZONE_OFFICE
from szu_guardian.startup import is_autostart_enabled, set_autostart
from szu_guardian.storage import ConfigStore


UI_FONT_FAMILY = (
    "Microsoft YaHei UI" if sys.platform == "win32" else "PingFang SC"
)
UI_FONT = (UI_FONT_FAMILY, 11)
UI_FONT_SMALL = (UI_FONT_FAMILY, 10)
UI_FONT_TITLE = (UI_FONT_FAMILY, 20, "bold")
UI_FONT_SECTION = (UI_FONT_FAMILY, 12, "bold")
UI_FONT_BUTTON = (UI_FONT_FAMILY, 11, "bold")

SECRET_HINT = (
    "密码使用 Windows DPAPI 加密保存在本机"
    if sys.platform == "win32"
    else "密码保存在 macOS 钥匙串中"
)
LOG_LOCATION_HINT = (
    "%LOCALAPPDATA%/SZUNetworkGuardian/logs"
    if sys.platform == "win32"
    else "应用程序支持/SZUNetworkGuardian/logs"
)

COLORS = {
    "background": "#F3F6FB",
    "card": "#FFFFFF",
    "text": "#172033",
    "muted": "#6B778C",
    "primary": "#2563EB",
    "primary_hover": "#1D4ED8",
    "border": "#DCE3EE",
}

ZONE_LABELS = {
    "教学 / 办公区（实验室）": ZONE_OFFICE,
    "宿舍区": ZONE_DORMITORY,
    "自动尝试（实验性）": ZONE_AUTO,
}
ZONE_NAMES = {value: key for key, value in ZONE_LABELS.items()}


class SettingsWindow:
    def __init__(self, store: ConfigStore):
        self.store = store
        self.config = store.load()

        self.root = tk.Tk()
        self.root.title("SZU 网络守护 · 设置")
        self.root.geometry("620x520")
        self.root.minsize(560, 480)
        self.root.configure(bg=COLORS["background"])
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        self.username_var = tk.StringVar(value=self.config.username)
        self.password_var = tk.StringVar(value=self.config.password)
        self.zone_var = tk.StringVar(
            value=ZONE_NAMES.get(self.config.zone, "教学 / 办公区（实验室）")
        )
        self.interval_var = tk.StringVar(value=str(self.config.interval_minutes))
        self.wifi_ssid_var = tk.StringVar(value=self.config.wifi_ssid)
        self.autostart_var = tk.BooleanVar(
            value=is_autostart_enabled() or self.config.autostart
        )
        self.start_on_launch_var = tk.BooleanVar(value=self.config.start_on_launch)
        self.direct_mode_var = tk.BooleanVar(value=self.config.direct_mode)
        self.password_visible = False

        self._configure_styles()
        self._build_ui()

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TFrame", background=COLORS["background"])
        style.configure("Card.TFrame", background=COLORS["card"])
        style.configure("TLabel", background=COLORS["background"], font=UI_FONT)
        style.configure(
            "Card.TLabel", background=COLORS["card"], foreground=COLORS["text"], font=UI_FONT,
        )
        style.configure(
            "Muted.Card.TLabel",
            background=COLORS["card"],
            foreground=COLORS["muted"],
            font=UI_FONT_SMALL,
        )
        style.configure(
            "Title.TLabel",
            background=COLORS["background"],
            foreground=COLORS["text"],
            font=UI_FONT_TITLE,
        )
        style.configure(
            "Subtitle.TLabel",
            background=COLORS["background"],
            foreground=COLORS["muted"],
            font=UI_FONT_SMALL,
        )
        style.configure(
            "Section.Card.TLabel",
            background=COLORS["card"],
            foreground=COLORS["text"],
            font=UI_FONT_SECTION,
        )
        style.configure(
            "TEntry",
            fieldbackground="#F8FAFC",
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            padding=(10, 8),
            font=UI_FONT,
        )
        style.configure(
            "TCombobox",
            fieldbackground="#F8FAFC",
            background="#F8FAFC",
            bordercolor=COLORS["border"],
            arrowsize=15,
            padding=(8, 7),
            font=UI_FONT,
        )
        style.configure(
            "TSpinbox",
            fieldbackground="#F8FAFC",
            padding=(8, 7),
            font=UI_FONT,
        )
        style.configure(
            "TCheckbutton",
            background=COLORS["card"],
            foreground=COLORS["text"],
            font=UI_FONT_SMALL,
        )
        style.map("TCheckbutton", background=[("active", COLORS["card"])])
        style.configure(
            "Primary.TButton",
            background=COLORS["primary"],
            foreground="#FFFFFF",
            borderwidth=0,
            padding=(18, 10),
            font=UI_FONT_BUTTON,
        )
        style.map(
            "Primary.TButton",
            background=[
                ("active", COLORS["primary_hover"]),
                ("disabled", "#A8B8D8"),
            ],
        )
        style.configure(
            "Secondary.TButton",
            background="#E9EFF9",
            foreground=COLORS["primary"],
            borderwidth=0,
            padding=(16, 10),
            font=UI_FONT,
        )
        style.map("Secondary.TButton", background=[("active", "#DCE7F8")])
        style.configure(
            "Ghost.TButton",
            background=COLORS["card"],
            foreground=COLORS["muted"],
            borderwidth=0,
            padding=(8, 7),
            font=UI_FONT_SMALL,
        )
        style.map("Ghost.TButton", background=[("active", "#F1F5F9")])

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(26, 20, 26, 16))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="SZU 网络守护", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="macOS 菜单栏将实时守护校园网连接 · 保存后自动生效，无需重启守护",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 14))

        form_card = ttk.Frame(outer, style="Card.TFrame", padding=(18, 16))
        form_card.pack(fill="x", pady=(0, 12))
        ttk.Label(form_card, text="连接设置", style="Section.Card.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )
        form_card.columnconfigure(1, weight=1)

        self._form_label(form_card, "账号", 1)
        ttk.Entry(form_card, textvariable=self.username_var).grid(
            row=1, column=1, columnspan=2, sticky="ew", pady=5
        )

        self._form_label(form_card, "密码", 2)
        self.password_entry = ttk.Entry(
            form_card, textvariable=self.password_var, show="●"
        )
        self.password_entry.grid(row=2, column=1, sticky="ew", pady=5)
        self.show_password_button = ttk.Button(
            form_card,
            text="显示",
            style="Ghost.TButton",
            command=self._toggle_password,
            width=6,
        )
        self.show_password_button.grid(row=2, column=2, padx=(6, 0), pady=5)

        self._form_label(form_card, "区域", 3)
        ttk.Combobox(
            form_card,
            textvariable=self.zone_var,
            values=list(ZONE_LABELS),
            state="readonly",
        ).grid(row=3, column=1, columnspan=2, sticky="ew", pady=5)

        self._form_label(form_card, "检测间隔", 4)
        interval_row = ttk.Frame(form_card, style="Card.TFrame")
        interval_row.grid(row=4, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Spinbox(
            interval_row,
            from_=1,
            to=1440,
            textvariable=self.interval_var,
            width=10,
        ).pack(side="left")
        ttk.Label(interval_row, text="分钟", style="Muted.Card.TLabel").pack(
            side="left", padx=(8, 0)
        )

        self._form_label(form_card, "Wi-Fi 重连", 5)
        wifi_cell = ttk.Frame(form_card, style="Card.TFrame")
        wifi_cell.grid(row=5, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Entry(wifi_cell, textvariable=self.wifi_ssid_var).pack(fill="x")
        ttk.Label(
            wifi_cell,
            text="断网时自动重新加入的 SSID（如 SZU_WLAN），留空则不管理 Wi-Fi",
            style="Muted.Card.TLabel",
        ).pack(anchor="w")

        options = ttk.Frame(form_card, style="Card.TFrame")
        options.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 2))
        ttk.Checkbutton(
            options, text="开机自动启动", variable=self.autostart_var
        ).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            options, text="程序启动后自动监控", variable=self.start_on_launch_var
        ).grid(row=0, column=1, sticky="w", padx=(18, 0))
        ttk.Checkbutton(
            options, text="检测和认证强制直连（推荐）", variable=self.direct_mode_var
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(2, 10))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        ttk.Button(
            buttons,
            text="保存设置",
            style="Primary.TButton",
            command=self._save,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(
            buttons,
            text="关闭",
            style="Secondary.TButton",
            command=self._close,
        ).grid(row=0, column=1, sticky="ew", padx=(5, 0))

        ttk.Label(
            outer,
            text=f"{SECRET_HINT} · 日志位于「{LOG_LOCATION_HINT}」",
            style="Subtitle.TLabel",
        ).pack(anchor="center")

    @staticmethod
    def _form_label(parent: ttk.Frame, text: str, row: int) -> None:
        ttk.Label(
            parent, text=text, style="Card.TLabel", width=10
        ).grid(row=row, column=0, sticky="w", pady=5)

    def _toggle_password(self) -> None:
        self.password_visible = not self.password_visible
        self.password_entry.configure(show="" if self.password_visible else "●")
        self.show_password_button.configure(
            text="隐藏" if self.password_visible else "显示"
        )

    def _read_form(self):
        try:
            interval = int(self.interval_var.get().strip())
        except ValueError:
            interval = 0
        return self.config.__class__(
            username=self.username_var.get(),
            password=self.password_var.get(),
            zone=ZONE_LABELS.get(self.zone_var.get(), ""),
            interval_minutes=interval,
            autostart=self.autostart_var.get(),
            start_on_launch=self.start_on_launch_var.get(),
            direct_mode=self.direct_mode_var.get(),
            wifi_ssid=self.wifi_ssid_var.get(),
        )

    def _save(self) -> None:
        try:
            config = self._read_form()
            config.validate()
            self.store.save(config)
            set_autostart(config.autostart)
            self.config = config
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法保存", str(exc), parent=self.root)
            return
        messagebox.showinfo(
            "已保存",
            "设置已保存；守护将在下一轮检测时自动生效。",
            parent=self.root,
        )

    def _close(self) -> None:
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    app = SettingsWindow(ConfigStore())
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
