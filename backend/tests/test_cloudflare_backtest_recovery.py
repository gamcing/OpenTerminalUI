import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db.models import BacktestRun
from backend.shared.db import Base
from backend.services import backtest_jobs


@pytest.fixture
def sessions(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[BacktestRun.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    def get_db():
        with factory() as db:
            yield db
    monkeypatch.setattr(backtest_jobs, "get_db", get_db)
    yield factory, engine
    engine.dispose()


def add_run(session, run_id, status):
    session.add(BacktestRun(run_id=run_id, status=status, request_json=json.dumps({"symbol": "SPY"}),
                            result_json='{"existing":true}' if status == "done" else ""))


@pytest.mark.asyncio
async def test_restart_recovers_queued_jobs_and_marks_interrupted_without_replay(sessions):
    factory, _engine = sessions
    with factory() as db:
        for run_id, status in [("first", "queued"), ("interrupted", "running"), ("done", "done"),
                               ("failed", "failed"), ("second", "queued")]:
            add_run(db, run_id, status)
        db.commit()
    service = backtest_jobs.BacktestJobService()
    executed = []
    async def execute(run_id):
        executed.append(run_id)
        with factory() as db:
            row = db.query(BacktestRun).filter_by(run_id=run_id).one()
            row.status = "done"
            db.commit()
    service._execute = execute
    try:
        await service.recover_snapshot_jobs()
        await service.recover_snapshot_jobs()
        await asyncio.wait_for(service._queue.join(), 5)
        assert executed == ["first", "second"]
        with factory() as db:
            rows = {row.run_id: row for row in db.query(BacktestRun).all()}
            assert rows["interrupted"].status == "failed"
            assert "interrupted" in rows["interrupted"].error
            assert rows["done"].result_json == '{"existing":true}'
            assert rows["failed"].status == "failed"
    finally:
        await service.stop_snapshot_worker()


@pytest.mark.asyncio
async def test_recovery_failure_does_not_launch_jobs(sessions):
    factory, engine = sessions
    with factory() as db:
        add_run(db, "queued", "queued")
        add_run(db, "running", "running")
        db.commit()
    def fail_commit(_session):
        raise RuntimeError("database unavailable")
    event.listen(factory, "before_commit", fail_commit)
    service = backtest_jobs.BacktestJobService()
    try:
        with pytest.raises(RuntimeError, match="database unavailable"):
            await service.recover_snapshot_jobs()
        assert service._worker_task is None and service._queue.empty()
    finally:
        event.remove(factory, "before_commit", fail_commit)
        await service.stop_snapshot_worker()
    with factory() as db:
        assert db.query(BacktestRun).filter_by(run_id="running").one().status == "running"


@pytest.mark.asyncio
async def test_shutdown_interrupts_running_job_and_next_start_reports_failure(sessions):
    factory, _engine = sessions
    with factory() as db:
        add_run(db, "in-flight", "queued")
        db.commit()
    service = backtest_jobs.BacktestJobService()
    entered = asyncio.Event()
    async def execute(run_id):
        with factory() as db:
            db.query(BacktestRun).filter_by(run_id=run_id).one().status = "running"
            db.commit()
        entered.set()
        await asyncio.Event().wait()
    service._execute = execute
    await service.recover_snapshot_jobs()
    await asyncio.wait_for(entered.wait(), 5)
    await service.stop_snapshot_worker()
    await service.recover_snapshot_jobs()
    try:
        with factory() as db:
            row = db.query(BacktestRun).filter_by(run_id="in-flight").one()
            assert row.status == "failed" and "interrupted" in row.error
        assert service._queue.empty()
    finally:
        await service.stop_snapshot_worker()


@pytest.mark.asyncio
async def test_application_lifespan_starts_and_stops_snapshot_recovery(sessions, monkeypatch):
    factory, _engine = sessions
    with factory() as db:
        add_run(db, "lifespan-queued", "queued")
        db.commit()
    import backend.main as main
    import backend.api.deps as deps
    import backend.shared.db as database
    import backend.research_dossiers.scheduler as dossiers
    import backend.screen_monitoring.scheduler as screens
    import backend.filings_watch.scheduler as filings

    service = backtest_jobs.BacktestJobService()
    executed = []
    async def execute(run_id):
        executed.append(run_id)
    service._execute = execute
    monkeypatch.setattr(database, "snapshot_storage", object())
    monkeypatch.setattr(backtest_jobs, "get_backtest_job_service", lambda: service)
    monkeypatch.setattr(main, "validate_runtime_secrets", lambda: None)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "_prefetch_enabled", False)
    monkeypatch.setattr(main, "get_prefetch_worker", lambda _fetcher: None)
    for name in ("get_instruments_loader", "get_news_ingestor", "get_pcr_snapshot_service",
                 "get_scanner_alert_scheduler_service"):
        monkeypatch.setattr(main, name, lambda: None)
    monkeypatch.setattr(main, "get_marketdata_hub", lambda: SimpleNamespace(start=AsyncMock(), shutdown=AsyncMock()))
    monkeypatch.setattr(main, "get_alert_evaluator_service", lambda: SimpleNamespace(start=Mock()))
    monkeypatch.setattr(main, "get_paper_engine", lambda: SimpleNamespace(start=Mock()))
    monkeypatch.setattr(deps, "get_unified_fetcher", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "shutdown_unified_fetcher", AsyncMock())
    for module, names in ((dossiers, ("start_dossier_monitor", "stop_dossier_monitor")),
                          (screens, ("start_screen_monitor", "stop_screen_monitor")),
                          (filings, ("start_filings_watch", "stop_filings_watch"))):
        for name in names:
            monkeypatch.setattr(module, name, AsyncMock())
    async with main.lifespan(main.app):
        await asyncio.wait_for(service._queue.join(), 5)
        assert executed == ["lifespan-queued"]
        assert service._snapshot_recovered
    assert service._worker_task is None and not service._snapshot_recovered
