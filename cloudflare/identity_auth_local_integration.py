"""Exercise experimental identity against real routes, middleware and local D1.

Unrelated services and the original SQLAlchemy session are stubbed; no business
routes are migrated by this test. Uses a disposable local D1 database only.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import sys
import types
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import declarative_base

from backend.cloudflare.d1 import D1BatchClient, D1Statement

os.environ.update({
    "OPENTERMINALUI_ENV": "testing", "OPENTERMINALUI_IDENTITY_STORE": "d1",
    "OPENTERMINALUI_D1_IDENTITY_EXPERIMENT": "1", "OPENTERMINALUI_D1_LOCAL_TEST": "1",
    "D1_GATEWAY_URL": "http://127.0.0.1:8899", "D1_BRIDGE_TOKEN": "test-secret-only-not-for-production-1234567890",
    "JWT_SECRET_KEY": "local-test-signing-key-only-not-for-production", "RATE_LIMIT_ENABLED": "0",
    "AUTH_MIDDLEWARE_ENABLED": "1", "E2E_DEV_AUTH": "0",
})
d1 = D1BatchClient(os.environ["D1_GATEWAY_URL"], os.environ["D1_BRIDGE_TOKEN"], allow_insecure_localhost=True)
schema = Path("cloudflare/migrations/0001_identity_saved_views.sql").read_text(encoding="utf-8")
d1.batch([D1Statement(sql.strip()) for sql in schema.split(";") if sql.strip()])
d1.batch([D1Statement("DELETE FROM users")])
root = Path.cwd()
def package(name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(root / path)]
    sys.modules[name] = module
    return module
package("backend.models", "backend/models")
package("backend.auth", "backend/auth")
package("backend.equity.routes", "backend/equity/routes")
shared_db = types.ModuleType("backend.shared.db")
shared_db.Base = declarative_base()
class NoDatabase:
    def query(self, *args): raise AssertionError("SQLAlchemy identity should not be used")
    def close(self): pass
shared_db.SessionLocal = NoDatabase
sys.modules[shared_db.__name__] = shared_db
api_deps = types.ModuleType("backend.api.deps")
def get_db(): yield NoDatabase()
api_deps.get_db = get_db
sys.modules[api_deps.__name__] = api_deps

from backend.auth.deps import get_current_user, require_role
from backend.auth.middleware import AuthMiddleware
from backend.equity.routes.auth import router

app = FastAPI()
app.add_middleware(AuthMiddleware)
app.include_router(router)
@app.get("/api/me")
def me(user=Depends(get_current_user)):
    return {"id": user.id, "email": user.email, "role": user.role}
@app.get("/api/admin")
def admin(user=Depends(require_role("admin"))):
    return {"id": user.id}
http = TestClient(app)

# Bootstrap uses the same D1 store and is idempotent.
from scripts.seed_admin import main as seed_admin
os.environ.update({"BOOTSTRAP_ADMIN_EMAIL": "admin@example.test", "BOOTSTRAP_ADMIN_PASSWORD": "bootstrap-password"})
assert seed_admin() == 0 and seed_admin() == 0
assert len(d1.batch([D1Statement("SELECT id FROM users")])[0]["results"]) == 1
admin_login = http.post("/api/auth/login", json={"email": "admin@example.test", "password": "bootstrap-password"})
assert admin_login.status_code == 200
assert http.get("/api/admin", headers={"Authorization": "Bearer " + admin_login.json()["access_token"]}).status_code == 200
d1.batch([D1Statement("DELETE FROM users")])

def post(path, payload): return http.post("/api/auth/" + path, json=payload)
def bearer(token): return {"Authorization": "Bearer " + token}
password = "correct-horse-battery"
registration = post("register", {"email": " Alice@Example.test ", "password": password})
assert registration.status_code == 200, registration.text
assert registration.json()["email"] == "alice@example.test" and registration.json()["role"] == "viewer"
assert post("register", {"email": "alice@example.test", "password": password}).status_code == 409
assert post("login", {"email": "alice@example.test", "password": "wrong-pass"}).status_code == 401
login = post("login", {"email": "alice@example.test", "password": password})
assert login.status_code == 200, login.text
pair = login.json()
assert http.get("/api/me", headers=bearer(pair["access_token"])).json()["id"] == registration.json()["id"]
assert http.get("/api/admin", headers=bearer(pair["access_token"])).status_code == 403
assert http.get("/api/me", headers=bearer(pair["refresh_token"])).status_code == 401

# Refresh is one-use even with concurrent requests.
with ThreadPoolExecutor(max_workers=2) as pool:
    responses = list(pool.map(lambda _: post("refresh", {"refresh_token": pair["refresh_token"]}), range(2)))
assert sorted(response.status_code for response in responses) == [200, 401]
new_pair = next(response.json() for response in responses if response.status_code == 200)
assert post("refresh", {"refresh_token": pair["refresh_token"]}).status_code == 401

# The existing dev-only reset revokes refresh tokens and updates the password.
assert post("forgot-access", {"email": "alice@example.test", "new_password": "replacement-password"}).status_code == 204
assert post("refresh", {"refresh_token": new_pair["refresh_token"]}).status_code == 401
assert post("login", {"email": "alice@example.test", "password": password}).status_code == 401
replacement = post("login", {"email": "alice@example.test", "password": "replacement-password"})
assert replacement.status_code == 200, replacement.text

# A protected dependency also works without middleware populating request.state.
no_middleware = FastAPI()
@no_middleware.get("/api/me")
def bare_me(user=Depends(get_current_user)):
    return {"id": user.id}
assert TestClient(no_middleware).get("/api/me", headers=bearer(replacement.json()["access_token"])).json()["id"] == registration.json()["id"]
os.environ["E2E_DEV_AUTH"] = "1"
assert TestClient(no_middleware).get("/api/me").status_code == 401
os.environ["E2E_DEV_AUTH"] = "0"

# Gateway failure and any attempt to use this experimental mode in production fail closed.
os.environ["D1_BRIDGE_TOKEN"] = "wrong-secret-only-not-for-production-1234567"
assert seed_admin() == 1
error = http.get("/api/me", headers=bearer(replacement.json()["access_token"]))
assert error.status_code == 503 and "wrong-secret" not in error.text
assert post("login", {"email": "alice@example.test", "password": "replacement-password"}).status_code == 503
os.environ["D1_BRIDGE_TOKEN"] = "test-secret-only-not-for-production-1234567890"
os.environ["OPENTERMINALUI_ENV"] = "production"
assert http.get("/api/me", headers=bearer(replacement.json()["access_token"])).status_code == 503
assert post("login", {"email": "alice@example.test", "password": "replacement-password"}).status_code == 503
assert post("forgot-access", {"email": "alice@example.test", "new_password": "not-allowed-password"}).status_code == 403
os.environ["OPENTERMINALUI_ENV"] = "testing"
d1.batch([D1Statement("DELETE FROM users")])
assert http.get("/api/me", headers=bearer(replacement.json()["access_token"])).status_code == 401

# Original SQLAlchemy mode remains functional with its own real SQLite session.
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from backend.models.user import User, RefreshToken
os.environ["OPENTERMINALUI_IDENTITY_STORE"] = "sqlalchemy"
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
shared_db.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
def sqlite_db():
    with Session() as db:
        yield db
legacy = FastAPI()
legacy.add_middleware(AuthMiddleware)
legacy.state.db_session_factory = Session
legacy.dependency_overrides[get_db] = sqlite_db
legacy.include_router(router)
@legacy.get("/api/me")
def legacy_me(user=Depends(get_current_user)):
    return {"id": user.id}
legacy_http = TestClient(legacy)
assert legacy_http.post("/api/auth/register", json={"email": "legacy@example.test", "password": password}).status_code == 200
legacy_pair = legacy_http.post("/api/auth/login", json={"email": "legacy@example.test", "password": password})
assert legacy_pair.status_code == 200, legacy_pair.text
assert legacy_http.get("/api/me", headers=bearer(legacy_pair.json()["access_token"])).status_code == 200
assert legacy_http.post("/api/auth/refresh", json={"refresh_token": legacy_pair.json()["refresh_token"]}).status_code == 200
engine.dispose()
print("D1 auth routes: bootstrap, login, middleware, concurrent refresh, fail-closed behavior and original storage verified")
