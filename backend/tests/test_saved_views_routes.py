from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.api.deps import get_db
from backend.auth.deps import get_current_user
from backend.models.user import User, UserRole
from backend.saved_views.models import SavedViewORM
from backend.saved_views.routes import router
from backend.shared.db import Base


def test_saved_view_routes_keep_using_sqlalchemy_by_default(monkeypatch) -> None:
    monkeypatch.delenv("OPENTERMINALUI_SAVED_VIEWS_STORE", raising=False)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, SavedViewORM.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(User(id="alice", email="alice@example.test", hashed_password="hash", role=UserRole.VIEWER))
        db.commit()
    app = FastAPI()
    app.include_router(router)

    def db_override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="alice")
    client = TestClient(app)
    created = client.post("/saved-views", json={"name": "View", "page": "/home", "payload": {"x": 1}})
    assert created.status_code == 201
    view_id = created.json()["id"]
    assert client.get("/saved-views").json()["items"][0]["id"] == view_id
    updated = client.patch(f"/saved-views/{view_id}", json={"payload": {"x": 2}})
    assert updated.status_code == 200 and updated.json()["payload"] == {"x": 2}
    assert client.delete(f"/saved-views/{view_id}").json() == {"ok": True}
    assert client.delete(f"/saved-views/{view_id}").status_code == 404
