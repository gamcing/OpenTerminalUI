"""Select experimental D1 identity only for isolated development tests.

Production remains blocked until all application users and their dependent
business records have a single consistent persistence backend.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from backend.cloudflare.d1 import D1BatchClient
from backend.cloudflare.identity import D1IdentityRepository
from backend.config.security import is_development_env


def get_d1_identity() -> D1IdentityRepository | None:
    mode = os.getenv("OPENTERMINALUI_IDENTITY_STORE", "sqlalchemy").strip().lower()
    if mode == "sqlalchemy":
        return None
    configured_env = (os.getenv("OPENTERMINALUI_ENV") or os.getenv("APP_ENV") or os.getenv("ENV") or "").strip().lower()
    endpoint = os.getenv("D1_GATEWAY_URL", "")
    url = urlsplit(endpoint)
    if (mode != "d1" or configured_env not in {"dev", "development", "local", "test", "testing"}
            or not is_development_env() or os.getenv("OPENTERMINALUI_D1_IDENTITY_EXPERIMENT") != "1"
            or os.getenv("OPENTERMINALUI_D1_LOCAL_TEST") != "1" or url.scheme != "http"
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}):
        raise RuntimeError("D1 identity is restricted to explicit isolated development tests")
    try:
        client = D1BatchClient(endpoint, os.getenv("D1_BRIDGE_TOKEN", ""), allow_insecure_localhost=True)
    except ValueError:
        raise RuntimeError("Invalid D1 identity gateway configuration") from None
    return D1IdentityRepository(client)
