"""Experimental D1-backed saved-view repository.

The HTTP and MCP consumers can opt in explicitly. Identity must already exist
in the same D1 database; the complete application migration is not finished.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from backend.cloudflare.d1 import D1BatchClient, D1Statement


@dataclass(frozen=True)
class SavedViewRecord:
    id: str
    user_id: str
    name: str
    scope: str
    page: str
    payload_json: dict[str, Any]
    description: str
    created_at: datetime
    updated_at: datetime


def _record(row: dict[str, Any]) -> SavedViewRecord:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError("saved-view payload must be a JSON object")
    return SavedViewRecord(
        id=row["id"], user_id=row["user_id"], name=row["name"],
        scope=row["scope"], page=row["page"], payload_json=payload,
        description=row["description"], created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _json_payload(value: dict[str, Any]) -> str:
    if not isinstance(value, dict):
        raise ValueError("saved-view payload must be a JSON object")
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class D1SavedViewRepository:
    def __init__(self, client: D1BatchClient) -> None:
        self.client = client

    def _rows(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        result = self.client.batch([D1Statement(sql, params)])[0]
        rows = result.get("results")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("unexpected D1 result")
        return rows

    def list(self, user_id: str, *, scope: str | None = None, page: str | None = None) -> list[SavedViewRecord]:
        conditions = ["user_id = ?"]
        params: list[Any] = [user_id]
        if scope:
            conditions.append("scope = ?")
            params.append(scope)
        if page:
            conditions.append("page = ?")
            params.append(page)
        sql = "SELECT * FROM saved_views WHERE " + " AND ".join(conditions) + " ORDER BY updated_at DESC"
        return [_record(row) for row in self._rows(sql, params)]

    def get(self, user_id: str, view_id: str) -> SavedViewRecord | None:
        rows = self._rows("SELECT * FROM saved_views WHERE user_id = ? AND id = ?", [user_id, view_id])
        return _record(rows[0]) if rows else None

    def create(self, user_id: str, *, name: str, scope: str, page: str,
               payload: dict[str, Any], description: str = "") -> SavedViewRecord:
        now = _timestamp()
        params = [str(uuid4()), user_id, name.strip(), scope.strip() or "workspace", page.strip(),
                  _json_payload(payload), description, now, now]
        rows = self._rows(
            "INSERT INTO saved_views (id,user_id,name,scope,page,payload_json,description,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) RETURNING *", params,
        )
        if len(rows) != 1:
            raise ValueError("D1 did not return the created saved view")
        return _record(rows[0])

    def update(self, user_id: str, view_id: str, *, name: str | None = None, scope: str | None = None,
               page: str | None = None, payload: dict[str, Any] | None = None,
               description: str | None = None) -> SavedViewRecord | None:
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name.strip()
        if scope is not None:
            fields["scope"] = scope.strip() or "workspace"
        if page is not None:
            fields["page"] = page.strip()
        if payload is not None:
            fields["payload_json"] = _json_payload(payload)
        if description is not None:
            fields["description"] = description
        fields["updated_at"] = _timestamp()
        assignments = ", ".join(f"{field} = ?" for field in fields)
        rows = self._rows(
            f"UPDATE saved_views SET {assignments} WHERE user_id = ? AND id = ? RETURNING *",
            [*fields.values(), user_id, view_id],
        )
        return _record(rows[0]) if rows else None

    def delete(self, user_id: str, view_id: str) -> bool:
        return bool(self._rows("DELETE FROM saved_views WHERE user_id = ? AND id = ? RETURNING id", [user_id, view_id]))
