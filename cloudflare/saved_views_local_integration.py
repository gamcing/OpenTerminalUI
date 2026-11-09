"""Integration test using only a disposable local Wrangler D1 database."""
from pathlib import Path

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement
from backend.cloudflare.saved_views import D1SavedViewRepository

client = D1BatchClient(
    "http://127.0.0.1:8899",
    "test-secret-only-not-for-production-1234567890",
    allow_insecure_localhost=True,
)
source = Path("cloudflare/migrations/0001_identity_saved_views.sql").read_text(encoding="utf-8")
statements = [D1Statement(sql.strip()) for sql in source.split(";") if sql.strip()]
client.batch(statements)
client.batch([D1Statement("DELETE FROM users")])
client.batch([
    D1Statement("INSERT INTO users (id,email,hashed_password,role,created_at) VALUES (?,?,?,?,?)",
                ["alice", "alice@example.test", "hash-a", "VIEWER", "2026-11-09 00:00:00"]),
    D1Statement("INSERT INTO users (id,email,hashed_password,role,created_at) VALUES (?,?,?,?,?)",
                ["bob", "bob@example.test", "hash-b", "VIEWER", "2026-11-09 00:00:00"]),
])
repository = D1SavedViewRepository(client)
alice = repository.create("alice", name="  My view  ", scope=" ", page=" /screener ",
                          payload={"条件": [1, 2], "x": True}, description="example")
bob = repository.create("bob", name="Bob", scope="scanner", page="/scanner", payload={})
assert alice.name == "My view" and alice.scope == "workspace" and alice.page == "/screener"
assert repository.get("alice", alice.id).payload_json == {"条件": [1, 2], "x": True}
assert repository.get("bob", alice.id) is None
assert repository.get("alice", bob.id) is None
assert [view.id for view in repository.list("alice")] == [alice.id]
assert repository.list("alice", scope="scanner") == []
assert repository.update("bob", alice.id, name="stolen") is None
assert not repository.delete("bob", alice.id)
updated = repository.update("alice", alice.id, name=" New ", payload={}, description="changed")
assert updated.name == "New" and updated.payload_json == {} and updated.description == "changed"
try:
    repository.create("nonexistent", name="orphan", scope="workspace", page="/x", payload={})
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("foreign key must reject a saved view without a user")
assert repository.delete("alice", alice.id)
assert not repository.delete("alice", alice.id)
client.batch([D1Statement("DELETE FROM users WHERE id = ?", ["bob"])])
assert repository.get("bob", bob.id) is None, "deleting a user must cascade to their saved views"
print("D1 saved views: CRUD, scoping, JSON, missing users and cascade verified")
