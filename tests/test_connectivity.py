import io
import unittest
from unittest.mock import Mock

import requests

from szu_guardian.connectivity import (
    InternetStatus,
    ProbeSpec,
    check_path,
    run_probe,
)


APPLE_TEXT = "<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"


def response(
    url="https://captive.apple.com/hotspot-detect.html",
    *,
    status=200,
    text=APPLE_TEXT,
    content_type="text/html",
    location=None,
    history=(),
):
    value = Mock(spec=requests.Response)
    value.url = url
    value.status_code = status
    value.text = text
    value.content = text.encode()
    value.raw = io.BytesIO(value.content)
    value.headers = {"Content-Type": content_type}
    if location is not None:
        value.headers["Location"] = location
    value.history = list(history)
    value.close.return_value = None
    return value


def spec(**kwargs):
    return ProbeSpec(
        name="Apple",
        url="https://captive.apple.com/hotspot-detect.html",
        exact_text=APPLE_TEXT,
        content_type="text/html",
        **kwargs,
    )


class StrictProbeTests(unittest.TestCase):
    def run_one(self, value):
        session = Mock()
        session.get.return_value = value
        result = run_probe(session, spec())
        kwargs = session.get.call_args.kwargs
        self.assertTrue(kwargs["verify"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        return result

    def test_exact_https_response_is_online_evidence(self):
        result = self.run_one(response())
        self.assertTrue(result.success)

    def test_portal_body_containing_success_text_is_not_accepted(self):
        result = self.run_one(response(text="<html>校园门户 Success</html>"))
        self.assertFalse(result.success)

    def test_cross_origin_redirect_cannot_be_online(self):
        result = self.run_one(
            response(status=302, location="http://172.30.1.1/login")
        )
        self.assertFalse(result.success)

    def test_relative_redirect_is_not_accepted(self):
        result = self.run_one(response(status=302, location="/portal?url=Success"))
        self.assertFalse(result.success)

    def test_redirect_history_is_rejected_even_if_final_url_matches(self):
        result = self.run_one(response(history=[response()]))
        self.assertFalse(result.success)

    def test_http_downgrade_is_rejected(self):
        result = self.run_one(
            response(url="http://captive.apple.com/hotspot-detect.html")
        )
        self.assertFalse(result.success)

    def test_certificate_error_is_not_retried_without_verification(self):
        session = Mock()
        session.get.side_effect = requests.exceptions.SSLError("bad certificate")

        result = run_probe(session, spec())

        self.assertFalse(result.success)
        self.assertIn("SSLError", result.detail)
        self.assertTrue(session.get.call_args.kwargs["verify"])
        self.assertEqual(session.get.call_count, 1)

    def test_interrupted_response_body_becomes_failed_probe(self):
        from urllib3.exceptions import ProtocolError

        value = response()
        value.raw = Mock()
        value.raw.read.side_effect = ProtocolError("connection interrupted")
        result = self.run_one(value)
        self.assertFalse(result.success)
        self.assertIn("ProtocolError", result.detail)

    def test_oversized_fixed_response_is_rejected(self):
        result = self.run_one(response(text="A" * (64 * 1024 + 1)))
        self.assertFalse(result.success)


class PathAggregationTests(unittest.TestCase):
    def test_two_independent_successes_are_required(self):
        session = Mock()
        session.get.side_effect = [
            response(),
            requests.exceptions.Timeout("blocked"),
            requests.exceptions.Timeout("blocked"),
        ]

        report = check_path(session, "system")

        self.assertEqual(report.status, InternetStatus.DEGRADED)
        self.assertEqual(report.succeeded, 1)

    def test_all_portal_like_responses_stay_blocked(self):
        session = Mock()
        session.get.side_effect = [
            response(text="portal"),
            response(
                "https://www.baidu.com/",
                text="szu_guardian_random_token",
            ),
            response(
                "https://www.taobao.com/",
                text="portal",
            ),
        ]

        report = check_path(session, "system")

        self.assertEqual(report.status, InternetStatus.BLOCKED)
        self.assertEqual(report.succeeded, 0)

    def test_total_timeout_is_unknown_not_online(self):
        session = Mock()
        session.get.side_effect = requests.exceptions.Timeout("timeout")

        report = check_path(session, "system")

        self.assertEqual(report.status, InternetStatus.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
