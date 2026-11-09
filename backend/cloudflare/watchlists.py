"""Experimental D1 watchlists, scoped to the isolated D1 identity mode."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement


@dataclass(frozen=True)
class WatchlistRecord:
    id: str
    user_id: str
    name: str
    symbols_json: list[str]
    column_config_json: dict[str, Any]
    created_at: datetime


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _record(row: dict[str, Any]) -> WatchlistRecord:
    symbols = json.loads(row["symbols_json"])
    config = json.loads(row["column_config_json"])
    if not isinstance(symbols, list) or any(not isinstance(item, str) for item in symbols) or not isinstance(config, dict):
        raise ValueError("invalid watchlist data in D1")
    return WatchlistRecord(id=row["id"], user_id=row["user_id"], name=row["name"], symbols_json=symbols,
                           column_config_json=config, created_at=datetime.fromisoformat(row["created_at"]))


def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = result.get("results")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise D1BatchOutcomeUnknown("D1 returned an unexpected watchlist response")
    return rows


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


class D1WatchlistRepository:
    def __init__(self, client: D1BatchClient) -> None:
        self.client = client

    def list(self, user_id: str) -> list[WatchlistRecord]:
        rows = _rows(self.client.batch([D1Statement(
            "SELECT * FROM watchlists WHERE user_id=? ORDER BY created_at,id", [user_id],
        )])[0])
        return [_record(row) for row in rows]

    def list_or_create_default(self, user_id: str) -> list[WatchlistRecord]:
        results = self.client.batch([
            D1Statement("INSERT INTO watchlists (id,user_id,name,symbols_json,column_config_json,created_at) "
                        "SELECT ?,id,?,?,?,? FROM users WHERE id=? AND NOT EXISTS "
                        "(SELECT 1 FROM watchlists WHERE user_id=?) RETURNING id",
                        [str(uuid4()), "Default Watchlist", _json(["AAPL", "MSFT", "TSLA", "GOOGL"]),
                         _json({}), _now(), user_id, user_id]),
            D1Statement("SELECT * FROM watchlists WHERE user_id=? ORDER BY created_at,id", [user_id]),
        ])
        if len(_rows(results[0])) > 1:
            raise D1BatchOutcomeUnknown("D1 returned inconsistent default-watchlist results")
        return [_record(row) for row in _rows(results[1])]

    def get(self, user_id: str, watchlist_id: str) -> WatchlistRecord | None:
        rows = _rows(self.client.batch([D1Statement("SELECT * FROM watchlists WHERE id=? AND user_id=?",
                                                    [watchlist_id, user_id])])[0])
        return _record(rows[0]) if rows else None

    def create(self, user_id: str, name: str, symbols: list[str], config: dict) -> WatchlistRecord | None:
        rows = _rows(self.client.batch([D1Statement(
            "INSERT INTO watchlists (id,user_id,name,symbols_json,column_config_json,created_at) "
            "SELECT ?,id,?,?,?,? FROM users WHERE id=? RETURNING *",
            [str(uuid4()), name, _json(symbols), _json(config), _now(), user_id],
        )])[0])
        return _record(rows[0]) if rows else None

    def update(self, user_id: str, watchlist_id: str, *, name: str | None = None,
               symbols: list[str] | None = None, config: dict | None = None) -> WatchlistRecord | None:
        changes: dict[str, Any] = {}
        if name is not None:
            changes["name"] = name
        if symbols is not None:
            changes["symbols_json"] = _json(symbols)
        if config is not None:
            changes["column_config_json"] = _json(config)
        assignments = ", ".join(f"{field}=?" for field in changes) if changes else "name=name"
        rows = _rows(self.client.batch([D1Statement(
            f"UPDATE watchlists SET {assignments} WHERE id=? AND user_id=? RETURNING *",
            [*changes.values(), watchlist_id, user_id],
        )])[0])
        return _record(rows[0]) if rows else None

    def delete(self, user_id: str, watchlist_id: str) -> bool:
        rows = _rows(self.client.batch([D1Statement("DELETE FROM watchlists WHERE id=? AND user_id=? RETURNING id",
                                                    [watchlist_id, user_id])])[0])
        return bool(rows)

    def add_symbols(self, user_id: str, watchlist_id: str, symbols: list[str]) -> WatchlistRecord | None:
        # Compute the append inside the UPDATE so concurrent writers cannot lose
        # each other's additions. Existing order and duplicates are preserved.
        incoming = [item.strip().upper() for item in symbols]
        rows = _rows(self.client.batch([D1Statement(
            "UPDATE watchlists SET symbols_json=(SELECT json_group_array(value) FROM ("
            "SELECT value,CAST(key AS INTEGER) AS pos,0 AS source FROM json_each(watchlists.symbols_json) "
            "UNION ALL SELECT incoming.value,MIN(CAST(incoming.key AS INTEGER)),1 FROM json_each(?) AS incoming "
            "WHERE NOT EXISTS (SELECT 1 FROM json_each(watchlists.symbols_json) AS old WHERE old.value=incoming.value) "
            "GROUP BY incoming.value ORDER BY source,pos)) WHERE id=? AND user_id=? RETURNING *",
            [_json(incoming), watchlist_id, user_id],
        )])[0])
        return _record(rows[0]) if rows else None

    def remove_symbol(self, user_id: str, watchlist_id: str, symbol: str) -> WatchlistRecord | None:
        rows = _rows(self.client.batch([D1Statement(
            "UPDATE watchlists SET symbols_json=(SELECT json_group_array(value) FROM json_each(watchlists.symbols_json) "
            "WHERE value<>?) WHERE id=? AND user_id=? RETURNING *", [symbol.strip().upper(), watchlist_id, user_id],
        )])[0])
        return _record(rows[0]) if rows else None
