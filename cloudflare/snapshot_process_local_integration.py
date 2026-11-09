"""Exercise gated application/Alembic/seed processes against disposable local D1."""
from __future__ import annotations

import os
import base64
import sqlite3
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from uuid import uuid4


def probe(action: str) -> None:
    # Keep unrelated market-fetcher dependencies out of this targeted test.
    from backend import shared  # noqa: F401
    import backend.shared.db as database
    database.init_db()
    from backend.models.user import User, UserRole
    from backend.models.core import Holding
    from backend.saved_views.models import SavedViewORM
    with database.SessionLocal() as session:
        admin = session.query(User).filter(User.email == "admin@example.test").one()
        uid, email, role = admin.id, admin.email, admin.role.value
        if action == "write":
            session.add(User(id="other-user", email="other@example.test", hashed_password="unused", role=UserRole.VIEWER))
            session.add(Holding(ticker="SPY", quantity=3, avg_buy_price=100, buy_date="2026-01-01"))
            session.add(SavedViewORM(id="saved-one", user_id=uid, name="Test", scope="workspace", page="/",
                                     payload_json={"layout": [1, 2]}, description=""))
            session.commit()
        else:
            assert session.query(User).count() == 2
            assert session.query(Holding).one().ticker == "SPY"
            assert session.query(SavedViewORM).one().payload_json == {"layout": [1, 2]}

    api_deps = types.ModuleType("backend.api.deps")
    def get_db():
        with database.SessionLocal() as session:
            yield session
    api_deps.get_db = get_db
    sys.modules["backend.api.deps"] = api_deps
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.auth.jwt import create_access_token
    from backend.auth.middleware import AuthMiddleware
    from backend.api.routes.watchlists import router
    app = FastAPI()
    app.state.db_session_factory = database.SessionLocal
    app.add_middleware(AuthMiddleware)
    app.include_router(router)
    headers = {"Authorization": "Bearer " + create_access_token(uid, email, role)}
    other = {"Authorization": "Bearer " + create_access_token("other-user", "other@example.test", "viewer")}
    with TestClient(app) as http:
        assert http.get("/api/watchlists").status_code == 401
        if action == "write":
            response = http.post("/api/watchlists", headers=headers, json={"name": "persistent", "symbols": ["spy", "msft"]})
            assert response.status_code == 200, response.text
            assert http.put("/api/watchlists/" + response.json()["id"], headers=other, json={"name": "bad"}).status_code == 404
        else:
            response = http.get("/api/watchlists", headers=headers)
            assert response.status_code == 200 and len(response.json()) == 1
            row = response.json()[0]
            assert row["name"] == "persistent"
            if action == "read":
                assert row["symbols"] == ["SPY", "MSFT"]
                assert http.post("/api/watchlists/" + row["id"] + "/symbols", headers=headers, json=["NVDA"]).status_code == 200
            else:
                assert row["symbols"] == ["SPY", "MSFT", "NVDA"]
    if action == "verify":
        from backend.models.core import FutureContract
        from backend.services.instrument_map import InstrumentMapService
        with database.SessionLocal() as session:
            row = session.query(FutureContract).filter_by(tradingsymbol="TESTFUT").one()
            assert row.instrument_token == 12345 and row.lot_size == 50
        instruments = InstrumentMapService()
        assert instruments._resolve_many(["NFO:TESTFUT"]) == {"NFO:TESTFUT": 12345}
        assert instruments._symbol_by_token_many([12345]) == {12345: "NFO:TESTFUT"}
    database.snapshot_storage.close()
    print("snapshot process " + action + " passed")


