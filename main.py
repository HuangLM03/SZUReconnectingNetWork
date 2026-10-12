from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from szu_guardian import __version__
from szu_guardian.storage import ConfigStore


SUPPORTED_PLATFORMS = ("darwin", "win32")


def enable_windows_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SZU 校园网自动重连工具（Windows / macOS）"
    )
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="由开机启动项（LaunchAgent / 注册表 Run 键）调用；启动后自动开始监控",
    )
    parser.add_argument(
        "--settings",
        action="store_true",
        help="仅打开设置窗口（一般由托盘 / 菜单栏应用自动调用）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="执行一轮完整的检测与重连（无界面，用于调试）",
    )
    parser.add_argument(
        "--detect",
        action="store_true",
        help="仅检测外网连通性，不做认证（无界面，用于调试）",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args()


def run_headless_check(store: ConfigStore, detect_only: bool) -> int:
    from szu_guardian.network import NetworkClient

    config = store.load()
    client = NetworkClient()

    if detect_only:
        result = client.check_connection()
        print(f"{'在线' if result.connected else '离线'}：{result.message}"
              + (f"（{result.latency_ms} ms）" if result.latency_ms is not None else ""))
        return 0 if result.connected else 1

    if not config.username or not config.password:
        print("尚未配置校园网账号：请先启动守护应用并完成设置", file=sys.stderr)
        return 2

    print(f"账号：{config.username} · 区域：{config.zone} · 间隔：{config.interval_minutes} 分钟")

    def progress(message: str) -> None:
        print(f"  · {message}")

    result = client.ensure_connected(config, progress_callback=progress)
    print(json.dumps({
        "connected": result.connected,
        "message": result.message,
        "latency_ms": result.latency_ms,
    }, ensure_ascii=False))
    return 0 if result.connected else 1


def run_desktop_app(store: ConfigStore, launched_by_autostart: bool) -> int:
    from szu_guardian.service import GuardianService

    service = GuardianService(store, launched_by_autostart=launched_by_autostart)
    if sys.platform == "win32":
        from szu_guardian.wintray import GuardianTrayApp

        GuardianTrayApp(service).run()
    else:
        from szu_guardian.menubar import GuardianMenuBarApp

        GuardianMenuBarApp(service).run()
    return 0


def main() -> int:
    if sys.platform not in SUPPORTED_PLATFORMS:
        print("本工具支持 Windows 与 macOS。", file=sys.stderr)
        return 2

    args = parse_args()
    store = ConfigStore()
    enable_windows_dpi_awareness()

    if args.settings:
        from settings_window import main as settings_main

        return settings_main()
    if args.check or args.detect:
        return run_headless_check(store, detect_only=args.detect)

    return run_desktop_app(store, launched_by_autostart=args.autostart)


if __name__ == "__main__":
    raise SystemExit(main())
