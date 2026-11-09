"""Exercise the real saved-view HTTP and MCP paths against disposable local D1.

Only authentication and unrelated SQLAlchemy dependencies are replaced; all
saved-view operations use the real repository, gateway and local D1 database.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.cloudflare.d1 import D1BatchClient, D1Statement

os.environ.update({
    "OPENTERMINALUI_SAVED_VIEWS_STORE": "d1",
    "D1_GATEWAY_URL": "http://127.0.0.1:8899",
    "D1_BRIDGE_TOKEN": "test-secret-only-not-for-production-1234567890",
    "OPENTERMINALUI_ENV": "testing",
    "OPENTERMINALUI_D1_LOCAL_TEST": "1",
})
root = Path.cwd()
client = D1BatchClient(os.environ["D1_GATEWAY_URL"], os.environ["D1_BRIDGE_TOKEN"], allow_insecure_localhost=True)
client.batch([D1Statement("DELETE FROM users")])
client.batch([
    D1Statement("INSERT INTO users (id,email,hashed_password,role,created_at) VALUES (?,?,?,?,?)",
                [user, f"{user}@example.test", "unused", "VIEWER", "2026-11-09 00:00:00"])
    for user in ("alice", "bob")
])

# Avoid loading unrelated provider dependencies in this focused integration.
def package(name: str, path: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__path__ = [str(root / path)]
    sys.modules[name] = module
    return module

package("backend.saved_views", "backend/saved_views")
models = types.ModuleType("backend.saved_views.models")
class SavedViewORM:  # should not be queried in D1 mode
    pass
models.SavedViewORM = SavedViewORM
sys.modules[models.__name__] = models
api_deps = types.ModuleType("backend.api.deps")
class EmptyQuery:
    def filter(self, *args): return self
    def all(self): return []
class Session:
    def query(self, model):
        assert model is not SavedViewORM, "saved views must use D1"
        return EmptyQuery()
    def close(self): pass
def get_db(): yield Session()
api_deps.get_db = get_db
sys.modules[api_deps.__name__] = api_deps
package("backend.auth", "backend/auth")
auth_deps = types.ModuleType("backend.auth.deps")
def get_current_user(request: Request): return SimpleNamespace(id=request.headers["x-test-user"])
auth_deps.get_current_user = get_current_user
sys.modules[auth_deps.__name__] = auth_deps
package("backend.models", "backend/models")
user_models = types.ModuleType("backend.models.user")
user_models.User = type("User", (), {})
sys.modules[user_models.__name__] = user_models
shared_db = types.ModuleType("backend.shared.db")
shared_db.SessionLocal = Session
sys.modules[shared_db.__name__] = shared_db
package("backend.agent", "backend/agent")
package("backend.agent.tools", "backend/agent/tools")
package("backend.mcp", "backend/mcp")

from backend.saved_views.routes import router
from backend.mcp import resources
from backend.mcp.resources import list_resources, read_resource

app = FastAPI()
app.include_router(router)
http = TestClient(app)
alice = {"x-test-user": "alice"}
bob = {"x-test-user": "bob"}
response = http.post("/saved-views", headers=alice, json={"name": " View ", "scope": "workspace", "page": "/home", "payload": {"value": 1}})
assert response.status_code == 201, response.text
view_id = response.json()["id"]
assert http.get("/saved-views", headers=alice).json()["items"][0]["id"] == view_id
assert http.get("/saved-views", headers=bob).json()["items"] == []
assert http.patch(f"/saved-views/{view_id}", headers=bob, json={"name": "stolen"}).status_code == 404
assert http.delete(f"/saved-views/{view_id}", headers=bob).status_code == 404
response = http.patch(f"/saved-views/{view_id}", headers=alice, json={"payload": {"value": 2}})
assert response.status_code == 200 and response.json()["payload"] == {"value": 2}
assert read_resource(f"otui://saved-view/{view_id}", "alice")["data"]["payload"] == {"value": 2}
class WatchlistORM:
    user_id = "user_id"
sys.modules["backend.models"].SavedViewORM = SavedViewORM
sys.modules["backend.models"].WatchlistORM = WatchlistORM
assert any(item["uri"] == f"otui://saved-view/{view_id}" for item in list_resources("alice"))
assert not any(item["uri"] == f"otui://saved-view/{view_id}" for item in list_resources("bob"))
assert read_resource(f"otui://saved-view/{view_id}", "bob")["error"]["code"] == "not_found"
assert http.delete(f"/saved-views/{view_id}", headers=alice).status_code == 200
assert read_resource(f"otui://saved-view/{view_id}", "alice")["error"]["code"] == "not_found"
from backend.cloudflare.d1 import D1BatchOutcomeUnknown
class Unavailable:
    def list(self, user_id): raise D1BatchOutcomeUnknown("sensitive details")
resources.get_d1_saved_views = lambda: Unavailable()
error = read_resource("otui://saved-view", "alice")
assert error["error"]["code"] == "storage_unavailable" and "sensitive details" not in str(error)
print("D1 saved views: HTTP and MCP CRUD with cross-user isolation verified")
