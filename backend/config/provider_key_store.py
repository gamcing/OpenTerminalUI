"""Encrypted provider settings in the gated D1 snapshot experiments."""
from __future__ import annotations

import json
import os
from threading import RLock

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text

from backend.config.provider_registry import PROVIDER_ENV_KEYS

_LOCK = RLock()


def database_keys_enabled() -> bool:
    mode = os.getenv("OPENTERMINALUI_PROVIDER_KEYS_STORE", "env_file").strip().lower()
    if mode == "env_file":
        return False
    if mode != "database":
        raise RuntimeError("invalid provider key storage mode")
    from backend.shared.db import snapshot_storage
    if snapshot_storage is None:
        raise RuntimeError("database provider keys are restricted to D1 snapshot experiments")
    _fernet()  # Fail closed before accepting a configuration without a stable key.
    return True


def _fernet() -> Fernet:
    try:
        return Fernet(os.environ["OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY"].encode("ascii"))
    except (KeyError, ValueError, TypeError, UnicodeError):
        raise RuntimeError("a valid provider key encryption key is required") from None


def _decode(name: str, ciphertext: str, cipher: Fernet) -> str:
    try:
        payload = json.loads(cipher.decrypt(ciphertext.encode("ascii")))
        value = payload["value"]
        if (name not in PROVIDER_ENV_KEYS or payload["name"] != name or not isinstance(value, str)
                or len(value) > 512 or "\n" in value or "\r" in value):
            raise ValueError
        return value
    except (InvalidToken, ValueError, TypeError, KeyError, UnicodeError, AttributeError):
        raise RuntimeError("cannot decrypt provider configuration") from None


def restore_provider_keys() -> None:
    if not database_keys_enabled():
        return
    from backend.shared.db import engine
    from backend.config.settings import get_settings
    with _LOCK:
        cipher = _fernet()
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT name,ciphertext FROM provider_secrets")).all()
        # Decrypt all values before changing any environment variable.
        restored = {name: _decode(name, ciphertext, cipher) for name, ciphertext in rows}
        for name, value in restored.items():
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
        get_settings.cache_clear()


def database_key_names() -> set[str]:
    if not database_keys_enabled():
        raise RuntimeError("database provider key storage is not enabled")
    from backend.shared.db import engine
    with engine.connect() as connection:
        return set(connection.execute(text("SELECT name FROM provider_secrets")).scalars())


def save_provider_keys(values: dict[str, str]) -> None:
    if not database_keys_enabled():
        raise RuntimeError("database provider key storage is not enabled")
    from backend.shared.db import engine
    from backend.config.settings import get_settings
    with _LOCK:
        cipher = _fernet()
        encrypted = []
        for name, value in values.items():
            if (name not in PROVIDER_ENV_KEYS or not isinstance(value, str) or len(value) > 512
                    or "\n" in value or "\r" in value):
                raise ValueError("invalid provider configuration")
            payload = json.dumps({"name": name, "value": value}, separators=(",", ":")).encode("utf-8")
            encrypted.append({"name": name, "ciphertext": cipher.encrypt(payload).decode("ascii")})
        if encrypted:
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO provider_secrets(name,ciphertext) VALUES (:name,:ciphertext) "
                    "ON CONFLICT(name) DO UPDATE SET ciphertext=excluded.ciphertext"
                ), encrypted)
            # Do not alter live settings if the database commit failed or is unknown.
            for name, value in values.items():
                if value:
                    os.environ[name] = value
                else:
                    os.environ.pop(name, None)
            get_settings.cache_clear()
