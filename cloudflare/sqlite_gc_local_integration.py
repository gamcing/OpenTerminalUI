"""Exercise bounded snapshot collection with concurrent D1 readers and writers."""
import sqlite3
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement
from backend.cloudflare.sqlite_images import D1SQLiteImageStore, capture_sqlite_image

client = D1BatchClient("http://127.0.0.1:8899", "test-secret-only-not-for-production-1234567890", allow_insecure_localhost=True)
schema = "".join(Path("cloudflare/migrations/" + filename).read_text(encoding="utf-8") for filename in (
    "0009_sqlite_image_storage.sql", "0010_sqlite_chunk_pool.sql", "0011_sqlite_snapshot_gc.sql",
))
statements, pending = [], ""
for line in schema.splitlines(keepends=True):
    pending += line
    if sqlite3.complete_statement(pending):
        statements.append(D1Statement(pending.strip()))
        pending = ""
assert not pending.strip()
client.batch(statements)
store = D1SQLiteImageStore(client)
key = "gc-" + str(uuid4())

db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT)")
db.executemany("INSERT INTO entries VALUES (?,?)", [(i, "private-" + str(i) + "x" * 2048) for i in range(1, 500)])
db.commit()
original = capture_sqlite_image(db)
db.execute("UPDATE entries SET value='changed' WHERE id=1")
db.commit()
changed = capture_sqlite_image(db)
db.close()

def stage(revision, data):
    run = str(uuid4())
    store.stage(key, revision, run, data)
    return run

first = stage(0, original)
assert store.publish(key, 0, first) == 1
entered, resume = Event(), Event()
class PausedReader:
    def batch(self, statements):
        if any("SELECT c.data_base64 AS marker" in stmt.sql for stmt in statements) and not entered.is_set():
            entered.set()
            assert resume.wait(30), "reader was not resumed"
        return client.batch(statements)

with ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(D1SQLiteImageStore(PausedReader()).head, key)
    assert entered.wait(30), "reader did not pin head"
    second = stage(1, changed)
    class LostPublishResponse:
        def batch(self, statements):
            result = client.batch(statements)
            if any("INSERT INTO cf_sqlite_heads" in stmt.sql for stmt in statements):
                raise D1BatchOutcomeUnknown("simulated lost publish response")
            return result
    try:
        D1SQLiteImageStore(LostPublishResponse()).publish(key, 1, second)
    except D1BatchOutcomeUnknown:
        pass
    else:
        raise AssertionError("lost response must remain unknown")
    assert store.published_receipt(second) == (key, 2, hashlib.sha256(changed).hexdigest())
    third = stage(2, changed)
    assert store.publish(key, 2, third) == 3
    staged = stage(3, original)
    count, _ = store.collect(key, keep_revisions=1, limit=100)
    assert count == 1  # revision 2 is free; pinned revision 1 remains
    rows = client.batch([D1Statement("SELECT id,sealed FROM cf_sqlite_images WHERE database_name=?", [key])])[0]["results"]
    assert {row["id"] for row in rows} == {first, third, staged}
    for image_id in (first, third):
        try:
            client.batch([D1Statement("DELETE FROM cf_sqlite_images WHERE id=?", [image_id])])
        except D1BatchOutcomeUnknown:
            pass
        else:
            raise AssertionError("pinned or head image must not be deleted")
    resume.set()
    restored = future.result(timeout=30)
    assert restored.revision == 1 and restored.data == original

count, _ = store.collect(key, keep_revisions=1, limit=100)
assert count == 1  # revision 1 is no longer pinned
assert store.published_receipt(first) == (key, 1, hashlib.sha256(original).hexdigest())
assert store.published_receipt(second) == (key, 2, hashlib.sha256(changed).hexdigest())
assert store.published_receipt(staged) is None  # an unsealed upload did not publish
try:
    store.stage(key, 3, first, original)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("a collected published image ID must not be reused")
assert store.publish(key, 3, staged) == 4  # active staged chunks survived both collections
assert store.head(key).data == original
removed_images, removed_chunks = store.collect(key, keep_revisions=1, limit=100)
assert removed_images == 1 and removed_chunks > 0
assert store.collect(key, keep_revisions=1, limit=100)[0] == 0

# Failure to release a pin leaks space but must preserve its image indefinitely.
class LostRelease:
    def batch(self, statements):
        if any("DELETE FROM cf_sqlite_image_pins" in stmt.sql for stmt in statements):
            raise D1BatchOutcomeUnknown("simulated unavailable release")
        return client.batch(statements)
try:
    D1SQLiteImageStore(LostRelease()).head(key)
except D1BatchOutcomeUnknown:
    pass
else:
    raise AssertionError("release failure must surface")
rows = client.batch([D1Statement("SELECT id,image_id FROM cf_sqlite_image_pins WHERE database_name=?", [key])])[0]["results"]
assert len(rows) == 1 and rows[0]["image_id"] == staged
fifth = stage(4, changed)
assert store.publish(key, 4, fifth) == 5
assert store.collect(key, keep_revisions=1, limit=100)[0] == 0
assert store.head(key).data == changed
# In this disposable test the reader has ended; only then may its pin be removed.
client.batch([D1Statement("DELETE FROM cf_sqlite_image_pins WHERE id=?", [rows[0]["id"]])])
assert store.collect(key, keep_revisions=1, limit=100)[0] == 1
assert store.head(key).data == changed
assert store.published_receipt(staged) == (key, 4, hashlib.sha256(original).hexdigest())
for sql in ("DELETE FROM cf_sqlite_publication_receipts WHERE image_id=?",
            "UPDATE cf_sqlite_publication_receipts SET revision=99 WHERE image_id=?"):
    try:
        client.batch([D1Statement(sql, [staged])])
    except D1BatchOutcomeUnknown:
        pass
    else:
        raise AssertionError("publication receipt must be immutable")
print("D1 snapshot GC: pinned readers, staged writes, lost release and bounded collection verified")
