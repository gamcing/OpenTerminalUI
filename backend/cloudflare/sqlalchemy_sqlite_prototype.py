"""Experimental SQLAlchemy/SQLite transaction coordinator for isolated tests only.

This is not wired into the application. The caller must use a fresh private
working directory and route *all* access through the returned engine. A fatal or
unknown publish poisons the coordinator; dispose it and restore with a new one.
Production deployment requires process fencing, multi-connection auditing,
container integration, durable file handling, and real failure testing.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool

from backend.cloudflare.sqlite_images import D1SQLiteImageStore, capture_sqlite_image

_READ_ONLY_PRAGMAS = frozenset({"table_info", "table_xinfo", "index_info", "index_xinfo", "index_list",
                                "foreign_key_list", "foreign_key_check", "integrity_check", "quick_check"})
_WRITE_ACTIONS = frozenset(getattr(sqlite3, name) for name in (
    "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE",
    "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW", "SQLITE_ALTER_TABLE", "SQLITE_REINDEX", "SQLITE_ANALYZE", "SQLITE_CREATE_VTABLE",
    "SQLITE_DROP_VTABLE",
))


class SnapshotCoordinatorError(RuntimeError):
    """The experimental engine cannot safely continue."""


class SnapshotCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        self.connection._check()
        return super().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        self.connection._check()
        return super().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        raise SnapshotCoordinatorError("executescript is unsupported")


class SnapshotConnection(sqlite3.Connection):
    _manager: SQLiteSnapshotCoordinator
    _dirty: bool = False
    _control: bool = False

    def _check(self):
        self._manager.check()

    def _authorize(self, action, arg1, arg2, database, source):
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_TRANSACTION and not self._control:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SAVEPOINT and not self.in_transaction:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            key = (arg1 or "").lower()
            if key in {"writable_schema", "journal_mode", "locking_mode", "foreign_keys", "ignore_check_constraints",
                        "read_uncommitted", "synchronous"} and arg2 is not None:
                return sqlite3.SQLITE_DENY
            if arg2 is not None and key not in _READ_ONLY_PRAGMAS:
                if not self.in_transaction:
                    return sqlite3.SQLITE_DENY
                self._dirty = True
        if action in _WRITE_ACTIONS:
            if not self.in_transaction:
                return sqlite3.SQLITE_DENY
            self._dirty = True
        return sqlite3.SQLITE_OK

    def set_authorizer(self, callback):
        raise SnapshotCoordinatorError("changing the authorizer is unsupported")

    def cursor(self, factory=SnapshotCursor):
        self._check()
        if factory is not SnapshotCursor:
            raise SnapshotCoordinatorError("custom cursors are unsupported")
        return super().cursor(factory)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        raise SnapshotCoordinatorError("executescript is unsupported")

    def commit(self):
        self._check()
        if not self.in_transaction:
            self._dirty = False
            return
        if not self._dirty:
            self._control = True
            try:
                return super().commit()
            finally:
                self._control = False
        self._manager.commit(self)
        self._dirty = False

    def rollback(self):
        # Cleanup must work after a failure has poisoned the coordinator.
        self._control = True
        try:
            return super().rollback()
        finally:
            self._control = False
            self._dirty = False

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


class SQLiteSnapshotCoordinator:
    def __init__(self, store: D1SQLiteImageStore, database_name: str, directory: Path):
        self.store = store
        self.database_name = database_name
        self._lock = threading.RLock()
        self._poisoned = False
        directory = Path(directory)
        if not directory.is_dir() or any(directory.iterdir()):
            raise ValueError("a fresh empty private directory is required")
        self.path = directory / "database.sqlite"
        self._revision = 0
        self._image_id = None
        head = store.head(database_name)
        if head is not None:
            # The directory is new and private; do not overwrite any local files.
            with self.path.open("xb") as output:
                output.write(head.data)
                output.flush()
            self._revision, self._image_id = head.revision, head.image_id
        # WAL keeps readers from blocking the local commit after D1 publishes.
        # This is performed before any application connection is made available.
        initial = sqlite3.connect(str(self.path))
        try:
            if initial.execute("PRAGMA journal_mode=WAL").fetchone() != ("wal",):
                raise SnapshotCoordinatorError("could not enable SQLite WAL")
        finally:
            initial.close()

    def check(self):
        if self._poisoned:
            raise SnapshotCoordinatorError("snapshot coordinator requires recovery")

    def _check_head(self):
        self.check()
        head = self.store.head_info(self.database_name)
        revision, image_id = (head.revision, head.image_id) if head else (0, None)
        if (revision, image_id) != (self._revision, self._image_id):
            self._poisoned = True
            raise SnapshotCoordinatorError("database revision changed; recovery required")

    def create_engine(self):
        manager = self
        def connect():
            manager.check()
            connection = sqlite3.connect(str(manager.path), factory=SnapshotConnection,
                                         check_same_thread=False, cached_statements=0, timeout=5)
            connection._manager = manager
            connection._dirty = False
            connection._control = False
            connection.isolation_level = None
            sqlite3.Connection.execute(connection, "PRAGMA foreign_keys=ON")
            sqlite3.Connection.set_authorizer(connection, connection._authorize)
            return connection
        engine = create_engine("sqlite://", creator=connect, poolclass=NullPool)

        @event.listens_for(engine, "begin")
        def begin(conn):
            with manager._lock:
                manager._check_head()
                raw = conn.connection.driver_connection
                raw._dirty = False
                raw._control = True
                try:
                    conn.exec_driver_sql("BEGIN")
                finally:
                    raw._control = False
        return engine

    def commit(self, connection: SnapshotConnection):
        with self._lock:
            self._check_head()
            # SQLite defers some FK checks until COMMIT. Never publish a snapshot
            # that is already known to violate them.
            if sqlite3.Connection.execute(connection, "PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("deferred foreign key violation")
            image = capture_sqlite_image(connection)
            image_id = str(uuid4())  # retain before staging in case the response is lost
            self.store.stage(self.database_name, self._revision, image_id, image)
            try:
                next_revision = self.store.publish(self.database_name, self._revision, image_id)
                connection._control = True
                self._commit_local(connection)
            except BaseException:
                # D1 may have committed even if its response was lost, or SQLite's
                # local commit may have failed after D1 committed. Do not reuse.
                self._poisoned = True
                raise
            finally:
                connection._control = False
            self._revision, self._image_id = next_revision, image_id

    def _commit_local(self, connection: SnapshotConnection):
        sqlite3.Connection.commit(connection)
