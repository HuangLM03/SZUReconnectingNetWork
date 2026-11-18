import threading
import unittest
from unittest.mock import Mock, patch

from szu_guardian.connectivity import InternetStatus, PathReport, ProbeResult
from szu_guardian.models import AppConfig
from szu_guardian.monitor import MonitorController, _RunContext
from szu_guardian.network import AuthStatus, ConnectionResult


def path_report(status, path="direct"):
    return PathReport(
        path=path,
        status=status,
        probes=(
            ProbeResult("A", "https://a.test", status == InternetStatus.ONLINE, "test"),
            ProbeResult("B", "https://b.test", status == InternetStatus.ONLINE, "test"),
        ),
        latency_ms=20,
    )


def result(status=InternetStatus.BLOCKED, auth=AuthStatus.NOT_ATTEMPTED):
    return ConnectionResult(
        status=status,
        message=status.value,
        direct_report=path_report(status),
        system_report=path_report(status, "system"),
        latency_ms=20,
        auth_status=auth,
        auth_client_ip="172.30.1.2" if auth == AuthStatus.ACCEPTED else "",
    )


def config(**kwargs):
    values = dict(username="u", password="p", interval_minutes=60)
    values.update(kwargs)
    return AppConfig(**values)


class RecoveryCycleTests(unittest.TestCase):
    def setUp(self):
        self.context = _RunContext(1)
        self.controller = MonitorController(lambda _event: None, config)

    def test_degraded_path_never_triggers_wifi_or_reauthentication(self):
        client = Mock()
        client.ensure_connected.return_value = result(InternetStatus.DEGRADED)
        self.controller.client_factory = Mock(return_value=client)

        with patch("szu_guardian.wifi.recover_wifi") as recover:
            final = self.controller._run_cycle(self.context, config(wifi_ssid="SZU_WLAN"))

        self.assertEqual(final.status, InternetStatus.DEGRADED)
        recover.assert_not_called()
        client.ensure_connected.assert_called_once()

    def test_auth_rejection_never_triggers_wifi_recovery(self):
        client = Mock()
        client.ensure_connected.return_value = result(auth=AuthStatus.REJECTED)
        self.controller.client_factory = Mock(return_value=client)

        with patch("szu_guardian.wifi.recover_wifi") as recover:
            final = self.controller._run_cycle(self.context, config(wifi_ssid="SZU_WLAN"))

        self.assertEqual(final.auth_status, AuthStatus.REJECTED)
        recover.assert_not_called()

    def test_initial_accepted_auth_survives_recovery_exception(self):
        first = Mock()
        first.ensure_connected.return_value = result(auth=AuthStatus.ACCEPTED)
        second = Mock()
        second.check_connection.side_effect = OSError("DNS disappeared")
        self.controller.client_factory = Mock(side_effect=[first, second])

        with (
            patch("szu_guardian.wifi.recover_wifi") as recover,
            patch("szu_guardian.monitor.RECOVERY_ATTEMPTS", 1),
            patch("szu_guardian.monitor.RECOVERY_SETTLE_SECONDS", 0),
        ):
            recover.return_value.ready = False
            recover.return_value.changed = False
            final = self.controller._run_cycle(self.context, config())

        self.assertTrue(final.auth_succeeded)
        self.assertFalse(final.connected)
        self.assertEqual(final.auth_client_ip, "172.30.1.2")
        self.assertIn("DNS disappeared", final.message)

    def test_initial_network_exception_enters_recovery_instead_of_killing_thread(self):
        first = Mock()
        first.ensure_connected.side_effect = OSError("route changed")
        second = Mock()
        second.ensure_connected.return_value = result(InternetStatus.ONLINE)
        self.controller.client_factory = Mock(side_effect=[first, second])

        with (
            patch("szu_guardian.monitor.RECOVERY_ATTEMPTS", 1),
            patch("szu_guardian.monitor.RECOVERY_SETTLE_SECONDS", 0),
        ):
            final = self.controller._run_cycle(self.context, config())

        self.assertTrue(final.connected)
        self.assertTrue(final.recovered)

    def test_accepted_auth_without_link_change_only_reruns_internet_check(self):
        first = Mock()
        first.ensure_connected.return_value = result(auth=AuthStatus.ACCEPTED)
        second = Mock()
        second.check_connection.return_value = result(InternetStatus.ONLINE)
        self.controller.client_factory = Mock(side_effect=[first, second])

        with (
            patch("szu_guardian.wifi.recover_wifi") as recover,
            patch("szu_guardian.monitor.RECOVERY_ATTEMPTS", 1),
            patch("szu_guardian.monitor.RECOVERY_SETTLE_SECONDS", 0),
        ):
            recover.return_value.ready = True
            recover.return_value.changed = False
            final = self.controller._run_cycle(self.context, config(wifi_ssid="SZU_WLAN"))

        self.assertTrue(final.connected)
        self.assertTrue(final.recovered)
        second.check_connection.assert_called_once()
        second.ensure_connected.assert_not_called()

    def test_changed_link_allows_fresh_full_authentication(self):
        first = Mock()
        first.ensure_connected.return_value = result(auth=AuthStatus.ACCEPTED)
        second = Mock()
        second.ensure_connected.return_value = result(InternetStatus.ONLINE)
        self.controller.client_factory = Mock(side_effect=[first, second])

        with (
            patch("szu_guardian.wifi.recover_wifi") as recover,
            patch("szu_guardian.monitor.RECOVERY_ATTEMPTS", 1),
            patch("szu_guardian.monitor.RECOVERY_SETTLE_SECONDS", 0),
        ):
            recover.return_value.ready = True
            recover.return_value.changed = True
            final = self.controller._run_cycle(self.context, config(wifi_ssid="SZU_WLAN"))

        self.assertTrue(final.connected)
        second.ensure_connected.assert_called_once()


class DeadlockTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.controller = MonitorController(
            lambda event: self.events.append(event), config
        )
        self.context = _RunContext(1)

    def test_six_cycles_reset_then_immediately_verify(self):
        verified = Mock()
        verified.ensure_connected.return_value = result()
        self.controller.client_factory = Mock(return_value=verified)

        with patch("szu_guardian.wifi.reset_wifi_interface") as reset:
            reset.return_value.changed = True
            reset.return_value.message = "链路已重建"
            for _ in range(6):
                final = self.controller._handle_auth_deadlock(
                    self.context,
                    config(wifi_ssid="SZU_WLAN"),
                    result(auth=AuthStatus.ACCEPTED),
                )

        reset.assert_called_once()
        verified.ensure_connected.assert_called_once()
        self.assertFalse(final.connected)  # 重置与重新获得 IP 都不是在线证据
        self.assertEqual(self.controller.auth_deadlock_count, 0)

    def test_no_reset_without_managed_ssid(self):
        with patch("szu_guardian.wifi.reset_wifi_interface") as reset:
            for _ in range(6):
                self.controller._handle_auth_deadlock(
                    self.context,
                    config(wifi_ssid=""),
                    result(auth=AuthStatus.ACCEPTED),
                )
        reset.assert_not_called()

    def test_system_online_does_not_count_as_campus_auth_deadlock(self):
        current = result(InternetStatus.DEGRADED, AuthStatus.ACCEPTED)
        with patch("szu_guardian.wifi.reset_wifi_interface") as reset:
            for _ in range(6):
                self.controller._handle_auth_deadlock(
                    self.context,
                    config(wifi_ssid="SZU_WLAN"),
                    current,
                )
        self.assertEqual(self.controller.auth_deadlock_count, 0)
        reset.assert_not_called()

    def test_online_resets_deadlock_counter(self):
        self.controller._auth_deadlock = 5
        self.controller._handle_auth_deadlock(
            self.context,
            config(wifi_ssid="SZU_WLAN"),
            result(InternetStatus.ONLINE),
        )
        self.assertEqual(self.controller.auth_deadlock_count, 0)


class LifecycleTests(unittest.TestCase):
    def test_restart_creates_a_new_generation_instead_of_reviving_old_thread(self):
        controller = MonitorController(lambda _event: None, config)
        entered = []
        stopped = []

        def worker(context):
            entered.append(context.generation)
            context.stop_event.wait(2)
            stopped.append(context.generation)

        controller._run_loop = worker
        controller.start()
        first = controller._thread
        controller.restart()
        second = controller._thread

        self.assertIsNot(first, second)
        self.assertEqual(entered, [1, 2])
        controller.stop()
        self.assertEqual(stopped, [1, 2])

    def test_stop_suppresses_success_from_inflight_network_request(self):
        events = []
        controller = MonitorController(lambda event: events.append(event), config)

        def stop_and_return(_config, progress_callback=None):
            controller.stop()
            return result(InternetStatus.ONLINE)

        client = Mock()
        client.ensure_connected.side_effect = stop_and_return
        with patch.object(controller, "client_factory", return_value=client):
            controller.start()
            controller._thread.join(timeout=3)

        self.assertFalse(any(event.state == "online" for event in events))
        self.assertEqual(events[-1].state, "stopped")

    def test_unexpected_thread_death_clears_health_and_emits_stopped(self):
        events = []
        controller = MonitorController(
            lambda event: events.append(event),
            config_provider=lambda: (_ for _ in ()).throw(RuntimeError("config broken")),
        )
        with patch.object(controller, "client_factory", return_value=Mock()):
            controller.start()
            controller._thread.join(timeout=3)

        self.assertTrue(any(event.state == "offline" for event in events))
        self.assertEqual(events[-1].state, "stopped")


if __name__ == "__main__":
    unittest.main()
