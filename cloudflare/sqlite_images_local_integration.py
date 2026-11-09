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
schema = (Path("cloudflare/migrations/0009_sqlite_image_storage.sql").read_text(encoding="utf-8")
          + Path("cloudflare/migrations/0010_sqlite_chunk_pool.sql").read_text(encoding="utf-8"))
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

# Reuse identical chunks within one database, and upload only changed chunks.
dedup_key = name + "-dedup"
class CountingClient:
    def __init__(self): self.uploads = 0
    def batch(self, statements):
        self.uploads += sum("INSERT INTO cf_sqlite_chunk_pool" in statement.sql for statement in statements)
        return client.batch(statements)
counter = CountingClient()
dedup = D1SQLiteImageStore(counter)
first = str(uuid4())
dedup.stage(dedup_key, 0, first, original.data)
first_uploads = counter.uploads
assert first_uploads > 1
assert dedup.publish(dedup_key, 0, first) == 1
identical = str(uuid4())
dedup.stage(dedup_key, 1, identical, original.data)
assert counter.uploads == first_uploads
assert dedup.publish(dedup_key, 1, identical) == 2
changed_db = sqlite3.connect(":memory:")
changed_db.deserialize(original.data)
changed_db.execute("UPDATE entries SET value='changed' WHERE id=1")
changed_db.commit()
changed_image = capture_sqlite_image(changed_db)
changed_db.close()
changed = str(uuid4())
dedup.stage(dedup_key, 2, changed, changed_image)
assert 0 < counter.uploads - first_uploads < first_uploads
assert dedup.publish(dedup_key, 2, changed) == 3
assert dedup.head(dedup_key).data == changed_image

# A separate logical database cannot reuse another database's pooled chunks.
other_counter = CountingClient()
D1SQLiteImageStore(other_counter).stage(name + "-separate", 0, str(uuid4()), original.data)
assert other_counter.uploads == first_uploads

# Concurrent stages can race to insert the same new pooled content.
race_key = name + "-pool-race"
race_ids = [str(uuid4()), str(uuid4())]
with ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(lambda run: store.stage(race_key, 0, run, original.data), race_ids))
assert store.publish(race_key, 0, race_ids[0]) == 1
assert store.head(race_key).data == original.data
store.abort(race_ids[1])

# Existing 0009 inline images remain readable and publishable.
legacy_key, legacy_id = name + "-legacy", str(uuid4())
from backend.cloudflare.sqlite_images import _CHUNK_SIZE
legacy_chunks = [original.data[i:i + _CHUNK_SIZE] for i in range(0, len(original.data), _CHUNK_SIZE)]
client.batch([D1Statement(
    "INSERT INTO cf_sqlite_images (id,database_name,parent_revision,byte_count,chunk_count,sha256) VALUES (?,?,?,?,?,?)",
    [legacy_id, legacy_key, 0, len(original.data), len(legacy_chunks), hashlib.sha256(original.data).hexdigest()],
)])
for ordinal, chunk in enumerate(legacy_chunks):
    client.batch([D1Statement("INSERT INTO cf_sqlite_image_chunks VALUES (?,?,?)",
                              [legacy_id, ordinal, base64.b64encode(chunk).decode("ascii")])])
assert store.publish(legacy_key, 0, legacy_id) == 1
assert store.head(legacy_key).data == original.data

pool_row = client.batch([D1Statement(
    "SELECT sha256 FROM cf_sqlite_chunk_pool WHERE database_name=? LIMIT 1", [dedup_key],
)])[0]["results"][0]
for statement in (
    D1Statement("UPDATE cf_sqlite_chunk_pool SET data_base64='bad' WHERE database_name=? AND sha256=?",
                [dedup_key, pool_row["sha256"]]),
    D1Statement("DELETE FROM cf_sqlite_chunk_pool WHERE database_name=? AND sha256=?",
                [dedup_key, pool_row["sha256"]]),
    D1Statement("INSERT OR REPLACE INTO cf_sqlite_chunk_pool VALUES (?,?,?)",
                [dedup_key, pool_row["sha256"], "bad"]),
):
    try:
        client.batch([statement])
    except D1BatchOutcomeUnknown:
        pass
    else:
        raise AssertionError("pooled chunks must be immutable")
assert dedup.head(dedup_key).data == changed_image

# A missing pool reference must not become a published image.
bad_id = str(uuid4())
client.batch([
    D1Statement("INSERT INTO cf_sqlite_images (id,database_name,parent_revision,byte_count,chunk_count,sha256) VALUES (?,?,?,?,?,?)",
                [bad_id, dedup_key, 3, len(changed_image), len(legacy_chunks), hashlib.sha256(changed_image).hexdigest()]),
    D1Statement("INSERT INTO cf_sqlite_image_chunks VALUES (?,?,?)", [bad_id, 0, "@" + "f" * 64]),
])
try:
    store.publish(dedup_key, 3, bad_id)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("missing pooled chunk must fail")
store.abort(bad_id)
assert dedup.head(dedup_key).data == changed_image

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
