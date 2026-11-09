import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.models.user import User, UserRole
from backend.shared.db import Base
import backend.shared.db as database
from scripts.seed_admin import _seed_cloudflare_snapshot_admin, main


@pytest.fixture
def snapshot_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[User.__table__])
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(database, "snapshot_storage", object())
    yield sessions
    engine.dispose()


@pytest.mark.parametrize("email,password", [("", "valid-password"), ("broken", "valid-password"),
                                            ("a@example.test", "short"), ("a@example.test", "x" * 73)])
def test_new_database_requires_valid_bootstrap(snapshot_db, email, password):
    assert _seed_cloudflare_snapshot_admin(email, password) == 1
    with snapshot_db() as session:
        assert session.query(User).count() == 0


def test_creates_admin_once_and_allows_later_boot_without_credentials(snapshot_db):
    assert _seed_cloudflare_snapshot_admin("admin@example.test", "valid-password") == 0
    assert _seed_cloudflare_snapshot_admin("", "") == 0
    with snapshot_db() as session:
        admin = session.query(User).one()
        assert admin.email == "admin@example.test" and admin.role == UserRole.ADMIN
        assert admin.hashed_password != "valid-password"


def test_does_not_promote_or_replace_existing_non_admin_users(snapshot_db):
    with snapshot_db() as session:
        session.add(User(email="viewer@example.test", hashed_password="hash", role=UserRole.VIEWER))
        session.commit()
    assert _seed_cloudflare_snapshot_admin("admin@example.test", "valid-password") == 1
    with snapshot_db() as session:
        assert session.query(User).one().role == UserRole.VIEWER


def test_database_failure_stops_startup_without_disclosing_details(snapshot_db, monkeypatch, capsys):
    class FailingSession:
        def query(self, *_args):
            raise RuntimeError("database-secret-in-error")
        def rollback(self):
            pass
        def close(self):
            pass
    monkeypatch.setattr(database, "SessionLocal", FailingSession)
    assert _seed_cloudflare_snapshot_admin("admin@example.test", "valid-password") == 1
    assert "database-secret-in-error" not in capsys.readouterr().out


def test_missing_snapshot_and_missing_credentials_fail_in_container(monkeypatch):
    monkeypatch.setenv("OPENTERMINALUI_CF_CONTAINER", "1")
    monkeypatch.setenv("OPENTERMINALUI_DATABASE_STORE", "d1_snapshot")
    monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(database, "snapshot_storage", None)
    assert main() == 1


def test_container_entrypoint_stops_on_failed_seed(tmp_path):
    bash = shutil.which("bash")
    if not bash and os.name == "nt":
        candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
        bash = str(candidate) if candidate.exists() else None
    if not bash:
        pytest.skip("a POSIX shell is unavailable")
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\nif [ \"$1\" = scripts/seed_admin.py ]; then exit \"$TEST_SEED_CODE\"; fi\necho UVICORN_INVOKED\n", encoding="utf-8")
    fake_python.chmod(0o755)
    script = Path("backend/entrypoint.sh").read_text(encoding="utf-8")
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ.get("PATH", ""),
               SKIP_MIGRATIONS="1", OPENTERMINALUI_CF_CONTAINER="1",
               OPENTERMINALUI_DATABASE_STORE="d1_snapshot", TEST_SEED_CODE="1")
    def run():
        return subprocess.run([bash, "--noprofile", "--norc", "-s"], input=script, text=True,
                              env=env, capture_output=True, timeout=15)
    failed = run()
    assert failed.returncode != 0 and "UVICORN_INVOKED" not in failed.stdout, failed.stderr
    env["TEST_SEED_CODE"] = "0"
    passed = run()
    assert passed.returncode == 0 and "UVICORN_INVOKED" in passed.stdout, passed.stderr
    env.update(TEST_SEED_CODE="1", OPENTERMINALUI_CF_CONTAINER="0")
    legacy = run()
    assert legacy.returncode == 0 and "UVICORN_INVOKED" in legacy.stdout, legacy.stderr
