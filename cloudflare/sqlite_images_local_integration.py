"""Test an experimental D1 SQLite image store against disposable local D1."""
import base64
import hashlib
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement
from backend.cloudflare.sqlite_images import D1SQLiteImageStore, capture_sqlite_image

client = D1BatchClient("http://127.0.0.1:8899", "test-secret-only-not-for-production-1234567890", allow_insecure_localhost=True)
schema = Path("cloudflare/migrations/0009_sqlite_image_storage.sql").read_text(encoding="utf-8")
statements, pending = [], ""
for line in schema.splitlines(keepends=True):
    pending += line
    if sqlite3.complete_statement(pending):
        statements.append(D1Statement(pending.strip()))
        pending = ""
assert not pending.strip()
client.batch(statements)
store = D1SQLiteImageStore(client)
name = "integration-" + str(uuid4())
assert store.head(name) is None

with tempfile.TemporaryDirectory() as temp:
    for mode in ("DELETE", "WAL"):
        db = sqlite3.connect(str(Path(temp) / (mode + ".sqlite")))
        db.execute("PRAGMA journal_mode=" + mode)
        db.execute("PRAGMA cache_size=10")
        db.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT)")
        db.commit()
        # Large, uncommitted changes and DDL exercise cache spills and WAL.
        db.execute("BEGIN")
        db.executemany("INSERT INTO entries(value) VALUES (?)", [("private-" + str(i) + "x" * 2048,) for i in range(600)])
        db.execute("CREATE TABLE additional(x)")
        db.execute("INSERT INTO additional VALUES (42)")
        image = capture_sqlite_image(db)
        db.rollback()
        assert db.execute("SELECT COUNT(*) FROM entries").fetchone() == (0,)
        staged = str(uuid4())
        key = name + "-" + mode
        store.stage(key, 0, staged, image)
        assert store.head(key) is None
        assert store.publish(key, 0, staged) == 1
        saved = store.head(key)
        assert saved and saved.revision == 1 and saved.image_id == staged and saved.data == image
        assert "private-" not in repr(saved)
        restored = sqlite3.connect(":memory:")
        restored.deserialize(saved.data)
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT COUNT(*) FROM entries").fetchone() == (600,)
        assert restored.execute("SELECT * FROM additional").fetchall() == [(42,)]
        restored.close()
        db.close()

key = name + "-WAL"
original = store.head(key)
assert original
# Two complete snapshots of the same base revision must have one winner.
runs = [str(uuid4()), str(uuid4())]
for run in runs:
    store.stage(key, original.revision, run, original.data)
def publish(run):
    try:
        return store.publish(key, original.revision, run)
    except D1BatchOutcomeUnknown:
        return None
with ThreadPoolExecutor(max_workers=2) as pool:
    assert sorted(pool.map(publish, runs), key=lambda x: x or 0) == [None, 2]
current = store.head(key)
assert current and current.revision == 2 and current.image_id in runs
for run in runs:
    store.abort(run)
assert store.head(key) == current

# Unknown publish results are resolved by reading the durable head, not retrying.
run = str(uuid4())
store.stage(key, current.revision, run, current.data)
class LostResponse:
    def batch(self, statements):
        result = client.batch(statements)
        if any("INSERT INTO cf_sqlite_heads" in statement.sql for statement in statements):
            raise D1BatchOutcomeUnknown("simulated lost response")
        return result
try:
    D1SQLiteImageStore(LostResponse()).publish(key, current.revision, run)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("lost response must remain unknown")
after = store.head(key)
assert after and after.image_id == run and after.revision == 3
store.abort(run)
assert store.head(key) == after

# An incomplete upload cannot be published, and deleting the staging parent removes chunks.
run = str(uuid4())
class Interrupted:
    def __init__(self): self.writes = 0
    def batch(self, statements):
        if "INSERT INTO cf_sqlite_image_chunks" in statements[0].sql:
            self.writes += 1
            if self.writes == 2: raise D1BatchOutcomeUnknown("simulated interruption")
        return client.batch(statements)
try:
    D1SQLiteImageStore(Interrupted()).stage(key, after.revision, run, after.data)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("interruption must fail")
try:
    store.publish(key, after.revision, run)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("incomplete image must not publish")
store.abort(run)
assert client.batch([D1Statement("SELECT COUNT(*) AS n FROM cf_sqlite_image_chunks WHERE image_id=?", [run])])[0]["results"][0]["n"] == 0
assert store.head(key) == after

# A complete staged payload with matching digest must still be a valid SQLite image.
run = str(uuid4())
invalid = b"not a valid sqlite database" * 5
client.batch([
    D1Statement("INSERT INTO cf_sqlite_images (id,database_name,parent_revision,byte_count,chunk_count,sha256) VALUES (?,?,?,?,?,?)",
                [run, key, after.revision, len(invalid), 1, hashlib.sha256(invalid).hexdigest()]),
    D1Statement("INSERT INTO cf_sqlite_image_chunks VALUES (?,?,?)", [run, 0, base64.b64encode(invalid).decode("ascii")]),
])
try:
    store.publish(key, after.revision, run)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("invalid SQLite image must not publish")
store.abort(run)
assert store.head(key) == after

# Staged and published bytes cannot be modified or replaced.
for statement in (
    D1Statement("UPDATE cf_sqlite_image_chunks SET data_base64='bad' WHERE image_id=?", [after.image_id]),
    D1Statement("DELETE FROM cf_sqlite_image_chunks WHERE image_id=?", [after.image_id]),
    D1Statement("INSERT OR REPLACE INTO cf_sqlite_image_chunks VALUES (?,?,?)", [after.image_id, 0, "bad"]),
    D1Statement("DELETE FROM cf_sqlite_images WHERE id=?", [after.image_id]),
    D1Statement("INSERT OR REPLACE INTO cf_sqlite_images (id,database_name,parent_revision,byte_count,chunk_count,sha256) VALUES (?,?,?,?,?,?)",
                [after.image_id, key, 0, 100, 1, "a" * 64]),
):
    try:
        client.batch([statement])
    except D1BatchOutcomeUnknown:
        pass
    else:
        raise AssertionError("published image must be immutable")
assert store.head(key) == after
print("D1 SQLite images: WAL and rollback snapshots, recovery, conflicts, lost responses and interruption verified")
