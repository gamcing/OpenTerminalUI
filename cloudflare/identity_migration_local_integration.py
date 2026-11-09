"""Exercise the offline identity copy against an isolated local D1 database."""
import json
from pathlib import Path

from sqlalchemy import create_engine

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement
from backend.cloudflare.identity_migration import IdentitySnapshot, import_identity_snapshot, read_identity_snapshot

client = D1BatchClient("http://127.0.0.1:8899", "test-secret-only-not-for-production-1234567890", allow_insecure_localhost=True)
schema = Path("cloudflare/migrations/0001_identity_saved_views.sql").read_text(encoding="utf-8")
client.batch([D1Statement(sql.strip()) for sql in schema.split(";") if sql.strip()])
client.batch([D1Statement("DELETE FROM users")])
engine = create_engine("sqlite:///:memory:")
with engine.connect() as source:
    for sql in schema.split(";"):
        if sql.strip():
            source.exec_driver_sql(sql)
    now = "2026-01-02 03:04:05.123456"
    source.exec_driver_sql("INSERT INTO users VALUES (?,?,?,?,?,?)", ("u1", "alice@example.test", "private-hash", "ADMIN", now, None))
    source.exec_driver_sql("INSERT INTO refresh_tokens VALUES (?,?,?,?,?,?)", ("t1", "u1", "secret-jti", now, now, None))
    payload = {"unicode": "条件😀", "nested": [True, {"n": 3}]}
    source.exec_driver_sql("INSERT INTO saved_views (id,user_id,name,scope,page,payload_json,description,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           ("v1", "u1", "view", "workspace", "/x", json.dumps(payload, ensure_ascii=False), "", now, now))
    source.commit()
    try:
        read_identity_snapshot(source)
    except ValueError:
        pass
    else:
        raise AssertionError("a source transaction must be explicit")
    source.exec_driver_sql("BEGIN")
    snapshot = read_identity_snapshot(source)
    assert "private-hash" not in repr(snapshot) and "secret-jti" not in repr(snapshot)
    assert import_identity_snapshot(client, snapshot) == {"users": 1, "refresh_tokens": 1, "saved_views": 1}
    rows = client.batch([
        D1Statement("SELECT * FROM users"), D1Statement("SELECT * FROM refresh_tokens"),
        D1Statement("SELECT * FROM saved_views"),
    ])
    assert rows[0]["results"][0]["id"] == "u1" and rows[0]["results"][0]["hashed_password"] == "private-hash"
    assert rows[1]["results"][0]["jti"] == "secret-jti" and rows[1]["results"][0]["user_id"] == "u1"
    assert json.loads(rows[2]["results"][0]["payload_json"]) == payload
    try:
        import_identity_snapshot(client, snapshot)
    except D1BatchOutcomeUnknown:
        pass
    else:
        raise AssertionError("an existing target must not be overwritten")
    assert client.batch([D1Statement("SELECT COUNT(*) AS n FROM users")])[0]["results"][0]["n"] == 1
    client.batch([D1Statement("DELETE FROM users")])
    # The third INSERT fails after the first two have run, rolling both back.
    client.batch([D1Statement("CREATE TRIGGER reject_views BEFORE INSERT ON saved_views BEGIN SELECT RAISE(ABORT, 'test'); END")])
    try:
        try:
            import_identity_snapshot(client, snapshot)
        except D1BatchOutcomeUnknown:
            pass
        else:
            raise AssertionError("trigger should abort the import")
    finally:
        client.batch([D1Statement("DROP TRIGGER reject_views")])
    results = client.batch([D1Statement("SELECT COUNT(*) AS n FROM " + table) for table in ("users", "refresh_tokens", "saved_views")])
    assert all(result["results"][0]["n"] == 0 for result in results), results
    # Oversized snapshots are rejected locally before contacting D1.
    oversized = IdentitySnapshot(users=[["u2", "b@example.test", "x" * (1024 * 1024), "VIEWER", now, None]],
                                 refresh_tokens=[], saved_views=[])
    try:
        import_identity_snapshot(client, oversized)
    except ValueError:
        pass
    else:
        raise AssertionError("oversized import should be rejected")
    assert client.batch([D1Statement("SELECT COUNT(*) AS n FROM users")])[0]["results"][0]["n"] == 0
    source.rollback()
engine.dispose()
print("D1 identity migration: data preservation, nonempty target and full rollback verified")
