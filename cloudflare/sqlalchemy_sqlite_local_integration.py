"""Exercise the isolated SQLAlchemy/D1 transaction prototype with local D1."""
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

from sqlalchemy import Column, ForeignKey, Integer, String, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement
from backend.cloudflare.sqlite_images import D1SQLiteImageStore
from backend.cloudflare.sqlalchemy_sqlite_prototype import SQLiteSnapshotCoordinator, SnapshotCoordinatorError

client = D1BatchClient("http://127.0.0.1:8899", "test-secret-only-not-for-production-1234567890", allow_insecure_localhost=True)
schema = (Path("cloudflare/migrations/0009_sqlite_image_storage.sql").read_text(encoding="utf-8")
          + Path("cloudflare/migrations/0010_sqlite_chunk_pool.sql").read_text(encoding="utf-8")
          + Path("cloudflare/migrations/0011_sqlite_snapshot_gc.sql").read_text(encoding="utf-8"))
statements, pending = [], ""
for line in schema.splitlines(keepends=True):
    pending += line
    if sqlite3.complete_statement(pending):
        statements.append(D1Statement(pending.strip()))
        pending = ""
assert not pending.strip()
client.batch(statements)
store = D1SQLiteImageStore(client)
key = "sqlalchemy-" + str(uuid4())

