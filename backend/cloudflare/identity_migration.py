"""Offline identity, API-key and saved-view copy primitive; not a complete application migration.

The caller must quiesce source writes and use a stable, read-only source snapshot
throughout the cutover. This module deliberately does not switch the application
or expose a command for live migrations.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement

_COLUMNS = {
    "users": ("id", "email", "hashed_password", "role", "created_at", "last_login"),
    "refresh_tokens": ("id", "user_id", "jti", "expires_at", "created_at", "revoked_at"),
    "saved_views": ("id", "user_id", "name", "scope", "page", "payload_json", "description", "created_at", "updated_at"),
    "api_keys": ("id", "user_id", "name", "key_prefix", "key_hash", "permissions", "is_active", "last_used_at", "created_at"),
}
_DATE_FIELDS = {"created_at", "updated_at", "last_login", "expires_at", "revoked_at", "last_used_at"}


@dataclass(frozen=True, repr=False)
class IdentitySnapshot:
    # Contains password hashes and token IDs. Never log or serialize this object
    # outside the encrypted/private D1 transport.
    users: list[list[Any]] = field(repr=False)
    refresh_tokens: list[list[Any]] = field(repr=False)
    saved_views: list[list[Any]] = field(repr=False)
    api_keys: list[list[Any]] = field(default_factory=list, repr=False)


def _timestamp(value: Any) -> str:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(parsed, datetime):
        raise ValueError("source contains an invalid timestamp")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.isoformat(sep=" ", timespec="microseconds")


def read_identity_snapshot(connection: Connection) -> IdentitySnapshot:
    """Read source rows inside a transaction selected by the caller.

    A transaction by itself does not guarantee repeatable reads: use a SQLite
    read transaction or PostgreSQL REPEATABLE READ and stop application writes.
    """
    if not connection.in_transaction():
        raise ValueError("source must be in an explicitly managed snapshot transaction")
    tables: dict[str, list[list[Any]]] = {}
    for table, columns in _COLUMNS.items():
        rows = connection.execute(text(f"SELECT {', '.join(columns)} FROM {table}")).mappings()
        output = []
        for row in rows:
            values = []
            for column in columns:
                value = row[column]
                if column in _DATE_FIELDS and value is not None:
                    value = _timestamp(value)
                elif column == "payload_json":
                    if isinstance(value, str):
                        value = json.loads(value)
                    if not isinstance(value, dict):
                        raise ValueError("source saved-view payload must be a JSON object")
                    value = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                elif table == "api_keys" and column == "key_hash" and (
                    not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
                ):
                    raise ValueError("source contains an invalid API-key hash")
                elif table == "api_keys" and column == "key_prefix" and (
                    not isinstance(value, str) or len(value) != 12 or not value.startswith("otui_")
                ):
                    raise ValueError("source contains an invalid API-key prefix")
                elif table == "api_keys" and column == "permissions" and value not in {"read", "read_write"}:
                    raise ValueError("source contains invalid API-key permissions")
                elif table == "api_keys" and column == "is_active" and value not in {0, 1}:
                    raise ValueError("source contains invalid API-key active state")
                elif column == "role":
                    value = str(value).upper()
                    if value not in {"ADMIN", "TRADER", "VIEWER"}:
                        raise ValueError("source contains an invalid user role")
                values.append(value)
            output.append(values)
        tables[table] = output
    return IdentitySnapshot(**tables)


def import_identity_snapshot(client: D1BatchClient, snapshot: IdentitySnapshot) -> dict[str, int]:
    """Atomically import into empty identity tables; never retry an unknown outcome.

    This deliberately uses one batch. Large snapshots that exceed the D1 client
    request limit fail before any write rather than risk a partial migration.
    """
    statements = [D1Statement(
        "SELECT CASE WHEN EXISTS(SELECT 1 FROM users) "
        "OR EXISTS(SELECT 1 FROM refresh_tokens) OR EXISTS(SELECT 1 FROM saved_views) "
        "OR EXISTS(SELECT 1 FROM api_keys) "
        "THEN json('target tables are not empty') ELSE 1 END"
    )]
    expected = {}
    for table, columns in _COLUMNS.items():
        rows = getattr(snapshot, table)
        expected[table] = len(rows)
        serialized = json.dumps(rows, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        fields = ", ".join(f"json_extract(value, '$[{i}]')" for i in range(len(columns)))
        statements.append(D1Statement(
            f"INSERT INTO {table} ({', '.join(columns)}) SELECT {fields} FROM json_each(?)", [serialized]
        ))
    results = client.batch(statements)
    for index, (table, count) in enumerate(expected.items(), start=1):
        meta = results[index].get("meta")
        if not isinstance(meta, dict) or meta.get("changes") != count:
            raise D1BatchOutcomeUnknown(f"D1 returned an unexpected {table} import result")
    return expected
