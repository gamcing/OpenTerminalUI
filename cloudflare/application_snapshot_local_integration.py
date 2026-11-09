"""Exercise project migrations, authentication and routes using disposable D1 images."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import types
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from backend.cloudflare.d1 import D1BatchClient, D1Statement
from backend.cloudflare.sqlite_images import D1SQLiteImageStore
from backend.cloudflare.sqlalchemy_sqlite_prototype import SQLiteSnapshotCoordinator

os.environ.update({
    "OPENTERMINALUI_ENV": "testing", "OPENTERMINALUI_IDENTITY_STORE": "sqlalchemy",
    "OPENTERMINALUI_WATCHLISTS_STORE": "sqlalchemy", "AUTH_MIDDLEWARE_ENABLED": "1", "E2E_DEV_AUTH": "0",
    "JWT_SECRET_KEY": "local-test-signing-key-only-not-for-production",
    "BOOTSTRAP_ADMIN_EMAIL": "admin@example.test", "BOOTSTRAP_ADMIN_PASSWORD": "local-test-password-only",
})
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
key = "application-" + str(uuid4())

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    os.environ["DATABASE_URL"] = "sqlite:///" + (root / "unused-default.sqlite").as_posix()
    os.environ["OPENTERMINALUI_SQLITE_URL"] = "sqlite:///" + (root / "unused-cache.sqlite").as_posix()
    def directory(name):
        path = root / name
        path.mkdir()
        return path
    def upgrade(engine):
        config = Config("backend/alembic.ini")
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    def factory(engine):
        return sessionmaker(bind=engine, autocommit=False, autoflush=False)

    # SQLite DDL and Alembic's version table must roll back together if startup fails.
    failed_key = key + "-failed"
    failed = SQLiteSnapshotCoordinator(store, failed_key, directory("failed"))
    failed_engine = failed.create_engine()
    try:
        config = Config("backend/alembic.ini")
        with failed_engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            raise RuntimeError("simulated startup failure")
    except RuntimeError as exc:
        assert str(exc) == "simulated startup failure"
    else:
        raise AssertionError("startup failure must propagate")
    assert store.head(failed_key) is None
    assert inspect(failed_engine).get_table_names() == []
    failed_engine.dispose()

    manager = SQLiteSnapshotCoordinator(store, key, directory("first"))
    engine = manager.create_engine()
    upgrade(engine)
    assert store.head_info(key).revision == 1
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0014_research_review"
    # The real startup also registers additional application models.
    import backend.shared.db as shared
    shared.engine = engine
    shared.SessionLocal = factory(engine)
    shared.init_db()
    assert {"users", "watchlists", "saved_views", "holdings", "agent_proposals"}.issubset(inspect(engine).get_table_names())
    from scripts.seed_admin import main as seed_admin
    assert seed_admin() == 0 and seed_admin() == 0
    from backend.models.user import User, UserRole
    from backend.models.core import Holding
    from backend.saved_views.models import SavedViewORM
    with shared.SessionLocal() as session:
        admin = session.query(User).one()
        admin_id, email, role = admin.id, admin.email, admin.role.value
        session.add(User(id="other-user", email="other@example.test", hashed_password="unused", role=UserRole.VIEWER))
        session.add(Holding(ticker="SPY", quantity=2, avg_buy_price=100, buy_date="2026-01-01"))
        session.add(SavedViewORM(id="view-one", user_id=admin_id, name="Test", scope="workspace", page="/",
                                 payload_json={"layout": [1, 2]}, description=""))
        session.commit()

    # Isolate unrelated data-fetching dependencies, while using the real auth
    # middleware, JWT implementation, watchlist router and application models.
    holder = {"factory": shared.SessionLocal}
    api_deps = types.ModuleType("backend.api.deps")
    def get_db():
        with holder["factory"]() as session:
            yield session
    api_deps.get_db = get_db
    sys.modules["backend.api.deps"] = api_deps
    from backend.auth.jwt import create_access_token
    from backend.auth.middleware import AuthMiddleware
    from backend.api.routes.watchlists import router
    def make_app(session_factory):
        app = FastAPI()
        app.state.db_session_factory = session_factory
        app.add_middleware(AuthMiddleware)
        app.include_router(router)
        return app
    admin_headers = {"Authorization": "Bearer " + create_access_token(admin_id, email, role)}
    other_headers = {"Authorization": "Bearer " + create_access_token("other-user", "other@example.test", "viewer")}
    http = TestClient(make_app(holder["factory"]))
    assert http.get("/api/watchlists").status_code == 401
    response = http.post("/api/watchlists", headers=admin_headers, json={"name": "persistent", "symbols": ["spy", "msft"]})
    assert response.status_code == 200, response.text
    watchlist_id = response.json()["id"]
    assert response.json()["symbols"] == ["SPY", "MSFT"]
    assert http.put("/api/watchlists/" + watchlist_id, headers=other_headers, json={"name": "stolen"}).status_code == 404
    revision = store.head_info(key).revision
    http.close()
    engine.dispose()

    recovered = SQLiteSnapshotCoordinator(store, key, directory("recovered"))
    recovered_engine = recovered.create_engine()
    holder["factory"] = factory(recovered_engine)
    shared.engine = recovered_engine
    shared.SessionLocal = holder["factory"]
    upgrade(recovered_engine)
    assert store.head_info(key).revision == revision  # already-current migration is read-only
    assert seed_admin() == 0
    with holder["factory"]() as session:
        assert session.query(User).count() == 2
        assert session.query(Holding).one().ticker == "SPY"
        assert session.query(SavedViewORM).one().payload_json == {"layout": [1, 2]}
    http = TestClient(make_app(holder["factory"]))
    response = http.get("/api/watchlists", headers=admin_headers)
    assert response.status_code == 200 and response.json()[0]["id"] == watchlist_id
    assert http.post("/api/watchlists/" + watchlist_id + "/symbols", headers=admin_headers, json=["NVDA"]).status_code == 200
    assert http.get("/api/watchlists", headers=admin_headers).json()[0]["symbols"] == ["SPY", "MSFT", "NVDA"]
    http.close()
    recovered_engine.dispose()

    # The standard asynchronous CLI path must continue working independently.
    standard_env = dict(os.environ)
    standard_env["DATABASE_URL"] = "sqlite+aiosqlite:///" + (root / "standard.sqlite").as_posix()
    completed = subprocess.run([sys.executable, "-m", "alembic", "-c", "backend/alembic.ini", "upgrade", "head"],
                               env=standard_env, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    with closing(sqlite3.connect(str(root / "standard.sqlite"))) as check:
        assert check.execute("SELECT version_num FROM alembic_version").fetchone() == ("0014_research_review",)

print("Application snapshot prototype: Alembic rollback, model startup, seed, auth, routes and restart verified")
