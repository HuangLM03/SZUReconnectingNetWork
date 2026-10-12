import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from szu_guardian.models import AppConfig
from szu_guardian.storage import (
    ConfigStore,
    DpapiBackend,
    KeychainBackend,
)


def fake_security_runner(calls=None):
    stored: dict[str, str] = {}
    runner = Mock()

    def side_effect(args, **kwargs):
        calls.append(args)
        command = args[0]
        if command == "add-generic-password":
            stored["password"] = args[args.index("-w") + 1]
            return Mock(returncode=0, stdout="", stderr="")
        if command == "find-generic-password":
            if "password" not in stored:
                return Mock(returncode=44, stdout="", stderr="not found")
            return Mock(returncode=0, stdout=stored["password"] + "\n", stderr="")
        if command == "delete-generic-password":
            stored.pop("password", None)
            return Mock(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected security call: {args}")

    runner.side_effect = side_effect
    return runner


class KeychainBackendTests(unittest.TestCase):
    def test_set_get_delete_roundtrip(self):
        calls: list[list[str]] = []
        backend = KeychainBackend(fake_security_runner(calls))

        backend.set_password("s3cret 密码!")
        self.assertEqual(backend.get_password(), "s3cret 密码!")
        backend.delete_password()
        self.assertEqual(backend.get_password(), "")

        self.assertTrue(
            all(
                call[0]
                in {"add-generic-password", "find-generic-password", "delete-generic-password"}
                for call in calls
            )
        )

    def test_delete_missing_item_is_tolerated(self):
        runner = Mock(
            return_value=Mock(returncode=44, stdout="", stderr="not found")
        )
        KeychainBackend(runner).delete_password()

    def test_read_failure_raises(self):
        runner = Mock(
            return_value=Mock(returncode=51, stdout="", stderr="locked")
        )
        with self.assertRaises(OSError):
            KeychainBackend(runner).get_password()


class ConfigStoreTests(unittest.TestCase):
    def test_password_never_written_to_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path, KeychainBackend(fake_security_runner([])))
            original = AppConfig(
                username="123456",
                password="very-private-password",
                interval_minutes=3,
            )

            store.save(original)
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            restored = store.load()

            self.assertNotIn(original.password, raw)
            self.assertEqual(payload["password_scheme"], "keychain")
            self.assertEqual(restored.password, original.password)
            self.assertEqual(restored.username, original.username)
            self.assertEqual(restored.interval_minutes, 3)

    def test_load_tolerates_missing_keychain_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps({"username": "u", "password_keychain": True}),
                encoding="utf-8",
            )
            # 钥匙串条目不存在（returncode 44）
            runner = Mock(
                return_value=Mock(returncode=44, stdout="", stderr="not found")
            )
            restored = ConfigStore(path, KeychainBackend(runner)).load()

            self.assertEqual(restored.username, "u")
            self.assertEqual(restored.password, "")

    def test_load_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(
                Path(directory) / "config.json",
                KeychainBackend(fake_security_runner([])),
            )
            self.assertEqual(store.load().username, "")


def fake_dpapi_pair():
    """可逆的假加密：b64(secret)；用于在非 Windows 平台测试 DPAPI 分支。"""
    return (
        lambda secret: __import__("base64").b64encode(secret.encode()).decode(),
        lambda encoded: __import__("base64").b64decode(encoded).decode(),
    )


class DpapiBackendTests(unittest.TestCase):
    def test_roundtrip_and_payload_fields(self):
        protect, unprotect = fake_dpapi_pair()
        backend = DpapiBackend(protector=protect, unprotector=unprotect)

        backend.set_password("win-密码-123")
        self.assertEqual(backend.get_password(), "win-密码-123")
        fields = backend.payload_fields(True)
        self.assertEqual(fields["password_scheme"], "dpapi")
        self.assertIn("password_dpapi", fields)
        backend.delete_password()
        self.assertEqual(backend.get_password(), "")

    def test_config_roundtrip_via_config_store(self):
        protect, unprotect = fake_dpapi_pair()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            store = ConfigStore(path, DpapiBackend(protect, unprotect))
            original = AppConfig(username="432802", password="secret-pw")

            store.save(original)
            payload = json.loads(path.read_text(encoding="utf-8"))
            restored = store.load()

            self.assertEqual(payload["password_scheme"], "dpapi")
            self.assertNotIn(original.password, json.dumps(payload))
            self.assertEqual(restored.password, original.password)

    def test_cross_platform_config_degrades_gracefully(self):
        """dpapi 配置拿到钥匙串后端读取 → 密码为空，其余字段保留。"""
        protect, unprotect = fake_dpapi_pair()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            windows_store = ConfigStore(path, DpapiBackend(protect, unprotect))
            windows_store.save(
                AppConfig(username="432802", password="secret-pw", wifi_ssid="SZU_WLAN")
            )

            mac_store = ConfigStore(path, KeychainBackend(fake_security_runner([])))
            restored = mac_store.load()

            self.assertEqual(restored.username, "432802")
            self.assertEqual(restored.wifi_ssid, "SZU_WLAN")
            self.assertEqual(restored.password, "")


if __name__ == "__main__":
    unittest.main()
