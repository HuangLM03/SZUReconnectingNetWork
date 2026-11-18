import unittest
from unittest.mock import Mock, patch

from szu_guardian.connectivity import InternetStatus, PathReport, ProbeResult
from szu_guardian.models import AppConfig, ZONE_AUTO, ZONE_DORMITORY, ZONE_OFFICE
from szu_guardian.network import (
    AuthEvidence,
    AuthStatus,
    AuthenticationRejected,
    NetworkClient,
    PortalUnavailable,
    _combine_reports,
)


def report(status, path="direct", setup_error=""):
    probes = ()
    if status == InternetStatus.ONLINE:
        probes = tuple(
            ProbeResult(name, f"https://{name.lower()}.test", True, "verified")
            for name in ("Apple", "Baidu")
        )
    return PathReport(
        path=path,
        status=status,
        probes=probes,
        latency_ms=20,
        interface="en0" if path == "direct" else "",
        source_ip="172.30.1.2" if path == "direct" else "",
        setup_error=setup_error,
    )


def config(**kwargs):
    return AppConfig(username="user", password="password", **kwargs)


class NetworkClientTests(unittest.TestCase):
    def test_two_verified_paths_are_online_without_login(self):
        client = NetworkClient()
        with (
            patch.object(
                client,
                "check_connection",
                return_value=_combine_reports(
                    report(InternetStatus.ONLINE),
                    report(InternetStatus.ONLINE, "system"),
                    direct_enabled=True,
                ),
            ),
            patch.object(client, "send_login") as login,
        ):
            result = client.ensure_connected(config())

        self.assertTrue(result.connected)
        login.assert_not_called()

    def test_srun_offline_does_not_skip_initial_internet_check(self):
        client = NetworkClient()
        with (
            patch.object(client, "check_connection") as check,
            patch.object(client, "_teaching_online_status") as online,
            patch.object(client, "send_login") as login,
        ):
            check.return_value = _combine_reports(
                report(InternetStatus.ONLINE),
                report(InternetStatus.ONLINE, "system"),
                direct_enabled=True,
            )
            result = client.ensure_connected(config(zone=ZONE_OFFICE))

        self.assertTrue(result.connected)
        check.assert_called_once()
        online.assert_not_called()
        login.assert_not_called()

    def test_direct_online_but_system_blocked_is_degraded_not_recovered(self):
        client = NetworkClient()
        from szu_guardian.network import _combine_reports

        current = _combine_reports(
            report(InternetStatus.ONLINE),
            report(InternetStatus.BLOCKED, "system"),
            direct_enabled=True,
        )
        with (
            patch.object(client, "check_connection", return_value=current),
            patch.object(client, "send_login") as login,
        ):
            result = client.ensure_connected(config())

        self.assertEqual(result.status, InternetStatus.DEGRADED)
        self.assertFalse(result.connected)
        self.assertIn("系统代理/TUN", result.message)
        login.assert_not_called()

    def test_system_online_but_direct_blocked_is_not_campus_recovery(self):
        client = NetworkClient()
        from szu_guardian.network import _combine_reports

        current = _combine_reports(
            report(InternetStatus.BLOCKED),
            report(InternetStatus.ONLINE, "system"),
            direct_enabled=True,
        )
        with (
            patch.object(client, "check_connection", return_value=current),
            patch.object(client, "send_login") as login,
        ):
            result = client.ensure_connected(config())

        self.assertEqual(result.status, InternetStatus.DEGRADED)
        self.assertFalse(result.connected)
        login.assert_not_called()

    def test_accepted_auth_but_internet_blocked_never_connected(self):
        client = NetworkClient(verify_delays=(0, 0), sleeper=lambda _: None)
        from szu_guardian.network import _combine_reports, AuthEvidence

        blocked = _combine_reports(
            report(InternetStatus.BLOCKED),
            report(InternetStatus.BLOCKED, "system"),
            direct_enabled=True,
        )
        with (
            patch.object(client, "check_connection", return_value=blocked),
            patch.object(
                client,
                "send_login",
                return_value=AuthEvidence(AuthStatus.ACCEPTED, "认证成功", "172.30.1.2"),
            ),
        ):
            result = client.ensure_connected(config())

        self.assertFalse(result.connected)
        self.assertTrue(result.auth_succeeded)
        self.assertEqual(result.auth_client_ip, "172.30.1.2")
        self.assertIn("公共互联网仍未通过", result.message)

    def test_auth_is_not_attempted_when_direct_route_setup_failed(self):
        client = NetworkClient()
        from szu_guardian.network import _combine_reports

        current = _combine_reports(
            report(InternetStatus.UNKNOWN, setup_error="DNS unavailable"),
            report(InternetStatus.UNKNOWN, "system"),
            direct_enabled=True,
        )
        with (
            patch.object(client, "check_connection", return_value=current),
            patch.object(client, "send_login") as login,
        ):
            result = client.ensure_connected(config())

        self.assertFalse(result.connected)
        login.assert_not_called()

    def test_explicit_rejection_does_not_get_retried_as_link_failure(self):
        client = NetworkClient()
        from szu_guardian.network import _combine_reports

        blocked = _combine_reports(
            report(InternetStatus.BLOCKED),
            report(InternetStatus.BLOCKED, "system"),
            direct_enabled=True,
        )
        with (
            patch.object(client, "check_connection", return_value=blocked),
            patch.object(
                client,
                "send_login",
                side_effect=AuthenticationRejected("Authentication fail"),
            ),
        ):
            result = client.ensure_connected(config())

        self.assertEqual(result.auth_status, AuthStatus.REJECTED)
        self.assertFalse(result.connected)

    def test_dormitory_requires_explicit_result_one(self):
        client = NetworkClient(portal_session=Mock(), session=Mock())
        client.portal_session.get.return_value.url = (
            "http://172.30.255.42:801/eportal/portal/login/"
        )
        client.portal_session.get.return_value.text = (
            'jsonpReturn({"result":0,"msg":"认证未成功 unsuccessful"});'
        )
        client.portal_session.get.return_value.raise_for_status.return_value = None

        with self.assertRaises(AuthenticationRejected):
            client._login_dormitory(config(zone=ZONE_DORMITORY))

    def test_dormitory_accepts_result_one_not_success_text(self):
        client = NetworkClient(portal_session=Mock(), session=Mock())
        client.portal_session.get.return_value.url = (
            "http://172.30.255.42:801/eportal/portal/login/"
        )
        client.portal_session.get.return_value.text = (
            'jsonpReturn({"result":1,"msg":"ok"});'
        )
        client.portal_session.get.return_value.raise_for_status.return_value = None

        evidence = client._login_dormitory(config(zone=ZONE_DORMITORY))

        self.assertEqual(evidence.status, AuthStatus.ACCEPTED)

    def test_dormitory_rejects_cross_origin_redirect(self):
        client = NetworkClient(portal_session=Mock(), session=Mock())
        client.portal_session.get.return_value.url = "http://portal.example/login"
        client.portal_session.get.return_value.text = (
            'jsonpReturn({"result":1,"msg":"认证成功"});'
        )
        client.portal_session.get.return_value.raise_for_status.return_value = None

        with self.assertRaises(PortalUnavailable):
            client._login_dormitory(config(zone=ZONE_DORMITORY))

    def test_auto_mode_falls_back_only_for_unavailable_teaching_portal(self):
        client = NetworkClient()
        with (
            patch.object(client, "_login_teaching", side_effect=PortalUnavailable("timeout")),
            patch.object(client, "_login_dormitory") as dormitory,
        ):
            client.send_login(config(zone=ZONE_AUTO))
        dormitory.assert_called_once()

    def test_auto_mode_does_not_retry_rejected_credentials_elsewhere(self):
        client = NetworkClient()
        with (
            patch.object(
                client,
                "_login_teaching",
                side_effect=AuthenticationRejected("bad password"),
            ),
            patch.object(client, "_login_dormitory") as dormitory,
        ):
            with self.assertRaises(AuthenticationRejected):
                client.send_login(config(zone=ZONE_AUTO))
        dormitory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