def real_application_probe() -> None:
    # Import the real app and dependencies, but do not start its lifespan: that
    # starts market fetchers and background services outside this local test.
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend.shared.db import snapshot_storage

    client = TestClient(app)
    response = client.post("/api/auth/login", json={
        "email": "admin@example.test", "password": "local-test-password-only",
    })
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    headers = {"Authorization": "Bearer " + token}
    response = client.get("/api/watchlists", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()[0]["symbols"] == ["SPY", "MSFT", "NVDA"]
    assert client.get("/api/watchlists").status_code == 401
    response = client.put("/api/settings/provider-keys", headers=headers, json={
        "values": {"FMP_API_KEY": "persisted-test-key-alpha", "FRED_API_KEY": ""},
    })
    assert response.status_code == 200, response.text
    provider_rows = {row["name"]: row for row in response.json()["keys"]}
    assert provider_rows["FMP_API_KEY"]["source"] == "database"
    assert provider_rows["FMP_API_KEY"]["masked"] != "persisted-test-key-alpha"
    assert not provider_rows["FRED_API_KEY"]["set"]
    from sqlalchemy import text
    from backend.shared.db import engine
    with engine.connect() as connection:
        values = connection.execute(text("SELECT ciphertext FROM provider_secrets")).scalars().all()
        assert len(values) == 2 and all("persisted-test-key-alpha" not in value for value in values)
    client.close()

    # These services historically bypassed SQLAlchemy and opened a different
    # SQLite file. Exercise their actual write paths against the coordinator.
    from backend.bg_services.instruments_loader import FutureContractRow, InstrumentsLoader
    from backend.models.core import FutureContract
    from backend.services.instrument_map import InstrumentMapService, InstrumentRow
    from backend.shared.db import SessionLocal
    from sqlalchemy.exc import IntegrityError

    loader = InstrumentsLoader()
    loader._upsert_rows([FutureContractRow("TEST", "2026-12-31", "NFO", "TESTFUT", 12345, 50, 0.05,
                                           "2026-11-09T00:00:00Z")])
    with SessionLocal() as session:
        assert session.query(FutureContract).filter_by(tradingsymbol="TESTFUT").one().instrument_token == 12345
    instruments = InstrumentMapService()
    instruments._init_table()
    row = InstrumentRow("NFO", "TESTFUT", 12345, "TESTFUT", "2026-11-09T00:00:00Z")
    instruments._replace_rows([row])
    try:
        instruments._replace_rows([row, row])
    except IntegrityError:
        pass
    else:
        raise AssertionError("duplicate instrument replacement should roll back")
    assert instruments._resolve_many(["NFO:TESTFUT"]) == {"NFO:TESTFUT": 12345}
    snapshot_storage.close()
    print("real application login and restored watchlist passed without lifespan")


def provider_probe(action: str) -> None:
    # The real app restores encrypted settings before importing provider modules.
    from backend.main import app
    from backend.shared.db import snapshot_storage
    assert "FRED_API_KEY" not in os.environ
    if action == "provider_read":
        assert os.environ["FMP_API_KEY"] == "persisted-test-key-alpha"
        from fastapi.testclient import TestClient
        client = TestClient(app)
        response = client.post("/api/auth/login", json={"email": "admin@example.test",
                                                        "password": "local-test-password-only"})
        assert response.status_code == 200
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        response = client.put("/api/settings/provider-keys", headers=headers, json={"values": {"FMP_API_KEY": ""}})
        assert response.status_code == 200
        assert "FMP_API_KEY" not in os.environ
        client.close()
    else:
        assert "FMP_API_KEY" not in os.environ
    snapshot_storage.close()


def integration() -> None:
    from backend.cloudflare.d1 import D1BatchClient, D1Statement
    from backend.cloudflare.sqlite_images import D1SQLiteImageStore
    client = D1BatchClient("http://127.0.0.1:8899", "test-secret-only-not-for-production-1234567890", allow_insecure_localhost=True)
    schema = (Path("cloudflare/migrations/0009_sqlite_image_storage.sql").read_text(encoding="utf-8")
              + Path("cloudflare/migrations/0010_sqlite_chunk_pool.sql").read_text(encoding="utf-8")
              + Path("cloudflare/migrations/0011_sqlite_snapshot_gc.sql").read_text(encoding="utf-8"))
    statements, pending = [], ""
    for line in schema.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statements.append(D1Statement(pending.strip()))
            pending = ""
    assert not pending.strip()
    client.batch(statements)
    store = D1SQLiteImageStore(client)
    key = "process-" + str(uuid4())
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        env = dict(os.environ)
        env.update({
            "OPENTERMINALUI_ENV": "testing", "OPENTERMINALUI_DATABASE_STORE": "d1_snapshot",
            "OPENTERMINALUI_D1_SNAPSHOT_EXPERIMENT": "1", "OPENTERMINALUI_D1_LOCAL_TEST": "1",
            "OPENTERMINALUI_PROVIDER_KEYS_STORE": "database",
            "OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"local-test-only-encryption-key!!").decode("ascii"),
            "FMP_API_KEY": "fallback-from-process", "FRED_API_KEY": "fallback-from-process",
            "OPENTERMINALUI_D1_SNAPSHOT_NAME": key, "OPENTERMINALUI_IDENTITY_STORE": "sqlalchemy",
            "OPENTERMINALUI_SAVED_VIEWS_STORE": "sqlalchemy", "OPENTERMINALUI_WATCHLISTS_STORE": "sqlalchemy",
            "OPENTERMINALUI_USER_LAYOUTS_STORE": "sqlalchemy", "D1_GATEWAY_URL": "http://127.0.0.1:8899",
            "D1_BRIDGE_TOKEN": "test-secret-only-not-for-production-1234567890",
            "DATABASE_URL": "sqlite:///" + (root / "fallback.sqlite").as_posix(),
            "OPENTERMINALUI_SQLITE_URL": "sqlite:///" + (root / "cache.sqlite").as_posix(),
            "JWT_SECRET_KEY": "local-test-signing-key-only-not-for-production",
            "BOOTSTRAP_ADMIN_EMAIL": "admin@example.test", "BOOTSTRAP_ADMIN_PASSWORD": "local-test-password-only",
            "AUTH_MIDDLEWARE_ENABLED": "1", "E2E_DEV_AUTH": "0", "TMP": str(root), "TEMP": str(root), "TMPDIR": str(root),
        })
        def run(args, *, overrides=None, success=True):
            child_env = dict(env)
            if overrides:
                child_env.update(overrides)
            result = subprocess.run([sys.executable, *args], env=child_env, capture_output=True, text=True, timeout=90)
            if success:
                assert result.returncode == 0, result.stdout + result.stderr
            else:
                assert result.returncode != 0, "unsafe configuration was accepted"
                assert "test-secret-only-not-for-production" not in result.stderr
            return result
        # Refuse every unsafe mode before contacting D1 or creating a local DB.
        for overrides in (
            {"OPENTERMINALUI_ENV": "production"},
            {"OPENTERMINALUI_D1_SNAPSHOT_EXPERIMENT": "0"},
            {"OPENTERMINALUI_D1_LOCAL_TEST": "0"},
            {"D1_GATEWAY_URL": "https://example.com"},
            {"D1_GATEWAY_URL": "http://127.0.0.1:8899/path"},
            {"OPENTERMINALUI_IDENTITY_STORE": "d1"},
            {"OPENTERMINALUI_WATCHLISTS_STORE": "d1"},
            {"OPENTERMINALUI_SAVED_VIEWS_STORE": "d1"},
            {"OPENTERMINALUI_USER_LAYOUTS_STORE": "d1"},
            {"OPENTERMINALUI_DATABASE_STORE": "unknown"},
        ):
            run(["-c", "import backend.shared.db"], overrides=overrides, success=False)
        assert store.head(key) is None and not (root / "fallback.sqlite").exists()
        run(["-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"])
        assert store.head_info(key).revision == 1
        run(["scripts/seed_admin.py"])
        run(["-m", "cloudflare.snapshot_process_local_integration", "write"])
        run(["-m", "cloudflare.snapshot_process_local_integration", "read"])
        run(["-m", "cloudflare.snapshot_process_local_integration", "realapp"])
        assert b"persisted-test-key-alpha" not in store.head(key).data
        wrong_key = base64.urlsafe_b64encode(b"wrong-test-only-encryption-key!!").decode("ascii")
        failed = run(["-c", "import backend.main"], overrides={"OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY": wrong_key}, success=False)
        assert "cannot decrypt provider configuration" in failed.stderr
        assert "persisted-test-key-alpha" not in failed.stderr
        run(["-m", "cloudflare.snapshot_process_local_integration", "provider_read"])
        run(["-m", "cloudflare.snapshot_process_local_integration", "provider_verify"])
        run(["-m", "cloudflare.snapshot_process_local_integration", "verify"])
        assert not (root / "fallback.sqlite").exists()
        assert not (root / "cache.sqlite").exists(), "instrument services must not create a separate database"
        assert not list(root.glob("openterminalui-d1-local-*")), "temporary snapshots must be cleaned up"
    print("D1 snapshot local mode: migrations, authenticated app, encrypted settings and restart verified")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "realapp":
        real_application_probe()
    elif len(sys.argv) == 2 and sys.argv[1] in {"provider_read", "provider_verify"}:
        provider_probe(sys.argv[1])
    elif len(sys.argv) == 2 and sys.argv[1] in {"write", "read", "verify"}:
        probe(sys.argv[1])
    else:
        integration()