def rows(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT id,name FROM items ORDER BY id")).all()

def assert_unhealthy(coordinator):
    try:
        coordinator.check()
    except SnapshotCoordinatorError:
        return
    raise AssertionError("coordinator must report recovery required")

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    def directory(name):
        path = root / name
        path.mkdir()
        return path
    manager = SQLiteSnapshotCoordinator(store, key, directory("first"))
    engine = manager.create_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL)"))
        conn.execute(text("CREATE TABLE child (parent_id INTEGER REFERENCES items(id) DEFERRABLE INITIALLY DEFERRED)"))
        conn.execute(text("INSERT INTO items (name) VALUES (:name)"), {"name": "first"})
    assert store.head(key).revision == 1
    assert rows(engine) == [(1, "first")]
    assert store.head(key).revision == 1  # reads do not publish
    with engine.begin() as conn:
        assert conn.exec_driver_sql("PRAGMA table_info(items)").all()
    assert store.head(key).revision == 1  # schema introspection is read-only

    Session = sessionmaker(bind=engine)
    with Session() as session:
        session.execute(text("INSERT INTO items (name) VALUES ('rolled back')"))
        session.rollback()
    assert rows(engine) == [(1, "first")]
    assert store.head(key).revision == 1
    with Session.begin() as session:
        session.execute(text("INSERT INTO items (name) VALUES ('second')"))
        nested = session.begin_nested()
        session.execute(text("INSERT INTO items (name) VALUES ('discarded')"))
        nested.rollback()
    assert rows(engine) == [(1, "first"), (2, "second")]
    assert store.head(key).revision == 2

    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO child VALUES (999)"))
    except (sqlite3.IntegrityError, DBAPIError):
        pass
    else:
        raise AssertionError("deferred FK violation must prevent publish")
    assert store.head(key).revision == 2 and rows(engine) == [(1, "first"), (2, "second")]

    # Reject routes around the commit hook and dangerous SQLite modes.
    with engine.connect() as conn:
        for sql in ("COMMIT", "ATTACH DATABASE ':memory:' AS other", "PRAGMA foreign_keys=OFF"):
            try:
                conn.exec_driver_sql(sql)
            except DBAPIError:
                pass
            else:
                raise AssertionError("unsafe SQL was accepted: " + sql)
        raw = conn.connection.driver_connection
        try:
            raw.executescript("CREATE TABLE bypass(x)")
        except SnapshotCoordinatorError:
            pass
        else:
            raise AssertionError("executescript must be rejected")
    # Bypassing SQLAlchemy's begin hook cannot autocommit a mutation.
    raw_holder = engine.raw_connection()
    raw = raw_holder.driver_connection
    try:
        raw.execute("CREATE TABLE bypass(id INTEGER)")
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError("autocommit mutation must be rejected")
    raw_holder.close()
    engine.dispose()

    # A real SQLAlchemy ORM unit of work survives a fresh local database.
    Base = declarative_base()
    class Account(Base):
        __tablename__ = "accounts"
        id = Column(Integer, primary_key=True)
        name = Column(String, nullable=False)
        records = relationship("Record", cascade="all, delete-orphan")
    class Record(Base):
        __tablename__ = "records"
        id = Column(Integer, primary_key=True)
        account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
        value = Column(String)
    orm_key = key + "-orm"
    orm_manager = SQLiteSnapshotCoordinator(store, orm_key, directory("orm"))
    orm_engine = orm_manager.create_engine()
    Base.metadata.create_all(orm_engine)
    ORMSession = sessionmaker(bind=orm_engine)
    with ORMSession.begin() as session:
        account = Account(name="alice", records=[Record(value="one"), Record(value="two")])
        session.add(account)
    orm_engine.dispose()
    orm_recovered = SQLiteSnapshotCoordinator(store, orm_key, directory("orm-recovered"))
    orm_engine = orm_recovered.create_engine()
    with sessionmaker(bind=orm_engine)() as session:
        account = session.query(Account).one()
        assert account.name == "alice" and sorted(record.value for record in account.records) == ["one", "two"]
    orm_engine.dispose()

    # Restore from D1 into a fresh directory and reject stale readers/writers.
    first = SQLiteSnapshotCoordinator(store, key, directory("restored"))
    stale = SQLiteSnapshotCoordinator(store, key, directory("stale"))
    first_engine, stale_engine = first.create_engine(), stale.create_engine()
    assert rows(first_engine) == [(1, "first"), (2, "second")]
    with first_engine.begin() as conn:
        conn.execute(text("INSERT INTO items(name) VALUES ('third')"))
    try:
        rows(stale_engine)
    except SnapshotCoordinatorError:
        pass
    else:
        raise AssertionError("stale instance must refuse reads")
    first_engine.dispose()
    assert_unhealthy(stale)
    stale_engine.dispose()

    # Two independent instances can stage the same revision, but D1 publishes
    # exactly one; the other is fenced and cannot keep serving stale state.
    barrier = Barrier(2)
    class RacingStore:
        def head(self, name): return store.head(name)
        def head_info(self, name): return store.head_info(name)
        def stage(self, *args):
            store.stage(*args)
            barrier.wait(timeout=15)
        def publish(self, *args): return store.publish(*args)
    racing = RacingStore()
    competitors = [SQLiteSnapshotCoordinator(racing, key, directory("race-" + str(i))) for i in range(2)]
    engines = [competitor.create_engine() for competitor in competitors]
    def compete(i):
        try:
            with engines[i].begin() as conn:
                conn.execute(text("INSERT INTO items(name) VALUES (:name)"), {"name": "race-" + str(i)})
            return True
        except D1BatchOutcomeUnknown:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(compete, range(2))) == [False, True]
    for candidate in engines:
        candidate.dispose()
    winner = SQLiteSnapshotCoordinator(store, key, directory("race-winner"))
    winner_engine = winner.create_engine()
    current_rows = rows(winner_engine)
    assert len(current_rows) == 4 and current_rows[-1][1] in {"race-0", "race-1"}
    winner_engine.dispose()

    # A failure known to happen before publish rolls back locally and leaves
    # the coordinator usable; no durable application data was published.
    class FailedStage:
        def head(self, name): return store.head(name)
        def head_info(self, name): return store.head_info(name)
        def stage(self, *args): raise D1BatchOutcomeUnknown("simulated upload failure")
        def publish(self, *args): raise AssertionError("publish must not run")
    before = store.head(key)
    stage_failure = SQLiteSnapshotCoordinator(FailedStage(), key, directory("stage-failure"))
    stage_engine = stage_failure.create_engine()
    try:
        with stage_engine.begin() as conn:
            conn.execute(text("INSERT INTO items(name) VALUES ('not published')"))
    except D1BatchOutcomeUnknown:
        pass
    else:
        raise AssertionError("stage failure must be reported")
    assert store.head(key) == before and rows(stage_engine) == current_rows
    stage_engine.dispose()

    # A response lost after D1 commits must poison the old engine; reopening
    # discovers the committed remote snapshot without retrying the write.
    class LostPublish:
        def head(self, name): return store.head(name)
        def head_info(self, name): return store.head_info(name)
        def stage(self, *args): return store.stage(*args)
        def publish(self, *args):
            store.publish(*args)
            raise D1BatchOutcomeUnknown("simulated lost response")
    uncertain = SQLiteSnapshotCoordinator(LostPublish(), key, directory("uncertain"))
    uncertain_engine = uncertain.create_engine()
    try:
        with uncertain_engine.begin() as conn:
            conn.execute(text("INSERT INTO items(name) VALUES ('unknown')"))
    except D1BatchOutcomeUnknown:
        pass
    else:
        raise AssertionError("lost response must fail")
    try:
        rows(uncertain_engine)
    except SnapshotCoordinatorError:
        pass
    else:
        raise AssertionError("old engine must be poisoned")
    assert_unhealthy(uncertain)
    uncertain_engine.dispose()
    recovered = SQLiteSnapshotCoordinator(store, key, directory("recovered"))
    recovered_engine = recovered.create_engine()
    assert rows(recovered_engine) == current_rows + [(5, "unknown")]
    recovered.check()
    recovered_engine.dispose()

    # An error during the local commit after remote publication has the same
    # unknown outcome and cannot leave the old process serving stale data.
    class BrokenLocalCommit(SQLiteSnapshotCoordinator):
        def _commit_local(self, connection): raise sqlite3.OperationalError("simulated local failure")
    broken = BrokenLocalCommit(store, key, directory("broken"))
    broken_engine = broken.create_engine()
    try:
        with broken_engine.begin() as conn:
            conn.execute(text("INSERT INTO items(name) VALUES ('remote only')"))
    except (sqlite3.OperationalError, DBAPIError):
        pass
    else:
        raise AssertionError("local commit failure must be reported")
    try:
        rows(broken_engine)
    except SnapshotCoordinatorError:
        pass
    else:
        raise AssertionError("local commit failure must poison the engine")
    assert_unhealthy(broken)
    broken_engine.dispose()
    final = SQLiteSnapshotCoordinator(store, key, directory("final"))
    final_engine = final.create_engine()
    assert rows(final_engine)[-1] == (6, "remote only")
    final_engine.dispose()

    # A delayed publication prepared by an earlier process must not commit
    # after a replacement has fenced its restored revision.
    fence_key = "startup-fence-" + str(uuid4())
    previous = SQLiteSnapshotCoordinator(store, fence_key, directory("pre-fence"))
    previous_engine = previous.create_engine()
    with previous_engine.begin() as conn:
        conn.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)"))
        conn.execute(text("INSERT INTO items(name) VALUES ('before restart')"))
    old_head = store.head(fence_key)
    delayed_id = str(uuid4())
    store.stage(fence_key, old_head.revision, delayed_id, old_head.data)
    replacement = SQLiteSnapshotCoordinator(store, fence_key, directory("fenced"), fence_on_start=True)
    replacement_engine = replacement.create_engine()
    fenced_head = store.head(fence_key)
    assert fenced_head.revision == old_head.revision + 1
    assert fenced_head.data == old_head.data
    try:
        store.publish(fence_key, old_head.revision, delayed_id)
    except D1BatchOutcomeUnknown:
        pass
    else:
        raise AssertionError("delayed old publication must be fenced")
    assert rows(replacement_engine) == [(1, "before restart")]
    try:
        rows(previous_engine)
    except SnapshotCoordinatorError:
        pass
    else:
        raise AssertionError("previous process must detect the fence")
    previous_engine.dispose()
    replacement_engine.dispose()

    # The same rule applies before the first application migration: a delayed
    # initial publication cannot win after an empty database has been fenced.
    empty_key = "empty-fence-" + str(uuid4())
    first_id = str(uuid4())
    store.stage(empty_key, 0, first_id, old_head.data)
    empty = SQLiteSnapshotCoordinator(store, empty_key, directory("empty-fenced"), fence_on_start=True)
    assert store.head_info(empty_key).revision == 1
    try:
        store.publish(empty_key, 0, first_id)
    except D1BatchOutcomeUnknown:
        pass
    else:
        raise AssertionError("delayed initial publication must be fenced")
    empty.check()

    # A fence response can itself be lost after D1 commits. Startup must fail;
    # a later process restores the head and publishes a fresh fence.
    class LostFence:
        def head(self, name): return store.head(name)
        def stage(self, *args): return store.stage(*args)
        def publish(self, *args):
            store.publish(*args)
            raise D1BatchOutcomeUnknown("simulated lost fence response")
    prior_revision = store.head_info(fence_key).revision
    try:
        SQLiteSnapshotCoordinator(LostFence(), fence_key, directory("uncertain-fence"), fence_on_start=True)
    except D1BatchOutcomeUnknown:
        pass
    else:
        raise AssertionError("unknown fence outcome must prevent startup")
    assert store.head_info(fence_key).revision == prior_revision + 1
    after_unknown = SQLiteSnapshotCoordinator(store, fence_key, directory("after-unknown-fence"), fence_on_start=True)
    assert store.head_info(fence_key).revision == prior_revision + 2
    after_engine = after_unknown.create_engine()
    assert rows(after_engine) == [(1, "before restart")]
    after_engine.dispose()

print("D1 SQLAlchemy prototype: transactions, rollback, recovery, startup fencing and unknown outcomes verified")
