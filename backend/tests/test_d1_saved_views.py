from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.cloudflare.saved_views import D1SavedViewRepository
from backend.saved_views.d1_storage import get_d1_saved_views


class SQLiteBatchClient:
    """Exercise repository SQL against SQLite; real D1 is tested separately."""

    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        migration = Path(__file__).resolve().parents[2] / "cloudflare" / "migrations" / "0001_identity_saved_views.sql"
        self.db.executescript(migration.read_text(encoding="utf-8"))
        self.db.executemany(
            "INSERT INTO users (id,email,hashed_password,role,created_at) VALUES (?,?,?,?,?)",
            [(user, f"{user}@example.test", "hash", "VIEWER", "2026-01-01 00:00:00") for user in ("alice", "bob")],
        )
        self.db.commit()

    def batch(self, statements):
        results = []
        with self.db:
            for statement in statements:
                cursor = self.db.execute(statement.sql, statement.params)
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                results.append({"success": True, "results": rows})
        return results


def test_saved_views_user_isolation_and_cascade() -> None:
    client = SQLiteBatchClient()
    repository = D1SavedViewRepository(client)
    alice = repository.create("alice", name=" A ", scope=" ", page=" /home ", payload={"k": "值"})
    bob = repository.create("bob", name="B", scope="scanner", page="/scanner", payload={})
    assert alice.name == "A" and alice.scope == "workspace" and alice.page == "/home"
    assert repository.get("bob", alice.id) is None
    assert repository.get("alice", bob.id) is None
    assert [row.id for row in repository.list("alice")] == [alice.id]
    assert repository.list("alice", scope="scanner") == []
    assert repository.update("bob", alice.id, name="stolen") is None
    assert repository.delete("bob", alice.id) is False
    updated = repository.update("alice", alice.id, payload={}, description="updated")
    assert updated is not None and updated.payload_json == {} and updated.description == "updated"
    assert repository.delete("alice", alice.id) is True
    with pytest.raises(sqlite3.IntegrityError):
        repository.create("missing", name="orphan", scope="workspace", page="/", payload={})
    with client.db:
        client.db.execute("DELETE FROM users WHERE id = ?", ("bob",))
    assert repository.get("bob", bob.id) is None


def test_d1_is_opt_in_and_invalid_configuration_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("OPENTERMINALUI_SAVED_VIEWS_STORE", raising=False)
    assert get_d1_saved_views() is None
    monkeypatch.setenv("OPENTERMINALUI_SAVED_VIEWS_STORE", "typo")
    with pytest.raises(RuntimeError):
        get_d1_saved_views()
    monkeypatch.setenv("OPENTERMINALUI_SAVED_VIEWS_STORE", "d1")
    monkeypatch.delenv("D1_GATEWAY_URL", raising=False)
    monkeypatch.delenv("D1_BRIDGE_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        get_d1_saved_views()


def test_d1_local_gateway_requires_explicit_development_mode(monkeypatch) -> None:
    monkeypatch.setenv("OPENTERMINALUI_SAVED_VIEWS_STORE", "d1")
    monkeypatch.setenv("D1_GATEWAY_URL", "http://127.0.0.1:8899")
    monkeypatch.setenv("D1_BRIDGE_TOKEN", "test-secret-only-not-for-production-1234567890")
    monkeypatch.setenv("OPENTERMINALUI_D1_LOCAL_TEST", "1")
    monkeypatch.setenv("OPENTERMINALUI_ENV", "testing")
    assert get_d1_saved_views() is not None
    monkeypatch.setenv("OPENTERMINALUI_ENV", "production")
    with pytest.raises(RuntimeError):
        get_d1_saved_views()


def test_mcp_reports_d1_failure_without_leaking_details(monkeypatch) -> None:
    from backend.cloudflare.d1 import D1BatchOutcomeUnknown
    from backend.mcp import resources

    class Unavailable:
        def list(self, user_id):
            raise D1BatchOutcomeUnknown("internal details")

    monkeypatch.setattr(resources, "get_d1_saved_views", lambda: Unavailable())
    result = resources.read_resource("otui://saved-view", "alice")
    assert result["ok"] is False
    assert result["error"]["code"] == "storage_unavailable"
    assert "internal details" not in str(result)
