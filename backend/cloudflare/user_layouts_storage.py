"""Gate D1 user layouts behind isolated development identity configuration."""
from __future__ import annotations

import os

from backend.cloudflare.identity_storage import get_d1_identity
from backend.cloudflare.user_layouts import D1UserLayoutsRepository


def get_d1_user_layouts() -> D1UserLayoutsRepository | None:
    mode = os.getenv("OPENTERMINALUI_USER_LAYOUTS_STORE", "sqlalchemy").strip().lower()
    if mode == "sqlalchemy":
        return None
    if mode != "d1":
        raise RuntimeError("Unsupported user layouts storage backend")
    identity = get_d1_identity()
    if identity is None:
        raise RuntimeError("D1 user layouts require isolated D1 identity")
    return D1UserLayoutsRepository(identity.client)
