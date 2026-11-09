"""Experimental offline, staged D1 identity import with atomic cutover.

The caller must stop source writes and keep them stopped through cutover. Staging
contains sensitive values. Only a successful final batch makes application rows
visible. A failed or unknown request must be inspected, never blindly retried.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from uuid import UUID

from backend.cloudflare.d1 import D1BatchClient, D1BatchOutcomeUnknown, D1Statement
from backend.cloudflare.identity_migration import IdentitySnapshot, _COLUMNS

# Conservative bound below the gateway's 1 MiB request limit, including JSON
# encoding a second time for transport. A single larger row is rejected.
_CHUNK_LIMIT = 768 * 1024
_INSERT_STAGE = (
    "INSERT INTO cf_identity_import_rows (run_id,table_name,ordinal,row_json) "
    "SELECT ?,?,?+CAST(key AS INTEGER),value FROM json_each(?) RETURNING ordinal,row_json"
)
_EMPTY_TARGET = (
    "EXISTS(SELECT 1 FROM users) OR EXISTS(SELECT 1 FROM refresh_tokens) "
    "OR EXISTS(SELECT 1 FROM saved_views) OR EXISTS(SELECT 1 FROM api_keys)"
)


def _run_id(run_id: str) -> str:
    try:
        parsed = UUID(run_id)
    except (TypeError, ValueError, AttributeError):
        raise ValueError("import run ID must be a canonical UUID") from None
    if str(parsed) != run_id:
        raise ValueError("import run ID must be a canonical UUID")
    return run_id


def _encoded(row: list) -> str:
    return json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _chunks(rows: list[list]) -> Iterator[tuple[int, str, int]]:
    parts: list[str] = []
    size = 2  # enclosing brackets, before transport encoding
    start = 0
    for ordinal, row in enumerate(rows):
        item = _encoded(row)
        # The JSON array is itself a string in the gateway's JSON request.
        cost = len(json.dumps(item, ensure_ascii=True).encode("utf-8")) - 2 + 1
        if cost + 4096 > _CHUNK_LIMIT:
            raise ValueError("a single source row exceeds the import request limit")
        if parts and size + cost + 4096 > _CHUNK_LIMIT:
            yield start, "[" + ",".join(parts) + "]", len(parts)
            start, parts, size = ordinal, [], 2
        parts.append(item)
        size += cost
    if parts:
        yield start, "[" + ",".join(parts) + "]", len(parts)


def _digest(snapshot: IdentitySnapshot) -> str:
    digest = hashlib.sha256()
    for table in _COLUMNS:
        digest.update(table.encode("ascii") + b"\0")
        for row in getattr(snapshot, table):
            data = _encoded(row).encode("utf-8")
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    return digest.hexdigest()


def _changes(result: dict, expected: int) -> None:
    meta = result.get("meta")
    if not isinstance(meta, dict) or meta.get("changes") != expected:
        raise D1BatchOutcomeUnknown("D1 returned an unexpected import result")


def stage_identity_snapshot(client: D1BatchClient, run_id: str, snapshot: IdentitySnapshot) -> dict[str, int]:
    """Upload rows under a fresh caller-supplied UUID without changing live tables.

    Retain the run ID before calling. On any failure inspect the run and abort it
    before starting a new one; do not repeat a request with an unknown outcome.
    """
    run_id = _run_id(run_id)
    counts = {table: len(getattr(snapshot, table)) for table in _COLUMNS}
    # Check every row before creating staging records, including late oversized rows.
    for table in _COLUMNS:
        for _ in _chunks(getattr(snapshot, table)):
            pass
    digest = _digest(snapshot)
    result = client.batch([
        D1Statement(f"SELECT CASE WHEN {_EMPTY_TARGET} THEN json('target is not empty') ELSE 1 END"),
        D1Statement(
            "INSERT INTO cf_identity_import_runs (id,snapshot_sha256,users_count,refresh_tokens_count,saved_views_count,api_keys_count) "
            "VALUES (?,?,?,?,?,?)", [run_id, digest, *counts.values()],
        ),
    ])
    _changes(result[1], 1)
    transmitted = hashlib.sha256()
    for table in _COLUMNS:
        transmitted.update(table.encode("ascii") + b"\0")
        for start, payload, count in _chunks(getattr(snapshot, table)):
            expected_rows = json.loads(payload)
            result = client.batch([D1Statement(_INSERT_STAGE, [run_id, table, start, payload])])
            _changes(result[0], count)
            returned = result[0].get("results")
            if not isinstance(returned, list) or len(returned) != count:
                raise D1BatchOutcomeUnknown("D1 returned incomplete staged rows")
            try:
                ordered = sorted(returned, key=lambda row: row["ordinal"])
                for offset, row in enumerate(ordered):
                    expected = _encoded(expected_rows[offset])
                    if row["ordinal"] != start + offset or _encoded(json.loads(row["row_json"])) != expected:
                        raise ValueError("mismatch")
                    data = expected.encode("utf-8")
                    transmitted.update(len(data).to_bytes(8, "big"))
                    transmitted.update(data)
            except (TypeError, ValueError, KeyError, IndexError):
                raise D1BatchOutcomeUnknown("D1 returned inconsistent staged rows") from None
    if transmitted.hexdigest() != digest:
        raise D1BatchOutcomeUnknown("source snapshot changed during staging")
    sealed = client.batch([D1Statement(
        "UPDATE cf_identity_import_runs SET sealed=1 WHERE id=? AND sealed=0 AND snapshot_sha256=? RETURNING id",
        [run_id, digest],
    )])[0]
    if sealed.get("results") != [{"id": run_id}]:
        raise D1BatchOutcomeUnknown("D1 returned an unexpected import seal result")
    return counts


def finalize_identity_snapshot(client: D1BatchClient, run_id: str, snapshot: IdentitySnapshot) -> dict[str, int]:
    """Validate staging and atomically install all four tables, then erase it."""
    run_id = _run_id(run_id)
    counts = {table: len(getattr(snapshot, table)) for table in _COLUMNS}
    checks = [f"({_EMPTY_TARGET})", "NOT EXISTS (SELECT 1 FROM cf_identity_import_runs WHERE id=? "
              "AND snapshot_sha256=? AND users_count=? AND refresh_tokens_count=? AND saved_views_count=? AND api_keys_count=? AND sealed=1)"]
    params: list = [run_id, _digest(snapshot), *counts.values()]
    for table, count in counts.items():
        checks.append("(SELECT COUNT(*) FROM cf_identity_import_rows WHERE run_id=? AND table_name=?) <> ?")
        params.extend((run_id, table, count))
        checks.append("EXISTS (SELECT 1 FROM cf_identity_import_rows WHERE run_id=? AND table_name=? AND ordinal>=?)")
        params.extend((run_id, table, count))
    statements = [D1Statement("SELECT CASE WHEN " + " OR ".join(checks) +
                              " THEN json('incomplete import or nonempty target') ELSE 1 END", params)]
    for table, columns in _COLUMNS.items():
        fields = ", ".join(f"json_extract(row_json, '$[{i}]')" for i in range(len(columns)))
        statements.append(D1Statement(
            f"INSERT INTO {table} ({', '.join(columns)}) SELECT {fields} FROM cf_identity_import_rows "
            "WHERE run_id=? AND table_name=? ORDER BY ordinal", [run_id, table],
        ))
    statements.append(D1Statement("DELETE FROM cf_identity_import_runs WHERE id=? RETURNING id", [run_id]))
    results = client.batch(statements)
    for index, count in enumerate(counts.values(), start=1):
        _changes(results[index], count)
    if results[-1].get("results") != [{"id": run_id}]:
        raise D1BatchOutcomeUnknown("D1 returned an unexpected import cleanup result")
    return counts


def abort_identity_snapshot(client: D1BatchClient, run_id: str) -> None:
    """Erase one abandoned import and its sensitive staging rows."""
    client.batch([D1Statement("DELETE FROM cf_identity_import_runs WHERE id=?", [_run_id(run_id)])])
