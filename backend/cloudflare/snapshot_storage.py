"""Explicitly gated local integration for the D1 SQLite snapshot prototype.

Never use this module for production or live data. It is an integration step,
not a complete Cloudflare deployment.
"""
from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from backend.cloudflare.d1 import D1BatchClient
from backend.cloudflare.sqlite_images import D1SQLiteImageStore
from backend.cloudflare.sqlalchemy_sqlite_prototype import SQLiteSnapshotCoordinator


class LocalSnapshotStorage:
    def __init__(self, client: D1BatchClient, name: str) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="openterminalui-d1-local-")
        self._closed = False
        try:
            self.coordinator = SQLiteSnapshotCoordinator(D1SQLiteImageStore(client), name, Path(self._temporary.name))
            self.engine = self.coordinator.create_engine()
        except BaseException:
            self._temporary.cleanup()
            raise
        atexit.register(self.close)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.engine.dispose()
        self._temporary.cleanup()


def local_snapshot_storage_from_env() -> LocalSnapshotStorage | None:
    mode = os.getenv("OPENTERMINALUI_DATABASE_STORE", "sqlalchemy").strip().lower()
    if mode == "sqlalchemy":
        return None
    configured_env = (os.getenv("OPENTERMINALUI_ENV") or os.getenv("APP_ENV") or os.getenv("ENV") or "").strip().lower()
    endpoint = os.getenv("D1_GATEWAY_URL", "")
    try:
        url = urlsplit(endpoint)
        local_gateway = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    except ValueError:
        local_gateway = False
    other_stores = ("OPENTERMINALUI_IDENTITY_STORE", "OPENTERMINALUI_SAVED_VIEWS_STORE",
                    "OPENTERMINALUI_WATCHLISTS_STORE", "OPENTERMINALUI_USER_LAYOUTS_STORE")
    if (mode != "d1_snapshot" or configured_env not in {"dev", "development", "local", "test", "testing"}
            or os.getenv("OPENTERMINALUI_D1_SNAPSHOT_EXPERIMENT") != "1"
            or os.getenv("OPENTERMINALUI_D1_LOCAL_TEST") != "1" or not local_gateway
            or any(os.getenv(key, "sqlalchemy").strip().lower() != "sqlalchemy" for key in other_stores)):
        raise RuntimeError("D1 snapshot database is restricted to isolated local tests with unified storage")
    try:
        client = D1BatchClient(endpoint, os.getenv("D1_BRIDGE_TOKEN", ""), allow_insecure_localhost=True)
    except ValueError:
        raise RuntimeError("Invalid local D1 snapshot gateway configuration") from None
    return LocalSnapshotStorage(client, os.getenv("OPENTERMINALUI_D1_SNAPSHOT_NAME", "openterminalui-local"))
