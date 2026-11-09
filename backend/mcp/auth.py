"""API-key authentication for the network-reachable MCP surface.

The stdio MCP server trusted its local process; an HTTP MCP server is
attacker-reachable like any other route, so this module resolves the same
``otui_`` API keys used by the public REST API (``backend/core/api_key_auth.py``)
into an :class:`McpPrincipal` MCP sessions can build a permission-scoped tool
registry from.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.models.api_key import APIKeyORM
from backend.cloudflare.api_keys import get_d1_api_keys
from backend.cloudflare.d1 import D1BatchError
from backend.shared.db import SessionLocal


class McpAuthError(Exception):
    """Raised when an MCP request's API key is missing, invalid, or inactive."""


class McpStorageError(Exception):
    """Raised when API-key storage is unavailable or misconfigured."""


@dataclass(frozen=True)
class McpPrincipal:
    user_id: str
    key_id: int
    permissions: str
    name: str

    @property
    def allow_writes(self) -> bool:
        return self.permissions == "read_write"


def authenticate(raw_key: str, db: Session | None = None) -> McpPrincipal:
    """Resolve an ``otui_`` API key to an :class:`McpPrincipal`.

    The SQLAlchemy path compares sha256 hashes with ``hmac.compare_digest``.
    The experimental D1 path authenticates and records last use atomically.
    """
    if not raw_key:
        raise McpAuthError("Missing API key")

    try:
        identity_keys = get_d1_api_keys()
        if identity_keys is not None:
            record = identity_keys.authenticate(raw_key)
            if record is None:
                raise McpAuthError("Invalid API key")
            return McpPrincipal(user_id=record.user_id, key_id=record.id,
                                permissions=record.permissions, name=record.name)
    except (D1BatchError, RuntimeError, ValueError, KeyError):
        raise McpStorageError("API-key storage unavailable") from None

    owns_session = db is None
    session = db or SessionLocal()
    try:
        prefix = raw_key[:12]
        record = session.query(APIKeyORM).filter(APIKeyORM.key_prefix == prefix).first()
        if record is None:
            raise McpAuthError("Invalid API key")

        computed_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        if not hmac.compare_digest(computed_hash, record.key_hash):
            raise McpAuthError("Invalid API key")

        if record.is_active != 1:
            raise McpAuthError("API key is inactive")

        record.last_used_at = func.now()
        session.commit()

        return McpPrincipal(
            user_id=record.user_id,
            key_id=record.id,
            permissions=record.permissions,
            name=record.name,
        )
    finally:
        if owns_session:
            session.close()
