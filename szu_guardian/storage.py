"""配置与凭据存储（跨平台）。

- macOS：密码存入登录钥匙串（``security`` CLI），配置文件只留标记
- Windows：密码用 DPAPI（CryptProtectData）加密后存入配置文件（同上游原版格式）

配置通过 ``password_scheme`` 字段自描述保护方案（keychain / dpapi），
跨系统拷贝配置时读取方只认自己的方案，另一侧优雅降级为要求重新输入。
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

from .models import AppConfig, ZONE_AUTO


APP_DIRECTORY = "SZUNetworkGuardian"
KEYCHAIN_SERVICE = "com.szu.network-guardian"
KEYCHAIN_ACCOUNT = "szu-guardian"

SECURITY_NOT_FOUND = 44

SCHEME_KEYCHAIN = "keychain"
SCHEME_DPAPI = "dpapi"


class KeychainError(OSError):
    pass


class SecretBackend:
    """密码保护后端的统一接口。"""

    name = ""

    def set_password(self, secret: str) -> None:
        raise NotImplementedError

    def get_password(self) -> str:
        raise NotImplementedError

    def delete_password(self) -> None:
        raise NotImplementedError

    def payload_fields(self, has_password: bool) -> dict[str, object]:
        """保存进 config.json 的方案描述字段。"""
        raise NotImplementedError


def _run_security(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/usr/bin/security", *args],
        capture_output=True,
        text=True,
        timeout=8,
    )


class KeychainBackend(SecretBackend):
    """Login-keychain password storage (macOS).

    密码以 base64(UTF-8) 形式写入钥匙串：macOS 的 ``security`` CLI
    读取非 ASCII 或以 ``-`` 开头的密码时会返回十六进制甚至报错，
    base64 化后存取恒为纯 ASCII，可完整还原任意密码。
    """

    name = SCHEME_KEYCHAIN

    def __init__(self, runner=_run_security):
        self._runner = runner

    @staticmethod
    def _encode(secret: str) -> str:
        return base64.b64encode(secret.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode(raw: str) -> str:
        if not raw:
            return ""
        try:
            return base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            pass
        if len(raw) % 2 == 0:
            try:
                return bytes.fromhex(raw).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                pass
        return raw

    def set_password(self, secret: str) -> None:
        if not secret:
            self.delete_password()
            return
        result = self._runner(
            [
                "add-generic-password",
                "-U",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
                self._encode(secret),
                "-j",
                "SZU Network Guardian",
            ]
        )
        if result.returncode != 0:
            raise KeychainError(
                f"写入钥匙串失败：{result.stderr.strip() or result.returncode}"
            )

    def get_password(self) -> str:
        result = self._runner(
            [
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ]
        )
        if result.returncode == SECURITY_NOT_FOUND:
            return ""
        if result.returncode != 0:
            raise KeychainError(
                f"读取钥匙串失败：{result.stderr.strip() or result.returncode}"
            )
        return self._decode(result.stdout.rstrip("\n"))

    def delete_password(self) -> None:
        result = self._runner(
            [
                "delete-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
            ]
        )
        if result.returncode not in (0, SECURITY_NOT_FOUND):
            raise KeychainError(
                f"删除钥匙串条目失败：{result.stderr.strip() or result.returncode}"
            )

    def payload_fields(self, has_password: bool) -> dict[str, object]:
        return {"password_scheme": SCHEME_KEYCHAIN}


def dpapi_protect(secret: str) -> str:
    """CryptProtectData —— 按当前用户加密，返回 base64。仅 Windows。"""
    if sys.platform != "win32":
        raise OSError("DPAPI 仅支持 Windows")

    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    raw = secret.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw, max(1, len(raw)))
    input_blob = DATA_BLOB(
        len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    )
    output_blob = DATA_BLOB()

    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "SZU Network Guardian",
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        protected = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return base64.b64encode(protected).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def dpapi_unprotect(encoded: str) -> str:
    if sys.platform != "win32":
        raise OSError("DPAPI 仅支持 Windows")

    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    protected = base64.b64decode(encoded)
    buffer = ctypes.create_string_buffer(protected, max(1, len(protected)))
    input_blob = DATA_BLOB(
        len(protected), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    )
    output_blob = DATA_BLOB()

    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        raw = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return raw.decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


class DpapiBackend(SecretBackend):
    """Windows DPAPI（CryptProtectData）密码保护，密文存入配置文件。

    与上游 Windows 原版兼容：原版的 ``password_dpapi`` 字段可直接读取。
    """

    name = SCHEME_DPAPI

    def __init__(self, protector=dpapi_protect, unprotector=dpapi_unprotect):
        self._protect = protector
        self._unprotect = unprotector
        self._encoded = ""

    def set_password(self, secret: str) -> None:
        self._encoded = self._protect(secret) if secret else ""

    def get_password(self) -> str:
        if not self._encoded:
            return ""
        return self._unprotect(self._encoded)

    def decode_password(self, encoded: str) -> str:
        """从配置文件中的密文还原密码（不改变会话态）。"""
        if not encoded:
            return ""
        return self._unprotect(encoded)

    def delete_password(self) -> None:
        self._encoded = ""

    def payload_fields(self, has_password: bool) -> dict[str, object]:
        fields: dict[str, object] = {"password_scheme": SCHEME_DPAPI}
        if has_password:
            fields["password_dpapi"] = self._encoded
        return fields


def default_secret_backend() -> SecretBackend:
    if sys.platform == "win32":
        return DpapiBackend()
    return KeychainBackend()


def default_config_path() -> Path:
    override = os.environ.get("SZU_GUARDIAN_DATA_DIR")
    if override:
        return Path(override) / "config.json"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_DIRECTORY / "config.json"
        return Path.home() / f".{APP_DIRECTORY}" / "config.json"
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / APP_DIRECTORY
        / "config.json"
    )


class ConfigStore:
    def __init__(self, path: Path | None = None, backend: SecretBackend | None = None):
        self.path = path or default_config_path()
        self.backend = backend or default_secret_backend()

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            password = self._load_password(payload)
            schema_version = int(payload.get("schema_version", 1))
            zone = str(payload.get("zone", ZONE_AUTO))
            if schema_version < 2:
                zone = ZONE_AUTO
            return AppConfig(
                username=str(payload.get("username", "")),
                password=password,
                zone=zone,
                interval_minutes=int(payload.get("interval_minutes", 5)),
                autostart=bool(payload.get("autostart", False)),
                start_on_launch=bool(payload.get("start_on_launch", True)),
                direct_mode=bool(payload.get("direct_mode", True)),
                wifi_ssid=str(payload.get("wifi_ssid", "")),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return AppConfig()

    def _load_password(self, payload: dict) -> str:
        scheme = str(payload.get("password_scheme", ""))
        if not scheme:
            # 旧版 macOS 配置：password_keychain 布尔标记
            if payload.get("password_keychain") and isinstance(
                self.backend, KeychainBackend
            ):
                return self.backend.get_password()
            return ""
        if scheme != self.backend.name:
            # 配置来自另一个系统（如 Windows 的 dpapi 配置拿到 macOS 读）：
            # 无法解密，视为未配置，提示用户重新输入
            return ""
        if isinstance(self.backend, DpapiBackend):
            encoded = str(payload.get("password_dpapi", ""))
            return self.backend.decode_password(encoded)
        return self.backend.get_password()

    def save(self, config: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backend.set_password(config.password)
        payload = config.public_dict()
        payload["schema_version"] = 3
        payload.update(self.backend.payload_fields(bool(config.password)))
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
