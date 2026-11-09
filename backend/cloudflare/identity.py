"""D1 identity operations with atomic, single-use refresh-token rotation.

This repository is not wired into login or authorization yet. Other application
models still depend on users in the SQLAlchemy database.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement


@dataclass(frozen=True)
class IdentityUser:
    id: str
    email: str
    hashed_password: str
    role: str
    created_at: datetime
    last_login: datetime | None


def _timestamp(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(sep=" ", timespec="microseconds")


def _user(row: dict[str, Any]) -> IdentityUser:
    role = str(row["role"]).upper()
    if role not in {"ADMIN", "TRADER", "VIEWER"}:
        raise ValueError("unknown user role in D1")
    return IdentityUser(
        id=row["id"], email=row["email"], hashed_password=row["hashed_password"], role=role.lower(),
        created_at=datetime.fromisoformat(row["created_at"]),
        last_login=datetime.fromisoformat(row["last_login"]) if row["last_login"] else None,
    )


def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = result.get("results")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise D1BatchOutcomeUnknown("D1 returned an unexpected identity response")
    return rows


class D1IdentityRepository:
    def __init__(self, client: D1BatchClient) -> None:
        self.client = client

    def create_user(self, email: str, hashed_password: str, *, role: str = "viewer") -> IdentityUser | None:
        normalized_role = role.strip().upper()
        if normalized_role not in {"ADMIN", "TRADER", "VIEWER"}:
            raise ValueError("invalid user role")
        now = _timestamp(datetime.now(timezone.utc))
        result = self.client.batch([D1Statement(
            "INSERT INTO users (id,email,hashed_password,role,created_at,last_login) VALUES (?,?,?,?,?,NULL) "
            "ON CONFLICT(email) DO NOTHING RETURNING *",
            [str(uuid4()), email.strip().lower(), hashed_password, normalized_role, now],
        )])[0]
        rows = _rows(result)
        return _user(rows[0]) if rows else None

    def get_by_id(self, user_id: str) -> IdentityUser | None:
        rows = _rows(self.client.batch([D1Statement("SELECT * FROM users WHERE id = ?", [user_id])])[0])
        return _user(rows[0]) if rows else None

    def get_by_email(self, email: str) -> IdentityUser | None:
        rows = _rows(self.client.batch([D1Statement("SELECT * FROM users WHERE email = ?", [email.strip().lower()])])[0])
        return _user(rows[0]) if rows else None

    def seed_first_admin(self, email: str, hashed_password: str) -> IdentityUser | None:
        now = _timestamp(datetime.now(timezone.utc))
        result = self.client.batch([D1Statement(
            "INSERT INTO users (id,email,hashed_password,role,created_at,last_login) "
            "SELECT ?,?,?,?, ?,NULL WHERE NOT EXISTS (SELECT 1 FROM users) RETURNING *",
            [str(uuid4()), email.strip().lower(), hashed_password, "ADMIN", now],
        )])[0]
        rows = _rows(result)
        return _user(rows[0]) if rows else None

    def record_login_and_issue_refresh(self, user_id: str, jti: str, expires_at: datetime) -> bool:
        now = _timestamp(datetime.now(timezone.utc))
        results = self.client.batch([
            D1Statement("UPDATE users SET last_login = ? WHERE id = ? RETURNING id", [now, user_id]),
            D1Statement(
                "INSERT INTO refresh_tokens (id,user_id,jti,expires_at,created_at,revoked_at) "
                "SELECT ?,id,?,?,?,NULL FROM users WHERE id = ? RETURNING user_id",
                [str(uuid4()), jti, _timestamp(expires_at), now, user_id],
            ),
        ])
        updated, inserted = (_rows(result) for result in results)
        if len(updated) != len(inserted) or len(updated) > 1:
            raise D1BatchOutcomeUnknown("D1 returned inconsistent login results")
        return bool(inserted)

    def rotate_refresh(self, user_id: str, old_jti: str, new_jti: str, expires_at: datetime,
                       *, now: datetime | None = None) -> bool:
        current = _timestamp(now or datetime.now(timezone.utc))
        results = self.client.batch([
            D1Statement(
                "INSERT INTO refresh_tokens (id,user_id,jti,expires_at,created_at,revoked_at) "
                "SELECT ?,user_id,?,?,?,NULL FROM refresh_tokens "
                "WHERE user_id = ? AND jti = ? AND revoked_at IS NULL AND expires_at > ? RETURNING user_id",
                [str(uuid4()), new_jti, _timestamp(expires_at), current, user_id, old_jti, current],
            ),
            D1Statement(
                "UPDATE refresh_tokens SET revoked_at = ? "
                "WHERE user_id = ? AND jti = ? AND revoked_at IS NULL AND expires_at > ? RETURNING user_id",
                [current, user_id, old_jti, current],
            ),
        ])
        inserted, revoked = (_rows(result) for result in results)
        if len(inserted) != len(revoked) or len(inserted) > 1:
            raise D1BatchOutcomeUnknown("D1 returned inconsistent token rotation results")
        return bool(inserted)
