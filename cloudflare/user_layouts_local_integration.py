"""Exercise D1 launchpad layout routes against a disposable local D1 database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Column, DateTime, JSON, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.cloudflare.d1 import D1BatchClient, D1Statement

os.environ.update({
    "OPENTERMINALUI_ENV": "testing", "OPENTERMINALUI_IDENTITY_STORE": "d1",
    "OPENTERMINALUI_D1_IDENTITY_EXPERIMENT": "1", "OPENTERMINALUI_D1_LOCAL_TEST": "1",
    "OPENTERMINALUI_USER_LAYOUTS_STORE": "d1", "D1_GATEWAY_URL": "http://127.0.0.1:8899",
    "D1_BRIDGE_TOKEN": "test-secret-only-not-for-production-1234567890",
})
d1 = D1BatchClient(os.environ["D1_GATEWAY_URL"], os.environ["D1_BRIDGE_TOKEN"], allow_insecure_localhost=True)
schema = Path("cloudflare/migrations/0008_user_layouts.sql").read_text(encoding="utf-8").split(";", 1)[1]
d1.batch([D1Statement(sql.strip()) for sql in schema.split(";") if sql.strip()])
d1.batch([D1Statement("DELETE FROM user_layouts")])

# Isolate this route while retaining a real SQLAlchemy-backed fallback.
base = declarative_base()
class UserLayoutORM(base):
    __tablename__ = "user_layouts"
    id = Column(String, primary_key=True, default=lambda: "legacy-id")
    user_key = Column(String, unique=True)
    layouts_json = Column(JSON)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
models = types.ModuleType("backend.models")
models.UserLayoutORM = UserLayoutORM
sys.modules["backend.models"] = models
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
api_deps = types.ModuleType("backend.api.deps")
def get_db():
    with Session() as db:
        yield db
api_deps.get_db = get_db
sys.modules["backend.api.deps"] = api_deps
from backend.api.routes.user_layouts import router
from backend.cloudflare.user_layouts_storage import get_d1_user_layouts

app = FastAPI()
@app.middleware("http")
async def current_user(request: Request, call_next):
    user = request.headers.get("x-test-user")
    if user:
        request.state.current_user = SimpleNamespace(id=user)
    return await call_next(request)
app.include_router(router)
http = TestClient(app)
alice = {"x-test-user": "alice"}
bob = {"x-test-user": "bob"}
assert http.get("/user/layouts", headers=alice).json() == {"items": []}
layouts = [{"name": "??", "panels": [{"x": 3, "enabled": True}]}]
assert http.put("/user/layouts", headers=alice, json={"items": layouts}).json() == {"ok": True, "count": 1}
assert http.get("/user/layouts", headers=alice).json() == {"items": layouts}
assert http.get("/user/layouts", headers=bob).json() == {"items": []}
assert http.put("/user/layouts", json={"items": [{"name": "anonymous"}]}).status_code == 200
assert http.get("/user/layouts").json()["items"] == [{"name": "anonymous"}]
row = d1.batch([D1Statement("SELECT id FROM user_layouts WHERE user_key=?", ["alice"])])[0]["results"][0]
with ThreadPoolExecutor(max_workers=8) as pool:
    results = list(pool.map(lambda i: http.put("/user/layouts", headers=alice, json={"items": [{"value": i}]}), range(20)))
assert all(response.status_code == 200 for response in results)
rows = d1.batch([D1Statement("SELECT id FROM user_layouts WHERE user_key=?", ["alice"])])[0]["results"]
assert rows == [row] and http.get("/user/layouts", headers=alice).json()["items"][0]["value"] in range(20)
os.environ["D1_BRIDGE_TOKEN"] = "wrong-secret-only-not-for-production-1234567"
for response in (http.get("/user/layouts", headers=alice), http.put("/user/layouts", headers=alice, json={"items": []})):
    assert response.status_code == 503 and "wrong-secret" not in response.text
os.environ["D1_BRIDGE_TOKEN"] = "test-secret-only-not-for-production-1234567890"
os.environ["OPENTERMINALUI_ENV"] = "production"
assert http.get("/user/layouts", headers=alice).status_code == 503
assert http.put("/user/layouts", headers=alice, json={"items": []}).status_code == 503
os.environ["OPENTERMINALUI_ENV"] = "testing"
os.environ["OPENTERMINALUI_IDENTITY_STORE"] = "sqlalchemy"
try:
    get_d1_user_layouts()
except RuntimeError:
    pass
else:
    raise AssertionError("D1 layouts must require isolated D1 identity")
os.environ["OPENTERMINALUI_USER_LAYOUTS_STORE"] = "sqlalchemy"
assert http.put("/user/layouts", headers=alice, json={"items": layouts}).status_code == 200
assert http.get("/user/layouts", headers=alice).json() == {"items": layouts}
assert http.get("/user/layouts", headers=bob).json() == {"items": []}
with Session() as db:
    assert db.query(UserLayoutORM).count() == 1
engine.dispose()
d1.batch([D1Statement("DELETE FROM user_layouts")])
print("D1 layouts: HTTP isolation, anonymous compatibility, concurrent upsert, failure handling and SQLAlchemy fallback verified")
