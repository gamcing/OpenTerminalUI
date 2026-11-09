"""Opt-in D1 storage for saved views while the full database is migrated."""
from __future__ import annotations

import os

from backend.cloudflare.d1 import D1BatchClient
from backend.cloudflare.saved_views import D1SavedViewRepository


def get_d1_saved_views() -> D1SavedViewRepository | None:
    mode = os.getenv("OPENTERMINALUI_SAVED_VIEWS_STORE", "sqlalchemy").strip().lower()
    if mode == "sqlalchemy":
        return None
    if mode != "d1":
        raise RuntimeError("Unsupported saved-view storage backend")
    endpoint = os.getenv("D1_GATEWAY_URL", "")
    token = os.getenv("D1_BRIDGE_TOKEN", "")
    container = os.getenv("OPENTERMINALUI_CF_CONTAINER") == "1"
    environment = os.getenv("OPENTERMINALUI_ENV", "development").strip().lower()
    local_test = os.getenv("OPENTERMINALUI_D1_LOCAL_TEST") == "1" and environment in {
        "dev", "development", "local", "test", "testing",
    }
    try:
        client = D1BatchClient(
            endpoint, token,
            allow_insecure_localhost=local_test,
            allow_intercepted_container_origin=container,
        )
    except ValueError:
        raise RuntimeError("Invalid D1 saved-view gateway configuration") from None
    return D1SavedViewRepository(client)
