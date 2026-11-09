"""Test API-key routes and both REST/MCP auth against disposable local D1."""
import hashlib
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
    "JWT_SECRET_KEY": "local-test-signing-key-only-not-for-production", "AUTH_MIDDLEWARE_ENABLED": "1", "E2E_DEV_AUTH": "0",
})
d1 = D1BatchClient(os.environ["D1_GATEWAY_URL"], os.environ["D1_BRIDGE_TOKEN"], allow_insecure_localhost=True)
for name in ("0001_identity_saved_views.sql", "0004_api_keys.sql"):
    schema = Path("cloudflare/migrations", name).read_text(encoding="utf-8")
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
package("backend.mcp", "backend/mcp")
package("backend.agent", "backend/agent")
package("backend.agent.tools", "backend/agent/tools")
shared_db = types.ModuleType("backend.shared.db")
shared_db.Base = declarative_base()
class NoDatabase:
    def query(self, *args): raise AssertionError("SQLAlchemy API keys should not be used")
    def close(self): pass
shared_db.SessionLocal = NoDatabase
sys.modules[shared_db.__name__] = shared_db
api_deps = types.ModuleType("backend.api.deps")
def get_db(): yield NoDatabase()
api_deps.get_db = get_db
sys.modules[api_deps.__name__] = api_deps
# The MCP registry is unrelated to authentication; replace only that dependency.
module = types.ModuleType("backend.mcp.session")
class Registry:
    def names(self): return ["tool"]
module.build_session_registry = lambda principal: Registry()
sys.modules[module.__name__] = module
module = types.ModuleType("backend.agent.tools.envelope")
module.ensure_envelope = lambda value: value
module.err = lambda text: {"error": text}
sys.modules[module.__name__] = module

from backend.cloudflare.identity import D1IdentityRepository
from backend.cloudflare.api_keys import D1APIKeyRepository
from backend.auth.jwt import create_access_token
from backend.auth.middleware import AuthMiddleware
from backend.api.routes.api_keys import router as keys_router
from backend.core.api_key_auth import get_api_key_user
from backend.mcp.auth import authenticate, McpAuthError
from backend.mcp.http_app import build_mcp_router

identity = D1IdentityRepository(d1)
alice = identity.create_user("alice@example.test", "hash-a")
bob = identity.create_user("bob@example.test", "hash-b")
assert alice and bob

def headers(user):
    return {"Authorization": "Bearer " + create_access_token(user.id, user.email, user.role)}
app = FastAPI()
app.add_middleware(AuthMiddleware)
app.include_router(keys_router, prefix="/api")
app.include_router(build_mcp_router(), prefix="/api")
@app.get("/api/v1/quote/test")
async def quote(principal=Depends(get_api_key_user)):
    return {"user_id": principal.user_id}
http = TestClient(app)
created = http.post("/api/settings/api-keys", headers=headers(alice), json={"name": "desktop", "permissions": "read_write"})
assert created.status_code == 200, created.text
key = created.json()["key"]
key_id = created.json()["id"]
assert http.post("/api/settings/api-keys", headers=headers(alice), json={"name": "bad", "permissions": "admin"}).status_code == 422
listed = http.get("/api/settings/api-keys", headers=headers(alice))
assert listed.status_code == 200 and len(listed.json()) == 1
assert key not in listed.text and "key_hash" not in listed.text
assert http.get("/api/settings/api-keys", headers=headers(bob)).json() == []
assert http.delete(f"/api/settings/api-keys/{key_id}", headers=headers(bob)).status_code == 404
assert http.get("/api/v1/quote/test").status_code == 401
assert http.get("/api/v1/quote/test", headers={"X-API-Key": key}).json()["user_id"] == alice.id
assert http.get("/api/v1/quote/test", headers={"X-API-Key": key + "x"}).status_code == 401
manifest = http.get("/api/mcp/manifest", headers={"X-API-Key": key})
assert manifest.status_code == 200 and manifest.json()["permissions"] == "read_write", manifest.text
assert authenticate(key).allow_writes
rows = d1.batch([D1Statement("SELECT key_hash,last_used_at FROM api_keys WHERE id=?", [key_id])])[0]["results"]
assert rows[0]["key_hash"] == hashlib.sha256(key.encode()).hexdigest() and rows[0]["last_used_at"] is not None
assert key not in str(rows)

