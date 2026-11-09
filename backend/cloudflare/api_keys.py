"""API-key storage for isolated D1 identity tests, not production deployment."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement
from backend.cloudflare.identity_storage import get_d1_identity

_FIELDS = "id,user_id,name,key_prefix,permissions,is_active,last_used_at,created_at"


@dataclass(frozen=True)
class D1APIKey:
    id: int
    user_id: str
    name: str
    key_prefix: str
    permissions: str
    is_active: int
    last_used_at: datetime | None
    created_at: datetime


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _record(row: dict[str, Any]) -> D1APIKey:
    return D1APIKey(id=row["id"], user_id=row["user_id"], name=row["name"], key_prefix=row["key_prefix"],
                    permissions=row["permissions"], is_active=row["is_active"],
                    last_used_at=datetime.fromisoformat(row["last_used_at"]) if row["last_used_at"] else None,
                    created_at=datetime.fromisoformat(row["created_at"]))


def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = result.get("results")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise D1BatchOutcomeUnknown("D1 returned an unexpected API-key response")
    return rows


class D1APIKeyRepository:
    def __init__(self, client: D1BatchClient) -> None:
        self.client = client

    def create(self, user_id: str, name: str, prefix: str, key_hash: str, permissions: str) -> D1APIKey | None:
        if permissions not in {"read", "read_write"}:
            raise ValueError("invalid API-key permissions")
        rows = _rows(self.client.batch([D1Statement(
            "INSERT INTO api_keys (user_id,name,key_prefix,key_hash,permissions,is_active,created_at) "
            f"SELECT id,?,?,?,?,1,? FROM users WHERE id=? RETURNING {_FIELDS}",
            [name, prefix, key_hash, permissions, _now(), user_id],
        )])[0])
        if len(rows) > 1:
            raise D1BatchOutcomeUnknown("D1 returned inconsistent API-key creation results")
        return _record(rows[0]) if rows else None

    def list(self, user_id: str) -> list[D1APIKey]:
        rows = _rows(self.client.batch([D1Statement(
            f"SELECT {_FIELDS} FROM api_keys WHERE user_id=? AND is_active=1 ORDER BY id", [user_id],
        )])[0])
        return [_record(row) for row in rows]

    def revoke(self, user_id: str, key_id: int) -> bool:
        rows = _rows(self.client.batch([D1Statement(
            "UPDATE api_keys SET is_active=0 WHERE id=? AND user_id=? RETURNING id", [key_id, user_id],
        )])[0])
        if len(rows) > 1:
            raise D1BatchOutcomeUnknown("D1 returned inconsistent API-key revocation results")
        return bool(rows)

    def authenticate(self, key: str) -> D1APIKey | None:
        if not key.startswith("otui_") or len(key) < 12 or len(key) > 512:
            return None
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        rows = _rows(self.client.batch([D1Statement(
            f"UPDATE api_keys SET last_used_at=? WHERE key_prefix=? AND key_hash=? AND is_active=1 RETURNING {_FIELDS}",
            [_now(), key[:12], key_hash],
        )])[0])
        if len(rows) > 1:
            raise D1BatchOutcomeUnknown("D1 returned inconsistent API-key authentication results")
        return _record(rows[0]) if rows else None


def get_d1_api_keys() -> D1APIKeyRepository | None:
    identity = get_d1_identity()
    return D1APIKeyRepository(identity.client) if identity is not None else None
