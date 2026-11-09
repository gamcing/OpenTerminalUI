"""Verify instrument services preserve their SQLite transaction behavior."""
import sqlite3

import pytest

from backend.bg_services.instruments_loader import FutureContractRow, InstrumentsLoader
from backend.services.instrument_map import InstrumentMapService, InstrumentRow


def test_instruments_share_the_configured_sqlite_file_and_roll_back(tmp_path):
    path = tmp_path / "nested" / "instruments.sqlite"
    loader = InstrumentsLoader()
    loader.sqlite_path = path
    loader._upsert_rows([FutureContractRow("TEST", "2026-12-31", "NFO", "TESTFUT", 12345, 50, 0.05, "2026-11-09")])

    instruments = InstrumentMapService()
    instruments.sqlite_path = path
    instruments._init_table()
    row = InstrumentRow("NFO", "TESTFUT", 12345, "TESTFUT", "2026-11-09")
    instruments._replace_rows([row])
    with pytest.raises(sqlite3.IntegrityError):
        instruments._replace_rows([row, row])
    assert instruments._resolve_many(["NFO:TESTFUT"]) == {"NFO:TESTFUT": 12345}
    assert instruments._symbol_by_token_many([12345]) == {12345: "NFO:TESTFUT"}
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT instrument_token,lot_size FROM future_contracts").fetchall() == [(12345, 50)]
