from __future__ import annotations

from pathlib import Path
from io import BytesIO
from typing import Any

import pandas as pd

from backend.cloudflare.r2_backtests import r2_backtest_store_from_env

DATA_DIR = Path("data") / "backtest_store"
PARQUET_DIR = DATA_DIR / "parquet"
CATALOG_FILE = DATA_DIR / "catalog.sqlite"


def ensure_store() -> None:
    PARQUET_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def write_equity_curve(run_id: str, equity_curve: list[dict[str, Any]]) -> dict[str, Any]:
    store = r2_backtest_store_from_env()
    if store is None:
        ensure_store()
    frame = pd.DataFrame(equity_curve)
    if frame.empty:
        return {"run_id": run_id, "rows": 0, "path": ""}
    if store is not None:
        output = BytesIO()
        try:
            frame.to_parquet(output, index=False)
            content, extension = output.getvalue(), "parquet"
        except Exception:
            content, extension = frame.to_csv(index=False, lineterminator="\n").encode("utf-8"), "csv"
        name = store.put(run_id, extension, content)
        return {"run_id": run_id, "rows": len(frame), "path": "r2://backtests/" + name}
    out_path = PARQUET_DIR / f"{run_id}.parquet"
    try:
        frame.to_parquet(out_path, index=False)
    except Exception:
        fallback = PARQUET_DIR / f"{run_id}.csv"
        frame.to_csv(fallback, index=False)
        return {"run_id": run_id, "rows": len(frame), "path": str(fallback)}
    return {"run_id": run_id, "rows": len(frame), "path": str(out_path)}


def list_store_items() -> list[dict[str, Any]]:
    store = r2_backtest_store_from_env()
    if store is not None:
        return store.list()
    ensure_store()
    items: list[dict[str, Any]] = []
    for path in sorted(PARQUET_DIR.glob("*")):
        if not path.is_file():
            continue
        items.append(
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "updated_at": path.stat().st_mtime,
            }
        )
    return items
