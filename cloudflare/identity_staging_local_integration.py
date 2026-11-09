"""Exercise staged D1 identity imports against an isolated local database."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement
from backend.cloudflare.identity_migration import IdentitySnapshot
from backend.cloudflare.identity_staging import abort_identity_snapshot, finalize_identity_snapshot, stage_identity_snapshot

client = D1BatchClient("http://127.0.0.1:8899", "test-secret-only-not-for-production-1234567890", allow_insecure_localhost=True)
for migration in ("0001_identity_saved_views.sql", "0002_identity_import_staging.sql"):
    schema = Path("cloudflare/migrations", migration).read_text(encoding="utf-8")
    client.batch([D1Statement(sql.strip()) for sql in schema.split(";") if sql.strip()])
client.batch([D1Statement("DELETE FROM users"), D1Statement("DELETE FROM cf_identity_import_runs")])
now = "2026-01-02 03:04:05.123456"
user = ["u1", "alice@example.test", "hash", "ADMIN", now, None]
token = ["t1", "u1", "secret-jti", now, now, None]
# Oversized individual rows are rejected before staging is created.
large_row = IdentitySnapshot(users=[user, ["u2", "b@example.test", "x" * (800 * 1024), "VIEWER", now, None]],
                             refresh_tokens=[], saved_views=[])
rejected_run = str(uuid4())
try:
    stage_identity_snapshot(client, rejected_run, large_row)
except ValueError:
    pass
else:
    raise AssertionError("an oversized row must be rejected")
assert client.batch([D1Statement("SELECT COUNT(*) AS n FROM cf_identity_import_runs WHERE id=?", [rejected_run])])[0]["results"][0]["n"] == 0

# More than 1 MiB in aggregate, with non-ASCII content and special characters.
payload = json.dumps({"text": "条件\\\"😀" * 800}, ensure_ascii=False)
views = [[f"v{i}", "u1", f"view {i}", "workspace", "/x", payload, "", now, now] for i in range(150)]
assert sum(len(json.dumps(row, ensure_ascii=False).encode("utf-8")) for row in views) > 1024 * 1024
snapshot = IdentitySnapshot(users=[user], refresh_tokens=[token], saved_views=views)
run = str(uuid4())
assert stage_identity_snapshot(client, run, snapshot) == {"users": 1, "refresh_tokens": 1, "saved_views": len(views)}
results = client.batch([D1Statement("SELECT COUNT(*) AS n FROM " + table) for table in ("users", "refresh_tokens", "saved_views")])
assert all(result["results"][0]["n"] == 0 for result in results)
assert finalize_identity_snapshot(client, run, snapshot)["saved_views"] == len(views)
rows = client.batch([D1Statement("SELECT payload_json FROM saved_views WHERE id = ?", ["v149"]),
                     D1Statement("SELECT COUNT(*) AS n FROM cf_identity_import_rows")])
assert json.loads(rows[0]["results"][0]["payload_json"]) == json.loads(payload)
assert rows[1]["results"][0]["n"] == 0

# A non-empty target cannot be overwritten, even by a fresh run.
try:
    stage_identity_snapshot(client, str(uuid4()), snapshot)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("staging must reject a nonempty target")
client.batch([D1Statement("DELETE FROM users")])
small = IdentitySnapshot(users=[user], refresh_tokens=[token], saved_views=[views[0]])

# Missing staging records and changed source snapshots must fail before cutover.
run = str(uuid4())
stage_identity_snapshot(client, run, small)
client.batch([D1Statement("DELETE FROM cf_identity_import_rows WHERE run_id=? AND table_name='saved_views'", [run])])
try:
    finalize_identity_snapshot(client, run, small)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("incomplete staging must not be finalized")
assert client.batch([D1Statement("SELECT COUNT(*) AS n FROM users")])[0]["results"][0]["n"] == 0
abort_identity_snapshot(client, run)
run = str(uuid4())
stage_identity_snapshot(client, run, small)
changed = IdentitySnapshot(users=[user], refresh_tokens=[token], saved_views=[["changed", *views[0][1:]]])
try:
    finalize_identity_snapshot(client, run, changed)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("a different source snapshot must not be finalized")
abort_identity_snapshot(client, run)

# A later insert failing rolls back both earlier application-table inserts and cleanup.
run = str(uuid4())
stage_identity_snapshot(client, run, small)
client.batch([D1Statement("CREATE TRIGGER reject_staged_views BEFORE INSERT ON saved_views BEGIN SELECT RAISE(ABORT, 'test'); END")])
try:
    try:
        finalize_identity_snapshot(client, run, small)
    except D1BatchOutcomeUnknown:
        pass
    else:
        raise AssertionError("trigger should abort the final transaction")
finally:
    client.batch([D1Statement("DROP TRIGGER reject_staged_views")])
results = client.batch([D1Statement("SELECT COUNT(*) AS n FROM " + table) for table in ("users", "refresh_tokens", "saved_views")])
assert all(result["results"][0]["n"] == 0 for result in results)
assert client.batch([D1Statement("SELECT COUNT(*) AS n FROM cf_identity_import_runs WHERE id=?", [run])])[0]["results"][0]["n"] == 1
abort_identity_snapshot(client, run)

# Concurrent cutovers can create only one complete copy.
runs = [str(uuid4()), str(uuid4())]
for run in runs:
    stage_identity_snapshot(client, run, small)
def finalize(run):
    try:
        finalize_identity_snapshot(client, run, small)
        return True
    except D1BatchOutcomeUnknown:
        return False
with ThreadPoolExecutor(max_workers=2) as pool:
    assert sorted(pool.map(finalize, runs)) == [False, True]
results = client.batch([D1Statement("SELECT COUNT(*) AS n FROM " + table) for table in ("users", "refresh_tokens", "saved_views")])
assert [item["results"][0]["n"] for item in results] == [1, 1, 1]
for run in runs:
    abort_identity_snapshot(client, run)
assert client.batch([D1Statement("SELECT COUNT(*) AS n FROM cf_identity_import_rows")])[0]["results"][0]["n"] == 0
print("D1 staged import: large snapshot, validation, rollback and concurrent cutover verified")
