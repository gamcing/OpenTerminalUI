"""Experimental immutable SQLite images in D1; not connected to application commits.

The caller must ensure exclusive control of the private D1 gateway. A successful
publish only makes a complete image durable; it does not commit a SQLite
transaction or provide an adapter for SQLAlchemy. Never retry a publish whose
outcome is unknown without inspecting the current head.
"""
from __future__ import annotations

import base64
import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from uuid import UUID

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement

_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_CHUNK_SIZE = 192 * 1024  # base64 and JSON remain below the bridge's 1 MiB request cap
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


@dataclass(frozen=True)
class SQLiteImageInfo:
    database_name: str
    revision: int
    image_id: str
    byte_count: int
    chunk_count: int
    sha256: str


@dataclass(frozen=True)
class SQLiteImage:
    database_name: str
    revision: int
    image_id: str
    data: bytes = field(repr=False)


def _name(value: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("invalid database name")
    return value


def _image_id(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("image ID must be a canonical UUID") from None
    return value


def _check_image(image: bytes) -> None:
    if (not isinstance(image, bytes) or not 100 <= len(image) <= _MAX_IMAGE_BYTES
            or image[:16] != b"SQLite format 3\x00" or image[18:20] != b"\x01\x01"):
        raise ValueError("invalid or oversized standalone SQLite image")
    copy = sqlite3.connect(":memory:")
    try:
        copy.deserialize(image)
        if copy.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite image failed integrity check")
    except sqlite3.Error:
        raise ValueError("SQLite image failed integrity check") from None
    finally:
        copy.close()


def capture_sqlite_image(connection: sqlite3.Connection) -> bytes:
    """Capture one connection's view; callers own transaction and locking rules.

    SQLite serialization from WAL mode retains WAL header markers even though
    the returned image has no WAL file. Normalize the standalone image to the
    rollback-journal format before validating it in a separate memory database.
    """
    image = bytearray(connection.serialize())
    if len(image) < 100 or image[:16] != b"SQLite format 3\x00":
        raise ValueError("invalid SQLite image")
    if image[18] not in (1, 2) or image[19] not in (1, 2):
        raise ValueError("unsupported SQLite journal format")
    image[18:20] = b"\x01\x01"
    result = bytes(image)
    _check_image(result)
    return result


class D1SQLiteImageStore:
    def __init__(self, client: D1BatchClient) -> None:
        self.client = client

    def _read_chunks(self, image_id: str, count: int, size: int, digest: str) -> bytes:
        chunks = []
        for ordinal in range(count):
            rows = self.client.batch([D1Statement(
                "SELECT data_base64 FROM cf_sqlite_image_chunks WHERE image_id=? AND ordinal=?", [image_id, ordinal],
            )])[0].get("results")
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                raise D1BatchOutcomeUnknown("incomplete SQLite image")
            try:
                raw = base64.b64decode(rows[0]["data_base64"], validate=True)
            except (ValueError, TypeError, KeyError):
                raise D1BatchOutcomeUnknown("invalid SQLite image chunk") from None
            expected = min(_CHUNK_SIZE, size - ordinal * _CHUNK_SIZE)
            if len(raw) != expected:
                raise D1BatchOutcomeUnknown("invalid SQLite image size")
            chunks.append(raw)
        image = b"".join(chunks)
        if len(image) != size or hashlib.sha256(image).hexdigest() != digest:
            raise D1BatchOutcomeUnknown("SQLite image failed digest verification")
        return image

    def head_info(self, database_name: str) -> SQLiteImageInfo | None:
        name = _name(database_name)
        rows = self.client.batch([D1Statement(
            "SELECT h.revision,i.id,i.byte_count,i.chunk_count,i.sha256 FROM cf_sqlite_heads h "
            "LEFT JOIN cf_sqlite_images i ON i.id=h.image_id AND i.sealed=1 "
            "AND i.database_name=h.database_name WHERE h.database_name=?", [name],
        )])[0].get("results")
        if not isinstance(rows, list) or len(rows) > 1:
            raise D1BatchOutcomeUnknown("invalid SQLite image head")
        if not rows:
            return None
        row = rows[0]
        try:
            revision, size, count = row["revision"], row["byte_count"], row["chunk_count"]
            image_id, digest = row["id"], row["sha256"]
            if (type(revision) is not int or revision < 1 or type(size) is not int or size < 1
                    or size > _MAX_IMAGE_BYTES or type(count) is not int or count != (size + _CHUNK_SIZE - 1) // _CHUNK_SIZE
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError
            _image_id(image_id)
        except (TypeError, KeyError, ValueError):
            raise D1BatchOutcomeUnknown("invalid SQLite image metadata") from None
        return SQLiteImageInfo(name, revision, image_id, size, count, digest)

    def head(self, database_name: str) -> SQLiteImage | None:
        info = self.head_info(database_name)
        if info is None:
            return None
        image = self._read_chunks(info.image_id, info.chunk_count, info.byte_count, info.sha256)
        try:
            _check_image(image)
        except ValueError:
            raise D1BatchOutcomeUnknown("invalid SQLite image") from None
        return SQLiteImage(info.database_name, info.revision, info.image_id, image)

    def stage(self, database_name: str, parent_revision: int, image_id: str, image: bytes) -> None:
        name = _name(database_name)
        image_id = _image_id(image_id)
        if type(parent_revision) is not int or parent_revision < 0:
            raise ValueError("invalid SQLite image staging parameters")
        _check_image(image)
        digest = hashlib.sha256(image).hexdigest()
        count = (len(image) + _CHUNK_SIZE - 1) // _CHUNK_SIZE
        self.client.batch([
            D1Statement("SELECT CASE WHEN COALESCE((SELECT revision FROM cf_sqlite_heads WHERE database_name=?),0)<>? "
                        "THEN json('stale image') ELSE 1 END", [name, parent_revision]),
            D1Statement("INSERT INTO cf_sqlite_images (id,database_name,parent_revision,byte_count,chunk_count,sha256) "
                        "VALUES (?,?,?,?,?,?)", [image_id, name, parent_revision, len(image), count, digest]),
        ])
        for ordinal in range(count):
            chunk = image[ordinal * _CHUNK_SIZE:(ordinal + 1) * _CHUNK_SIZE]
            self.client.batch([D1Statement(
                "INSERT INTO cf_sqlite_image_chunks (image_id,ordinal,data_base64) VALUES (?,?,?)",
                [image_id, ordinal, base64.b64encode(chunk).decode("ascii")],
            )])
        # Check the actual stored bytes before the final transaction publishes them.
        if self._read_chunks(image_id, count, len(image), digest) != image:
            raise D1BatchOutcomeUnknown("SQLite image verification failed")

    def publish(self, database_name: str, parent_revision: int, image_id: str) -> int:
        name = _name(database_name)
        image_id = _image_id(image_id)
        if type(parent_revision) is not int or parent_revision < 0:
            raise ValueError("invalid SQLite image publish parameters")
        rows = self.client.batch([D1Statement(
            "SELECT byte_count,chunk_count,sha256 FROM cf_sqlite_images "
            "WHERE id=? AND database_name=? AND parent_revision=? AND sealed=0", [image_id, name, parent_revision],
        )])[0].get("results")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise D1BatchOutcomeUnknown("invalid SQLite image staging record")
        try:
            size, count, digest = rows[0]["byte_count"], rows[0]["chunk_count"], rows[0]["sha256"]
            if (type(size) is not int or not 1 <= size <= _MAX_IMAGE_BYTES or type(count) is not int
                    or count != (size + _CHUNK_SIZE - 1) // _CHUNK_SIZE
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError
        except (KeyError, ValueError):
            raise D1BatchOutcomeUnknown("invalid SQLite image metadata") from None
        image = self._read_chunks(image_id, count, size, digest)
        try:
            _check_image(image)
        except ValueError:
            raise D1BatchOutcomeUnknown("invalid SQLite image") from None
        check = ("SELECT CASE WHEN COALESCE((SELECT revision FROM cf_sqlite_heads WHERE database_name=?),0)<>? "
                 "OR NOT EXISTS (SELECT 1 FROM cf_sqlite_images i WHERE i.id=? AND i.database_name=? "
                 "AND i.parent_revision=? AND i.sealed=0 AND "
                 "(SELECT COUNT(*) FROM cf_sqlite_image_chunks c WHERE c.image_id=i.id)=i.chunk_count AND "
                 "NOT EXISTS (SELECT 1 FROM cf_sqlite_image_chunks c WHERE c.image_id=i.id AND c.ordinal>=i.chunk_count)) "
                 "THEN json('stale or incomplete image') ELSE 1 END")
        results = self.client.batch([
            D1Statement(check, [name, parent_revision, image_id, name, parent_revision]),
            D1Statement("UPDATE cf_sqlite_images SET sealed=1 WHERE id=? AND sealed=0 RETURNING id", [image_id]),
            D1Statement("INSERT INTO cf_sqlite_heads (database_name,revision,image_id) VALUES (?,?,?) "
                        "ON CONFLICT(database_name) DO UPDATE SET revision=excluded.revision,image_id=excluded.image_id "
                        "RETURNING revision,image_id", [name, parent_revision + 1, image_id]),
        ])
        if (results[1].get("results") != [{"id": image_id}]
                or results[2].get("results") != [{"revision": parent_revision + 1, "image_id": image_id}]):
            raise D1BatchOutcomeUnknown("unexpected SQLite image publish result")
        return parent_revision + 1

    def abort(self, image_id: str) -> None:
        """Delete an unsealed image only; a committed image is never deleted."""
        self.client.batch([D1Statement("DELETE FROM cf_sqlite_images WHERE id=? AND sealed=0", [_image_id(image_id)])])
