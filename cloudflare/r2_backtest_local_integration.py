"""Exercise the R2 backtest adapter across separate local processes."""
from __future__ import annotations
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4


def probe(action: str):
    import pandas as pd
    import backend.core.data_store as data_store
    import backend.cloudflare.r2_backtests as r2
    if action.startswith("container_"):
        # Simulate the private outbound intercept; this is not a real container.
        from urllib.parse import urlsplit
        original_open = r2._open
        def intercepted(request, timeout):
            url = urlsplit(request.full_url)
            assert url.scheme == "http" and url.netloc == "openterminal-r2.internal"
            mapped = "http://127.0.0.1:8900" + url.path + (("?" + url.query) if url.query else "")
            forwarded = urllib.request.Request(mapped, data=request.data, method=request.get_method(),
                                               headers=dict(request.header_items()))
            return original_open(forwarded, timeout)
        r2._open = intercepted
    data_store.DATA_DIR = Path(os.environ["TEST_LOCAL_ROOT"])
    data_store.PARQUET_DIR = data_store.DATA_DIR / "parquet"
    store = r2.r2_backtest_store_from_env()
    assert store is not None
    run_id = os.environ["TEST_RUN_ID"]
    if action in {"write", "container_write"}:
        result = data_store.write_equity_curve(run_id, [{"date": "2026-01-01", "equity": 123.5}])
        assert result["rows"] == 1 and result["path"].startswith("r2://backtests/")
        print(json.dumps(result))
    else:
        name = os.environ["TEST_OBJECT_NAME"]
        rows = data_store.list_store_items()
        assert any(row["name"] == name and row["bytes"] > 0 for row in rows)
        content = store.get(name)
        if name.endswith(".parquet"):
            frame = pd.read_parquet(io.BytesIO(content))
        else:
            frame = pd.read_csv(io.BytesIO(content))
        assert frame.iloc[0]["equity"] == 123.5
        one = store.put(run_id, "csv", b"equity\n123.5\n")
        before = next(row for row in store.list() if row["name"] == one)
        assert store.put(run_id, "csv", b"equity\n123.5\n") == one
        after = next(row for row in store.list() if row["name"] == one)
        assert before == after  # conditional repeat must not overwrite the object
        assert store.get(one) == b"equity\n123.5\n"
    assert not data_store.DATA_DIR.exists(), "R2 operation fell back to local disk"


def integration():
    token = "test-secret-only-not-for-production-1234567890"
    with tempfile.TemporaryDirectory() as root:
        env = dict(os.environ, OPENTERMINALUI_ENV="testing", OPENTERMINALUI_BACKTEST_STORE="r2",
                   OPENTERMINALUI_R2_LOCAL_TEST="1", R2_GATEWAY_URL="http://127.0.0.1:8900",
                   R2_BRIDGE_TOKEN=token, TEST_LOCAL_ROOT=str(Path(root) / "fallback"),
                   TEST_RUN_ID="bt_" + uuid4().hex[:12])
        def run(action, overrides=None):
            child_env = dict(env, **(overrides or {}))
            result = subprocess.run([sys.executable, "-m", "cloudflare.r2_backtest_local_integration", action],
                                    env=child_env, capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr
            return result.stdout
        written = json.loads(run("write"))
        env["TEST_OBJECT_NAME"] = written["path"].rsplit("/", 1)[1]
        run("read")
        container_env = {"OPENTERMINALUI_ENV": "production", "OPENTERMINALUI_R2_LOCAL_TEST": "0",
                         "OPENTERMINALUI_CF_CONTAINER": "1", "OPENTERMINALUI_R2_CONTAINER_EXPERIMENT": "1",
                         "R2_GATEWAY_URL": "http://openterminal-r2.internal",
                         "TEST_RUN_ID": "bt_" + uuid4().hex[:12]}
        container_written = json.loads(run("container_write", container_env))
        container_env["TEST_OBJECT_NAME"] = container_written["path"].rsplit("/", 1)[1]
        run("container_read", container_env)
        assert not Path(env["TEST_LOCAL_ROOT"]).exists()
    request = urllib.request.Request("http://127.0.0.1:8900/objects", headers={"Authorization": "Bearer " + "x" * 40})
    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    else:
        raise AssertionError("R2 gateway must reject incorrect tokens")
    print("Local R2 backtest storage: cross-process persistence, simulated container intercept, checksum and authentication verified")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in {"write", "read", "container_write", "container_read"}:
        probe(sys.argv[1])
    else:
        integration()
