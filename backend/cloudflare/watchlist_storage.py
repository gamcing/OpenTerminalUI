"""Gate D1 watchlists behind the isolated development identity configuration."""
from __future__ import annotations

import os
from backend.cloudflare.identity_storage import get_d1_identity
from backend.cloudflare.watchlists import D1WatchlistRepository


def get_d1_watchlists() -> D1WatchlistRepository | None:
    mode = os.getenv("OPENTERMINALUI_WATCHLISTS_STORE", "sqlalchemy").strip().lower()
    if mode == "sqlalchemy":
        return None
    if mode != "d1":
        raise RuntimeError("Unsupported watchlist storage backend")
    identity = get_d1_identity()
    if identity is None:
        raise RuntimeError("D1 watchlists require isolated D1 identity")
    return D1WatchlistRepository(identity.client)