# Equal prefixes must not cause a different key to be authenticated or rejected.
repository = D1APIKeyRepository(d1)
first = "otui_abcdefg" + "a" * 32
second = "otui_abcdefg" + "b" * 32
for raw in (first, second):
    assert repository.create(bob.id, "collision", raw[:12], hashlib.sha256(raw.encode()).hexdigest(), "read")
assert first[:12] == second[:12]
assert authenticate(first).user_id == bob.id and authenticate(second).user_id == bob.id
assert not authenticate(first).allow_writes

# Missing backend and production opt-in fail closed across both API surfaces.
os.environ["D1_BRIDGE_TOKEN"] = "wrong-secret-only-not-for-production-1234567"
for path, auth in (("/api/settings/api-keys", headers(alice)), ("/api/v1/quote/test", {"X-API-Key": key}),
                   ("/api/mcp/manifest", {"X-API-Key": key})):
    response = http.get(path, headers=auth)
    assert response.status_code == 503 and "wrong-secret" not in response.text, response.text
os.environ["D1_BRIDGE_TOKEN"] = "test-secret-only-not-for-production-1234567890"
os.environ["OPENTERMINALUI_ENV"] = "production"
assert http.get("/api/mcp/manifest", headers={"X-API-Key": key}).status_code == 503
os.environ["OPENTERMINALUI_ENV"] = "testing"
assert http.delete(f"/api/settings/api-keys/{key_id}", headers=headers(alice)).status_code == 200
assert http.get("/api/v1/quote/test", headers={"X-API-Key": key}).status_code == 401
assert http.get("/api/mcp/manifest", headers={"X-API-Key": key}).status_code == 401
try:
    authenticate(key)
except McpAuthError:
    pass
else:
    raise AssertionError("revoked keys must not authenticate")
d1.batch([D1Statement("DELETE FROM users WHERE id=?", [bob.id])])
try:
    authenticate(first)
except McpAuthError:
    pass
else:
    raise AssertionError("deleting a user must revoke its API keys")
assert d1.batch([D1Statement("SELECT COUNT(*) AS n FROM api_keys WHERE user_id=?", [bob.id])])[0]["results"][0]["n"] == 0
# The original SQLAlchemy key-management and auth paths remain functional.
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from backend.models.user import User, UserRole
from backend.models.api_key import APIKeyORM
from backend.api.routes import api_keys as keys_module
from backend.core import api_key_auth as key_auth_module
from backend.mcp import auth as mcp_auth_module
os.environ["OPENTERMINALUI_IDENTITY_STORE"] = "sqlalchemy"
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
shared_db.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
with Session() as db:
    legacy_user = User(email="legacy@example.test", hashed_password="x", role=UserRole.VIEWER)
    db.add(legacy_user)
    db.commit()
    db.refresh(legacy_user)
    legacy_headers = {"Authorization": "Bearer " + create_access_token(legacy_user.id, legacy_user.email, legacy_user.role.value)}
def sqlite_db():
    with Session() as db:
        yield db
mcp_auth_module.SessionLocal = Session
legacy_app = FastAPI()
legacy_app.add_middleware(AuthMiddleware)
legacy_app.state.db_session_factory = Session
for dependency in (get_db, keys_module.get_db, key_auth_module.get_db):
    legacy_app.dependency_overrides[dependency] = sqlite_db
legacy_app.include_router(keys_router, prefix="/api")
legacy_app.include_router(build_mcp_router(), prefix="/api")
@legacy_app.get("/api/v1/quote/test")
async def legacy_quote(principal=Depends(get_api_key_user)):
    return {"user_id": principal.user_id}
legacy_http = TestClient(legacy_app)
legacy_created = legacy_http.post("/api/settings/api-keys", headers=legacy_headers, json={"name": "legacy"})
assert legacy_created.status_code == 200, legacy_created.text
legacy_key = legacy_created.json()["key"]
assert legacy_http.get("/api/v1/quote/test", headers={"X-API-Key": legacy_key}).status_code == 200
assert legacy_http.get("/api/mcp/manifest", headers={"X-API-Key": legacy_key}).status_code == 200
assert legacy_http.delete(f"/api/settings/api-keys/{legacy_created.json()['id']}", headers=legacy_headers).status_code == 200
assert legacy_http.get("/api/mcp/manifest", headers={"X-API-Key": legacy_key}).status_code == 401
engine.dispose()
print("D1 API keys: ownership, REST/MCP auth, prefix collisions, revoke, cascade and original storage verified")
