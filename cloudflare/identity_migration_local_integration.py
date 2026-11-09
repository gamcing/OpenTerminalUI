"""Exercise the offline identity copy against an isolated local D1 database."""
import json
import hashlib
from pathlib import Path

from sqlalchemy import create_engine

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement
from backend.cloudflare.identity_migration import IdentitySnapshot, import_identity_snapshot, read_identity_snapshot

client = D1BatchClient("http://127.0.0.1:8899", "test-secret-only-not-for-production-1234567890", allow_insecure_localhost=True)
schema = "\n".join(Path("cloudflare/migrations", name).read_text(encoding="utf-8")
                   for name in ("0001_identity_saved_views.sql", "0004_api_keys.sql", "0006_watchlists.sql"))
schema += "\n" + Path("cloudflare/migrations/0008_user_layouts.sql").read_text(encoding="utf-8").split(";", 1)[1]
client.batch([D1Statement(sql.strip()) for sql in schema.split(";") if sql.strip()])
client.batch([D1Statement("DELETE FROM users"), D1Statement("DELETE FROM user_layouts")])
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
    existing_key = "otui_abcdefg" + "x" * 32
    existing_hash = hashlib.sha256(existing_key.encode()).hexdigest()
    source.exec_driver_sql("INSERT INTO api_keys (id,user_id,name,key_prefix,key_hash,permissions,is_active,last_used_at,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (7, "u1", "existing", existing_key[:12], existing_hash, "read_write", 1, None, now))
    source.exec_driver_sql("INSERT INTO watchlists (id,user_id,name,symbols_json,column_config_json,created_at) VALUES (?,?,?,?,?,?)",
                           ("w1", "u1", "existing list", '["AAPL","TSLA"]', '{"columns":["price"]}', now))
    source.exec_driver_sql("INSERT INTO user_layouts (id,user_key,layouts_json,updated_at) VALUES (?,?,?,?)",
                           ("l1", "anonymous", '[{"name":"layout"}]', now))
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
    assert import_identity_snapshot(client, snapshot) == {"users": 1, "refresh_tokens": 1, "saved_views": 1, "api_keys": 1, "watchlists": 1, "user_layouts": 1}
    rows = client.batch([
        D1Statement("SELECT * FROM users"), D1Statement("SELECT * FROM refresh_tokens"),
        D1Statement("SELECT * FROM saved_views"), D1Statement("SELECT * FROM api_keys"), D1Statement("SELECT * FROM watchlists"), D1Statement("SELECT * FROM user_layouts"),
    ])
    assert rows[0]["results"][0]["id"] == "u1" and rows[0]["results"][0]["hashed_password"] == "private-hash"
    assert rows[1]["results"][0]["jti"] == "secret-jti" and rows[1]["results"][0]["user_id"] == "u1"
    assert json.loads(rows[2]["results"][0]["payload_json"]) == payload
    assert rows[3]["results"][0]["id"] == 7 and rows[3]["results"][0]["key_hash"] == existing_hash
    assert json.loads(rows[4]["results"][0]["symbols_json"]) == ["AAPL", "TSLA"]
    assert rows[5]["results"][0]["user_key"] == "anonymous" and json.loads(rows[5]["results"][0]["layouts_json"]) == [{"name": "layout"}]
    from backend.cloudflare.api_keys import D1APIKeyRepository
    migrated = D1APIKeyRepository(client).authenticate(existing_key)
    assert migrated and migrated.user_id == "u1" and migrated.permissions == "read_write"
    generated = D1APIKeyRepository(client).create("u1", "new", "otui_1234567", "b" * 64, "read")
    assert generated and generated.id > 7
    try:
        import_identity_snapshot(client, snapshot)
    except D1BatchOutcomeUnknown:
        pass
    else:
        raise AssertionError("an existing target must not be overwritten")
    assert client.batch([D1Statement("SELECT COUNT(*) AS n FROM users")])[0]["results"][0]["n"] == 1
    client.batch([D1Statement("DELETE FROM users"), D1Statement("DELETE FROM user_layouts")])
    # The final INSERT fails after the earlier writes, rolling every table back.
    client.batch([D1Statement("CREATE TRIGGER reject_layouts BEFORE INSERT ON user_layouts BEGIN SELECT RAISE(ABORT, 'test'); END")])
    try:
        try:
            import_identity_snapshot(client, snapshot)
        except D1BatchOutcomeUnknown:
            pass
        else:
            raise AssertionError("trigger should abort the import")
    finally:
        client.batch([D1Statement("DROP TRIGGER reject_layouts")])
    results = client.batch([D1Statement("SELECT COUNT(*) AS n FROM " + table) for table in ("users", "refresh_tokens", "saved_views", "api_keys", "watchlists", "user_layouts")])
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
    # Legacy SQLAlchemy databases may lack D1's check constraints.
    source.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
    for column, invalid, valid in (("permissions", "admin", "read_write"), ("key_hash", "bad", existing_hash),
                                    ("key_prefix", "bad", existing_key[:12]), ("is_active", 2, 1)):
        source.exec_driver_sql(f"UPDATE api_keys SET {column}=? WHERE id=7", (invalid,))
        source.commit()
        source.exec_driver_sql("BEGIN")
        try:
            read_identity_snapshot(source)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid {column} must block migration")
        source.rollback()
        source.exec_driver_sql(f"UPDATE api_keys SET {column}=? WHERE id=7", (valid,))
        source.commit()
    for column, invalid, valid in (("symbols_json", '["AAPL",1]', '["AAPL","TSLA"]'),
                                    ("column_config_json", '[1]', '{"columns":["price"]}')):
        source.exec_driver_sql(f"UPDATE watchlists SET {column}=? WHERE id='w1'", (invalid,))
        source.commit()
        source.exec_driver_sql("BEGIN")
        try:
            read_identity_snapshot(source)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid {column} must block migration")
        source.rollback()
        source.exec_driver_sql(f"UPDATE watchlists SET {column}=? WHERE id='w1'", (valid,))
        source.commit()
    source.exec_driver_sql("UPDATE user_layouts SET layouts_json=? WHERE id='l1'", ('{"not":"an array"}',))
    source.commit()
    source.exec_driver_sql("BEGIN")
    try:
        read_identity_snapshot(source)
    except ValueError:
        pass
    else:
        raise AssertionError("non-array layouts must block migration")
    source.rollback()
engine.dispose()

# Layouts are independent of user foreign keys; refuse to overwrite even an anonymous layout.
client.batch([D1Statement("INSERT INTO user_layouts VALUES (?,?,?,?)", ["existing", "anonymous", "[]", now])])
try:
    import_identity_snapshot(client, snapshot)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("existing layout must block import")
client.batch([D1Statement("DELETE FROM user_layouts")])

print("D1 identity migration: data preservation, nonempty target, invalid layouts and full rollback verified")
