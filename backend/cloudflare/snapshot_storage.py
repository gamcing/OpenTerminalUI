"""Explicitly gated D1 SQLite snapshot experiments.

The local and intercepted-container modes are not ready for live data.
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
    def __init__(self, client: D1BatchClient, name: str, *, fence_on_start: bool = False) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="openterminalui-d1-local-")
        self._closed = False
        try:
            self.coordinator = SQLiteSnapshotCoordinator(D1SQLiteImageStore(client), name, Path(self._temporary.name),
                                                         fence_on_start=fence_on_start)
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
        container_gateway = (url.scheme == "http" and url.hostname == "openterminal-d1.internal"
                             and url.port is None)
    except ValueError:
        local_gateway = False
        container_gateway = False
    local = (configured_env in {"dev", "development", "local", "test", "testing"}
             and os.getenv("OPENTERMINALUI_D1_LOCAL_TEST") == "1" and local_gateway)
    container = (configured_env == "production" and os.getenv("OPENTERMINALUI_CF_CONTAINER") == "1"
                 and os.getenv("OPENTERMINALUI_D1_CONTAINER_EXPERIMENT") == "1" and container_gateway)
    other_stores = ("OPENTERMINALUI_IDENTITY_STORE", "OPENTERMINALUI_SAVED_VIEWS_STORE",
                    "OPENTERMINALUI_WATCHLISTS_STORE", "OPENTERMINALUI_USER_LAYOUTS_STORE")
    if (mode != "d1_snapshot" or not (local or container)
            or os.getenv("OPENTERMINALUI_D1_SNAPSHOT_EXPERIMENT") != "1"
            or any(os.getenv(key, "sqlalchemy").strip().lower() != "sqlalchemy" for key in other_stores)):
        raise RuntimeError("D1 snapshot database requires an explicitly enabled experiment with unified storage")
    try:
        client = D1BatchClient(endpoint, os.getenv("D1_BRIDGE_TOKEN", ""), allow_insecure_localhost=local,
                               allow_intercepted_container_origin=container)
    except ValueError:
        raise RuntimeError("Invalid D1 snapshot gateway configuration") from None
    return LocalSnapshotStorage(client, os.getenv("OPENTERMINALUI_D1_SNAPSHOT_NAME", "openterminalui-local"),
                                fence_on_start=container)
