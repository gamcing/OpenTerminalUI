"""Tests for the gated R2 artifact adapter and failure handling."""
import json

import pandas as pd
import pytest

from backend.cloudflare import r2_backtests as r2
from backend.core import data_store

TOKEN = "test-secret-only-not-for-production-1234567890"


def test_storage_mode_rejects_production_and_unsafe_gateway(monkeypatch):
    monkeypatch.setenv("OPENTERMINALUI_BACKTEST_STORE", "r2")
    monkeypatch.setenv("OPENTERMINALUI_R2_LOCAL_TEST", "1")
    monkeypatch.setenv("R2_BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("R2_GATEWAY_URL", "http://127.0.0.1:8900")
    monkeypatch.setenv("OPENTERMINALUI_ENV", "production")
    with pytest.raises(RuntimeError):
        r2.r2_backtest_store_from_env()
    monkeypatch.setenv("OPENTERMINALUI_ENV", "testing")
    monkeypatch.setenv("R2_GATEWAY_URL", "http://example.com")
    with pytest.raises(ValueError):
        r2.r2_backtest_store_from_env()
    monkeypatch.setenv("R2_GATEWAY_URL", "http://127.0.0.1:8900/path")
    with pytest.raises(ValueError):
        r2.r2_backtest_store_from_env()
    monkeypatch.setenv("OPENTERMINALUI_BACKTEST_STORE", "typo")
    with pytest.raises(RuntimeError):
        r2.r2_backtest_store_from_env()


def test_storage_failure_never_falls_back_to_local_file(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENTERMINALUI_BACKTEST_STORE", "r2")
    monkeypatch.setenv("OPENTERMINALUI_R2_LOCAL_TEST", "1")
    monkeypatch.setenv("OPENTERMINALUI_ENV", "testing")
    monkeypatch.setenv("R2_BRIDGE_TOKEN", TOKEN)
    monkeypatch.setenv("R2_GATEWAY_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(data_store, "DATA_DIR", tmp_path / "fallback")
    monkeypatch.setattr(data_store, "PARQUET_DIR", tmp_path / "fallback" / "parquet")
    with pytest.raises(r2.R2BacktestError) as exc:
        data_store.write_equity_curve("bt_test", [{"equity": 3.0}])
    assert TOKEN not in str(exc.value)
    assert not (tmp_path / "fallback").exists()


def test_csv_fallback_serializes_before_upload(monkeypatch):
    class Store:
        def put(self, run_id, extension, content):
            assert run_id == "bt_test" and extension == "csv" and content == b"equity\n3.0\n"
            return "stored.csv"
    monkeypatch.setattr(data_store, "r2_backtest_store_from_env", lambda: Store())
    monkeypatch.setattr(pd.DataFrame, "to_parquet", lambda *a, **k: (_ for _ in ()).throw(ValueError("no parquet")))
    result = data_store.write_equity_curve("bt_test", [{"equity": 3.0}])
    assert result == {"run_id": "bt_test", "rows": 1, "path": "r2://backtests/stored.csv"}


def test_catalog_pagination_and_invalid_pages(monkeypatch):
    store = r2.R2BacktestStore("http://127.0.0.1:8900", TOKEN)
    first = {"name": "bt_one-" + "a" * 64 + ".csv", "bytes": 4, "updated_at": 1.0}
    second = {"name": "bt_two-" + "b" * 64 + ".parquet", "bytes": 5, "updated_at": 2.0}
    pages = iter([{ "items": [second], "cursor": "next+page" }, { "items": [first], "cursor": None }])
    paths = []
    def request(method, path, data=None):
        paths.append(path)
        return json.dumps(next(pages)).encode()
    monkeypatch.setattr(store, "_request", request)
    assert store.list() == [first, second]
    assert paths == ["/objects", "/objects?cursor=next%2Bpage"]
    monkeypatch.setattr(store, "_request", lambda *args: b'{"items":[],"cursor":"same"}')
    with pytest.raises(r2.R2BacktestError):
        store.list()


def test_artifact_names_and_digest_are_checked(monkeypatch):
    store = r2.R2BacktestStore("http://127.0.0.1:8900", TOKEN)
    for run_id in ("../escape", "a/b", "a" * 129):
        with pytest.raises(ValueError):
            store.put(run_id, "csv", b"data")
    monkeypatch.setattr(store, "_request", lambda *args: b"incorrect")
    with pytest.raises(r2.R2BacktestError, match="digest"):
        store.get("bt_test-" + "a" * 64 + ".csv")


def test_default_local_store_keeps_existing_empty_and_file_behavior(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENTERMINALUI_BACKTEST_STORE", raising=False)
    monkeypatch.setattr(data_store, "DATA_DIR", tmp_path / "local")
    monkeypatch.setattr(data_store, "PARQUET_DIR", tmp_path / "local" / "parquet")
    assert data_store.write_equity_curve("bt_empty", []) == {"run_id": "bt_empty", "rows": 0, "path": ""}
    assert (tmp_path / "local" / "parquet").is_dir()
    result = data_store.write_equity_curve("bt_test", [{"equity": 3.0}])
    assert result["rows"] == 1
    assert any(item["name"].startswith("bt_test.") for item in data_store.list_store_items())
