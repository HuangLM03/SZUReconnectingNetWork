import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from main import run_headless_check
from szu_guardian.connectivity import InternetStatus
from szu_guardian.models import AppConfig
from szu_guardian.network import ConnectionResult


class HeadlessEntryTests(unittest.TestCase):
    def test_detect_uses_configured_transport_but_never_authenticates(self):
        store = Mock()
        config = AppConfig(username="u", password="p", direct_mode=True)
        store.load.return_value = config
        client = Mock()
        client.check_connection.return_value = ConnectionResult(
            InternetStatus.ONLINE,
            "互联网连接正常",
        )
        output = io.StringIO()

        with (
            patch("szu_guardian.network.NetworkClient", return_value=client),
            redirect_stdout(output),
        ):
            exit_code = run_headless_check(store, detect_only=True)

        self.assertEqual(exit_code, 0)
        client.check_connection.assert_called_once_with(config)
        client.ensure_connected.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "online")
        self.assertNotIn("password", payload)


if __name__ == "__main__":
    unittest.main()
