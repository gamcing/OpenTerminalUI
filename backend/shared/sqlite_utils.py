from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


def configure_sqlite_connection(conn: sqlite3.Connection) -> None:
    # Improve concurrency on local SQLite under mixed read/write workloads.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA busy_timeout=5000;")


class _CoordinatedConnection:
    """Small DB-API facade for services using qmark SQLite statements."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, parameters=()):
        return self._connection.exec_driver_sql(sql, parameters)

    def executemany(self, sql, parameters):
        values = list(parameters)
        if values:
            return self._connection.exec_driver_sql(sql, values)
        return None

    def commit(self):
        # The enclosing engine.begin() owns the atomic transaction.
        pass


@contextmanager
def sqlite_service_connection(path: Path):
    """Use the coordinated engine in snapshot tests, otherwise the existing file."""
    # Import lazily because db itself imports configure_sqlite_connection.
    from backend.shared.db import engine, snapshot_storage

    if snapshot_storage is not None:
        with engine.begin() as connection:
            yield _CoordinatedConnection(connection)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        try:
            configure_sqlite_connection(conn)
            with conn:
                yield conn
        finally:
            conn.close()
