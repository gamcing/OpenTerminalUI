"""Bounded R2 backtest artifacts for isolated local and container experiments."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

from backend.cloudflare.d1 import _open

_LIMIT = 8 * 1024 * 1024
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}-([a-f0-9]{64})\.(?:parquet|csv)\Z")


class R2BacktestError(RuntimeError):
    """Object storage failed; a timed-out write may still have committed."""


class R2BacktestStore:
    def __init__(self, endpoint: str, token: str, *, allow_localhost: bool = True,
                 allow_intercepted_container_origin: bool = False):
        try:
            url = urlsplit(endpoint)
            local = allow_localhost and url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
            intercepted = (allow_intercepted_container_origin and url.scheme == "http"
                           and url.hostname == "openterminal-r2.internal" and url.port is None)
            valid = ((local or intercepted) and not url.username and not url.password and not url.query
                     and not url.fragment and url.path in ("", "/"))
            url.port  # validate the port before any request
        except ValueError:
            valid = False
        if not valid or len(token) < 32:
            raise ValueError("invalid R2 gateway configuration")
        self._endpoint = endpoint.rstrip("/")
        self._token = token

    def _request(self, method: str, path: str, data: bytes | None = None) -> bytes:
        request = urllib.request.Request(self._endpoint + path, data=data, method=method,
                                         headers={"Authorization": "Bearer " + self._token})
        try:
            with _open(request, 30) as response:
                body = response.read(_LIMIT + 1)
                if len(body) > _LIMIT:
                    raise R2BacktestError("oversized R2 gateway response")
                return body
        except (OSError, ValueError):
            raise R2BacktestError("R2 gateway request failed; write outcome may be unknown") from None

    def put(self, run_id: str, extension: str, data: bytes) -> str:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or extension not in {"parquet", "csv"}:
            raise ValueError("invalid backtest artifact name")
        if not isinstance(data, bytes) or not 0 < len(data) <= _LIMIT:
            raise ValueError("invalid or oversized backtest artifact")
        name = run_id + "-" + hashlib.sha256(data).hexdigest() + "." + extension
        try:
            result = json.loads(self._request("PUT", "/objects/" + name, data))
        except (ValueError, TypeError):
            raise R2BacktestError("invalid R2 gateway response") from None
        if result != {"name": name, "bytes": len(data)}:
            raise R2BacktestError("unexpected R2 gateway response")
        return name

    def get(self, name: str) -> bytes:
        match = _NAME.fullmatch(name) if isinstance(name, str) else None
        if not match:
            raise ValueError("invalid backtest artifact name")
        data = self._request("GET", "/objects/" + name)
        if hashlib.sha256(data).hexdigest() != match[1]:
            raise R2BacktestError("R2 artifact failed digest verification")
        return data

    def list(self) -> list[dict]:
        items, cursor, seen = [], None, set()
        while True:
            query = "?" + urlencode({"cursor": cursor}) if cursor else ""
            try:
                page = json.loads(self._request("GET", "/objects" + query))
                rows, next_cursor = page["items"], page["cursor"]
                if not isinstance(rows, list) or len(items) + len(rows) > 10000:
                    raise ValueError
                for row in rows:
                    if (not isinstance(row, dict) or not isinstance(row.get("name"), str) or not _NAME.fullmatch(row["name"])
                            or type(row.get("bytes")) is not int or not 0 <= row["bytes"] <= _LIMIT
                            or type(row.get("updated_at")) not in (int, float) or not math.isfinite(row["updated_at"])):
                        raise ValueError
                items.extend(rows)
                if next_cursor is None:
                    return sorted(items, key=lambda item: item["name"])
                if not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 2048 or next_cursor in seen:
                    raise ValueError
                seen.add(next_cursor)
                cursor = next_cursor
            except (ValueError, TypeError, KeyError):
                raise R2BacktestError("invalid R2 catalog response") from None


def r2_backtest_store_from_env() -> R2BacktestStore | None:
    mode = os.getenv("OPENTERMINALUI_BACKTEST_STORE", "local").strip().lower()
    if mode == "local":
        return None
    environment = (os.getenv("OPENTERMINALUI_ENV") or os.getenv("APP_ENV") or os.getenv("ENV") or "").strip().lower()
    local = (os.getenv("OPENTERMINALUI_R2_LOCAL_TEST") == "1"
             and environment in {"dev", "development", "local", "test", "testing"})
    container = (os.getenv("OPENTERMINALUI_CF_CONTAINER") == "1"
                 and os.getenv("OPENTERMINALUI_R2_CONTAINER_EXPERIMENT") == "1"
                 and environment == "production")
    if mode != "r2" or not (local or container):
        raise RuntimeError("R2 backtest storage requires an explicitly enabled experiment")
    return R2BacktestStore(os.getenv("R2_GATEWAY_URL", ""), os.getenv("R2_BRIDGE_TOKEN", ""),
                           allow_localhost=local and not container,
                           allow_intercepted_container_origin=container)
