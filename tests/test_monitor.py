import unittest
from unittest.mock import Mock, patch

from szu_guardian.models import AppConfig
from szu_guardian.monitor import MonitorController
from szu_guardian.network import ConnectionResult


class MonitorControllerTests(unittest.TestCase):
    def test_rereads_config_each_cycle(self):
        configs = [
            AppConfig(username="u", password="p", interval_minutes=1),
            AppConfig(username="u", password="p", interval_minutes=2),
        ]
        seen: list[AppConfig] = []
        events: list[tuple[str, str]] = []

        controller = MonitorController(
            event_callback=lambda event: events.append((event.state, event.message)),
            config_provider=lambda: configs[0] if not seen else configs[1],
        )

        client = Mock()
        client.ensure_connected.side_effect = lambda config, progress_callback=None: (
            seen.append(config),
            controller.stop(),
            ConnectionResult(True, "ok"),
        )[2]

        with patch.object(controller, "client_factory", return_value=client):
            controller.start()
            controller._thread.join(timeout=3)

        self.assertEqual([c.interval_minutes for c in seen], [1])
        self.assertTrue(any(state == "online" for state, _ in events))
        self.assertTrue(any(state == "stopped" for state, _ in events))

    def test_second_start_wakes_existing_thread(self):
        import threading

        controller = MonitorController(
            event_callback=lambda _event: None,
            config_provider=lambda: AppConfig(username="u", password="p"),
        )
        entered_check = threading.Event()
        release_check = threading.Event()
        client = Mock()

        def hold_in_check(config, progress_callback=None):
            entered_check.set()
            release_check.wait(3)
            controller.stop()
            return ConnectionResult(True, "ok")

        client.ensure_connected.side_effect = hold_in_check

        with patch.object(controller, "client_factory", return_value=client) as factory:
            controller.start()
            first_thread = controller._thread
            self.assertIsNotNone(first_thread)
            self.assertTrue(entered_check.wait(3))

            controller.start()  # 线程存活时应唤醒，而不是再开线程

            self.assertIs(controller._thread, first_thread)
            release_check.set()
            first_thread.join(timeout=3)

        self.assertEqual(factory.call_count, 1)

    def test_wifi_recovery_runs_when_link_fails_and_ssid_configured(self):
        events: list[tuple[str, str]] = []
        controller = MonitorController(
            event_callback=lambda event: events.append((event.state, event.message)),
            config_provider=lambda: AppConfig(
                username="u", password="p", wifi_ssid="SZU_WLAN"
            ),
        )
        client = Mock()
        outcomes = [
            ConnectionResult(False, "直连外网不可用（超时）"),
            ConnectionResult(True, "已自动重连，网络恢复正常", 12),
        ]

        def check_then_stop(config, progress_callback=None):
            result = outcomes.pop(0)
            if not outcomes:
                controller.stop()
            return result

        client.ensure_connected.side_effect = check_then_stop

        with (
            patch.object(controller, "client_factory", return_value=client) as factory,
            patch("szu_guardian.wifi.recover_wifi", return_value=True) as recover,
            patch("szu_guardian.monitor.RECOVERY_SETTLE_SECONDS", 0),
            patch("szu_guardian.monitor.RECOVERY_RETRY_DELAY", 0),
        ):
            controller.start()
            controller._thread.join(timeout=3)

        self.assertEqual(factory.call_count, 2)  # 断链后换了新客户端
        self.assertEqual(client.ensure_connected.call_count, 2)
        recover.assert_called_once()
        self.assertTrue(any(state == "online" for state, _ in events))

    def test_no_wifi_recovery_without_ssid(self):
        events: list[tuple[str, str]] = []
        controller = MonitorController(
            event_callback=lambda event: events.append((event.state, event.message)),
            config_provider=lambda: AppConfig(username="u", password="p"),
        )
        client = Mock()
        client.ensure_connected.side_effect = lambda config, progress_callback=None: (
            controller.stop(),
            ConnectionResult(False, "直连外网不可用"),
        )[1]

        with (
            patch.object(controller, "client_factory", return_value=client),
            patch("szu_guardian.wifi.recover_wifi") as recover,
        ):
            controller.start()
            controller._thread.join(timeout=3)

        recover.assert_not_called()
        self.assertTrue(any(state == "offline" for state, _ in events))

    def test_exception_triggers_immediate_fresh_client_retry(self):
        """Errno 49 一类的过程异常也应立即重试，而不是等下个周期。"""
        import requests

        events: list[tuple[str, str]] = []
        controller = MonitorController(
            event_callback=lambda event: events.append((event.state, event.message)),
            config_provider=lambda: AppConfig(
                username="u", password="p", wifi_ssid="SZU_WLAN"
            ),
        )
        client = Mock()
        attempts: list[object] = []

        def fail_then_succeed(config, progress_callback=None):
            attempts.append(config)
            if len(attempts) == 1:
                raise requests.exceptions.ConnectionError(
                    "HTTPSConnectionPool: Failed to establish a new connection: "
                    "[Errno 49] Can't assign requested address"
                )
            controller.stop()
            return ConnectionResult(True, "已自动重连，网络恢复正常", 15)

        client.ensure_connected.side_effect = fail_then_succeed

        with (
            patch.object(controller, "client_factory", return_value=client) as factory,
            patch("szu_guardian.wifi.recover_wifi", return_value=True),
            patch("szu_guardian.monitor.RECOVERY_SETTLE_SECONDS", 0),
            patch("szu_guardian.monitor.RECOVERY_RETRY_DELAY", 0),
        ):
            controller.start()
            controller._thread.join(timeout=3)

        self.assertEqual(factory.call_count, 2)  # 异常后立即换了新客户端重试
        self.assertEqual(client.ensure_connected.call_count, 2)
        self.assertTrue(any(state == "offline" and "Errno 49" in message for state, message in events))
        self.assertTrue(any(state == "online" for state, _ in events))

    def test_failed_cycle_shortens_next_wait(self):
        """失败的周期后最多等 5 分钟复查，而不是完整间隔。"""
        import szu_guardian.monitor as monitor_module

        events: list[tuple[str, str]] = []
        waits: list[float] = []
        controller = MonitorController(
            event_callback=lambda event: events.append((event.state, event.message)),
            config_provider=lambda: AppConfig(
                username="u", password="p", interval_minutes=60
            ),
        )
        client = Mock()
        client.ensure_connected.return_value = ConnectionResult(False, "直连外网不可用")

        wake = controller._wake_event
        original_wait = wake.wait

        def capture_and_stop(timeout=None):
            waits.append(timeout)
            controller.stop()
            original_wait(timeout)

        wake.wait = capture_and_stop

        with (
            patch.object(controller, "client_factory", return_value=client),
            patch("szu_guardian.wifi.recover_wifi"),
            patch.object(monitor_module, "RECOVERY_SETTLE_SECONDS", 0),
            patch.object(monitor_module, "RECOVERY_RETRY_DELAY", 0),
            patch.object(monitor_module, "RECOVERY_ATTEMPTS", 1),
        ):
            controller.start()
            controller._thread.join(timeout=3)

        self.assertEqual(waits, [5 * 60])
        self.assertTrue(
            any(state == "waiting" and "5 分钟" in message for state, message in events)
        )

    def test_recovery_retries_twice_after_settle(self):
        """链路恢复后的重试：失败/异常一次后还会再试一轮。"""
        import szu_guardian.monitor as monitor_module

        events: list[tuple[str, str]] = []
        controller = MonitorController(
            event_callback=lambda event: events.append((event.state, event.message)),
            config_provider=lambda: AppConfig(
                username="u", password="p", wifi_ssid="SZU_WLAN", interval_minutes=1
            ),
        )
        client = Mock()
        outcomes = [
            ConnectionResult(False, "直连外网不可用"),
            OSError("无法通过物理网卡进行直连 DNS 查询"),
            ConnectionResult(True, "已自动重连，网络恢复正常", 20),
        ]

        def sequence(config, progress_callback=None):
            outcome = outcomes.pop(0)
            if not outcomes:
                controller.stop()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        client.ensure_connected.side_effect = sequence

        with (
            patch.object(controller, "client_factory", return_value=client) as factory,
            patch("szu_guardian.wifi.recover_wifi", return_value=True),
            patch.object(monitor_module, "RECOVERY_SETTLE_SECONDS", 0),
            patch.object(monitor_module, "RECOVERY_RETRY_DELAY", 0),
        ):
            controller.start()
            controller._thread.join(timeout=3)

        self.assertEqual(factory.call_count, 3)  # 初始 + 恢复重试两轮
        self.assertTrue(any(state == "online" for state, _ in events))
        self.assertTrue(
            any(
                state == "reconnecting" and "再重试一轮" in message
                for state, message in events
            )
        )


if __name__ == "__main__":
    unittest.main()
