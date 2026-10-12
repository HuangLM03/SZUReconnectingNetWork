import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from szu_guardian.monitor import MonitorEvent
from szu_guardian.service import GuardianService
from szu_guardian.storage import ConfigStore


def make_service(directory: str) -> GuardianService:
    with patch.dict(os.environ, {"SZU_GUARDIAN_DATA_DIR": directory}):
        return GuardianService(ConfigStore(Path(directory) / "config.json"))


class DisplayStateTests(unittest.TestCase):
    def test_waiting_after_failure_keeps_offline_display(self):
        """修复的 bug：失败的周期后，waiting 事件不应把状态冲成绿色。"""
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)

            service.events.put(MonitorEvent("offline", "恢复重试仍失败", "warning"))
            service.events.put(MonitorEvent("waiting", "下次检测将在 5 分钟内进行"))
            service.drain_events()

            self.assertEqual(service.display_state, "offline")

    def test_waiting_after_success_keeps_online_display(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)

            service.events.put(MonitorEvent("online", "网络连接正常", "success", 30))
            service.events.put(MonitorEvent("waiting", "下次检测将在 60 分钟内进行"))
            service.drain_events()

            self.assertEqual(service.display_state, "online")

    def test_transient_states_take_priority_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)

            service.events.put(MonitorEvent("offline", "网络异常", "warning"))
            service.events.put(MonitorEvent("reconnecting", "正在重连…"))
            service.drain_events()
            self.assertEqual(service.display_state, "reconnecting")

            service.events.put(MonitorEvent("checking", "正在检测网络状态…"))
            service.drain_events()
            self.assertEqual(service.display_state, "checking")

            # checking 之后回到最近一次检测结论（offline）
            service.events.put(MonitorEvent("waiting", "下次检测将在 5 分钟内进行"))
            service.drain_events()
            self.assertEqual(service.display_state, "offline")

    def test_stopped_resets_to_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)

            service.events.put(MonitorEvent("online", "ok", "success"))
            service.events.put(MonitorEvent("stopped", "监控已停止"))
            service.drain_events()

            self.assertEqual(service.display_state, "stopped")

    def test_recovery_notification_fires_once_on_offline_to_online(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)
            notified: list[str] = []
            service.notify = notified.append

            service.events.put(MonitorEvent("offline", "断网", "warning"))
            service.events.put(MonitorEvent("reconnecting", "重连中"))
            service.events.put(MonitorEvent("online", "已自动重连", "success"))
            service.events.put(MonitorEvent("waiting", "下次检测…"))
            service.events.put(MonitorEvent("online", "网络连接正常", "success"))
            service.drain_events()

            self.assertEqual(notified, ["已自动重连"])


if __name__ == "__main__":
    unittest.main()
