import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from szu_guardian.monitor import MonitorEvent
from szu_guardian.service import GuardianService
from szu_guardian.storage import ConfigStore


def make_service(directory):
    with patch.dict(os.environ, {"SZU_GUARDIAN_DATA_DIR": directory}):
        return GuardianService(ConfigStore(Path(directory) / "config.json"))


class DisplayStateTests(unittest.TestCase):
    def test_waiting_preserves_offline_not_green(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)
            service.events.put(MonitorEvent("offline", "公网仍不可用", "warning"))
            service.events.put(MonitorEvent("waiting", "下次检测"))
            service.drain_events()
            self.assertEqual(service.display_state, "offline")

    def test_degraded_is_not_online_and_cannot_send_recovery_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)
            notified = []
            service.notify = notified.append
            service.events.put(MonitorEvent("offline", "断网"))
            service.events.put(MonitorEvent("degraded", "仅直连通", "warning"))
            service.drain_events()
            self.assertEqual(service.display_state, "degraded")
            self.assertEqual(notified, [])

    def test_only_fresh_online_after_offline_notifies_once(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)
            notified = []
            service.notify = notified.append
            for event in (
                MonitorEvent("offline", "断网"),
                MonitorEvent("online", "互联网已恢复", "success"),
                MonitorEvent("online", "互联网连接正常", "success"),
            ):
                service.events.put(event)
            service.drain_events()
            self.assertEqual(notified, ["互联网已恢复"])

    def test_successful_recovery_in_same_cycle_notifies(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)
            notified = []
            service.notify = notified.append
            service.events.put(
                MonitorEvent("online", "互联网已恢复", "success", recovered=True)
            )
            service.drain_events()
            self.assertEqual(notified, ["互联网已恢复"])

    def test_stopped_clears_health_and_pending_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)
            service.events.put(MonitorEvent("online", "在线", "success"))
            service.events.put(MonitorEvent("stopped", "停止"))
            service.drain_events()
            self.assertEqual(service.display_state, "stopped")
            self.assertIsNone(service.last_health_event)

    def test_old_generation_stopped_cannot_clear_new_health(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)
            service.events.put(MonitorEvent("started", "启动", generation=2))
            service.events.put(MonitorEvent("online", "在线", "success", generation=2))
            service.events.put(MonitorEvent("stopped", "旧线程退出", generation=1))
            service.drain_events()
            self.assertEqual(service.display_state, "online")

    def test_waiting_label_includes_observation_time_not_live_guarantee(self):
        with tempfile.TemporaryDirectory() as directory:
            service = make_service(directory)
            service.events.put(MonitorEvent("online", "在线", "success", observed_at=0))
            service.events.put(MonitorEvent("waiting", "下次检测"))
            service.drain_events()
            self.assertIn("上次检测互联网正常", service.display_label)
            self.assertIn("08:00:00", service.display_label)


if __name__ == "__main__":
    unittest.main()
