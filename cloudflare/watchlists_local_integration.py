"""Test watchlist HTTP, MCP and read consumers against disposable local D1."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, DateTime, JSON, String
from sqlalchemy.orm import declarative_base
from backend.cloudflare.d1 import D1BatchClient, D1Statement

os.environ.update({
    "OPENTERMINALUI_ENV": "testing", "OPENTERMINALUI_IDENTITY_STORE": "d1",
    "OPENTERMINALUI_D1_IDENTITY_EXPERIMENT": "1", "OPENTERMINALUI_D1_LOCAL_TEST": "1",
    "OPENTERMINALUI_WATCHLISTS_STORE": "d1", "D1_GATEWAY_URL": "http://127.0.0.1:8899",
    "D1_BRIDGE_TOKEN": "test-secret-only-not-for-production-1234567890",
    "JWT_SECRET_KEY": "local-test-signing-key-only-not-for-production", "AUTH_MIDDLEWARE_ENABLED": "1", "E2E_DEV_AUTH": "0",
})
d1 = D1BatchClient(os.environ["D1_GATEWAY_URL"], os.environ["D1_BRIDGE_TOKEN"], allow_insecure_localhost=True)
for name in ("0001_identity_saved_views.sql", "0006_watchlists.sql"):
    schema = Path("cloudflare/migrations", name).read_text(encoding="utf-8")
    d1.batch([D1Statement(sql.strip()) for sql in schema.split(";") if sql.strip()])
d1.batch([D1Statement("DELETE FROM users")])
root = Path.cwd()
def package(name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(root / path)]
    sys.modules[name] = module
    return module
models = package("backend.models", "backend/models")
package("backend.auth", "backend/auth")
package("backend.mcp", "backend/mcp")
package("backend.agent", "backend/agent")
package("backend.agent.tools", "backend/agent/tools")
package("backend.alerts", "backend/alerts")
package("backend.filings_rag", "backend/filings_rag")
package("backend.filings_watch", "backend/filings_watch")
shared_db = types.ModuleType("backend.shared.db")
shared_db.Base = declarative_base()
class EmptyQuery:
    def filter(self, *args): return self
    def all(self): return []
class NoDatabase:
    def query(self, model):
        assert model is SavedViewORM, "unexpected SQLAlchemy access during D1 watchlist operation"
        return EmptyQuery()
    def close(self): pass
shared_db.SessionLocal = NoDatabase
sys.modules[shared_db.__name__] = shared_db
api_deps = types.ModuleType("backend.api.deps")
def get_db(): yield NoDatabase()
api_deps.get_db = get_db
sys.modules[api_deps.__name__] = api_deps
class WatchlistORM(shared_db.Base):
    __tablename__ = "watchlists"
    id = Column(String, primary_key=True)
    user_id = Column(String)
    name = Column(String)
    symbols_json = Column(JSON)
    column_config_json = Column(JSON)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
class SavedViewORM:
    user_id = "user_id"
models.WatchlistORM, models.SavedViewORM = WatchlistORM, SavedViewORM
model_db = types.ModuleType("backend.db.models")
model_db.WatchlistORM = WatchlistORM
sys.modules[model_db.__name__] = model_db
for name in ("AlertORM", "VirtualOrder", "VirtualOrderStatus", "VirtualPortfolio"):
    setattr(models, name, type(name, (), {}))
paper = types.ModuleType("backend.paper_trading")
paper.get_paper_engine = lambda: None
sys.modules[paper.__name__] = paper
proposal_models = types.ModuleType("backend.models.agent_proposals")
proposal_models.AgentProposal = type("AgentProposal", (), {"id": "id", "user_id": "user_id"})
sys.modules[proposal_models.__name__] = proposal_models
filing_models = types.ModuleType("backend.filings_rag.models")
filing_models.FilingAnalysisORM = type("FilingAnalysisORM", (), {})
filing_models.FilingKnowledgeORM = type("FilingKnowledgeORM", (), {})
sys.modules[filing_models.__name__] = filing_models
filing_settings = types.ModuleType("backend.filings_watch.models")
filing_settings.FilingsWatchEventORM = type("FilingsWatchEventORM", (), {})
filing_settings.FilingsWatchSettingsORM = type("FilingsWatchSettingsORM", (), {})
sys.modules[filing_settings.__name__] = filing_settings

from backend.cloudflare.identity import D1IdentityRepository
from backend.cloudflare.watchlists import D1WatchlistRepository
from backend.auth.jwt import create_access_token
from backend.auth.middleware import AuthMiddleware
from backend.api.routes.watchlists import router
from backend.mcp.resources import list_resources, read_resource
from backend.agent.tools.portfolio_tools import _watchlists
from backend.filings_watch.service import collect_symbols
from backend.agent.proposals import _confirm_watchlist_add, _confirm_watchlist_remove, confirm_proposal
from backend.alerts.actions import _run_add_to_watchlist

identity = D1IdentityRepository(d1)
alice = identity.create_user("alice@example.test", "a")
bob = identity.create_user("bob@example.test", "b")
assert alice and bob

def headers(user):
    return {"Authorization": "Bearer " + create_access_token(user.id, user.email, user.role)}
app = FastAPI()
app.add_middleware(AuthMiddleware)
app.include_router(router)
http = TestClient(app)
# First access creates a single default even when both requests race.
with ThreadPoolExecutor(max_workers=2) as pool:
    defaults = list(pool.map(lambda _: http.get("/api/watchlists", headers=headers(alice)), range(2)))
assert all(response.status_code == 200 for response in defaults)
assert len(defaults[0].json()) == len(defaults[1].json()) == 1
assert defaults[0].json()[0]["id"] == defaults[1].json()[0]["id"]
created = http.post("/api/watchlists", headers=headers(alice), json={"name": "自选", "symbols": [" aapl ", "AAPL", "tsla"], "column_config": {"标签": ["价格"]}})
assert created.status_code == 200, created.text
watchlist_id = created.json()["id"]
assert created.json()["symbols"] == ["AAPL", "TSLA"]
assert http.put(f"/api/watchlists/{watchlist_id}", headers=headers(bob), json={"name": "stolen"}).status_code == 404
assert http.post(f"/api/watchlists/{watchlist_id}/symbols", headers=headers(bob), json=["X"]).status_code == 404
assert http.delete(f"/api/watchlists/{watchlist_id}", headers=headers(bob)).status_code == 404
repo = D1WatchlistRepository(d1)
# Atomic append must retain all updates even with concurrent requests.
symbols = [f"SYM{i}" for i in range(30)]
with ThreadPoolExecutor(max_workers=8) as pool:
    rows = list(pool.map(lambda symbol: repo.add_symbols(alice.id, watchlist_id, [symbol, symbol]), symbols))
assert all(row is not None for row in rows)
assert set(repo.get(alice.id, watchlist_id).symbols_json) == set(symbols) | {"AAPL", "TSLA"}
updated = http.put(f"/api/watchlists/{watchlist_id}", headers=headers(alice), json={"symbols": ["MSFT", "msft"], "column_config": {"nested": {"ok": True}}})
assert updated.status_code == 200 and updated.json()["symbols"] == ["MSFT", "msft"]
added = http.post(f"/api/watchlists/{watchlist_id}/symbols", headers=headers(alice), json=[" msft ", "spy", "spy"])
assert added.json()["symbols"] == ["MSFT", "msft", "SPY"]
removed = http.delete(f"/api/watchlists/{watchlist_id}/symbols/msft", headers=headers(alice))
assert removed.json()["symbols"] == ["msft", "SPY"]
assert read_resource(f"otui://watchlist/{watchlist_id}", alice.id)["data"]["symbols"] == ["msft", "SPY"]
assert read_resource(f"otui://watchlist/{watchlist_id}", bob.id)["error"]["code"] == "not_found"
assert any(item["uri"] == f"otui://watchlist/{watchlist_id}" for item in list_resources(alice.id))
assert not any(item["uri"] == f"otui://watchlist/{watchlist_id}" for item in list_resources(bob.id))
assert any(item["id"] == watchlist_id for item in _watchlists(alice.id)["items"])
assert "SPY" in collect_symbols(NoDatabase(), alice.id, {"symbols_source": "watchlists"})
# Confirmation must reject before claiming or committing an unsupported action.
class ProposalQuery:
    def __init__(self, proposal): self.proposal = proposal
    def filter(self, *args): return self
    def first(self): return self.proposal
    def update(self, *args, **kwargs): raise AssertionError("proposal must not be claimed")
class ProposalSession:
    def __init__(self, proposal): self.proposal = proposal
    def query(self, model): return ProposalQuery(self.proposal)
    def commit(self): raise AssertionError("proposal must not be committed")
for proposal_type in ("watchlist_add", "watchlist_remove"):
    pending = SimpleNamespace(id="proposal-id", user_id=alice.id, type=proposal_type,
                              status="pending", expires_at=datetime(2099, 1, 1))
    try:
        asyncio.run(confirm_proposal(ProposalSession(pending), alice.id, pending.id))
    except ValueError as exc:
        assert str(exc) == "watchlist_storage_unavailable"
    else:
        raise AssertionError("unsupported proposal confirmation must fail")
    assert pending.status == "pending"
# Actions that would write another store must not silently mutate SQLAlchemy.
proposal = SimpleNamespace(user_id=alice.id, payload={"watchlist_id": watchlist_id, "symbol": "X", "symbols": ["X"]})
for operation in (_confirm_watchlist_add, _confirm_watchlist_remove):
    try:
        operation(NoDatabase(), proposal)
    except ValueError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("cross-store watchlist proposal must fail closed")
alert = SimpleNamespace(user_id=alice.id, symbol="X")
assert not _run_add_to_watchlist(NoDatabase(), alert, {"watchlist_id": watchlist_id}, False)["ok"]
os.environ["D1_BRIDGE_TOKEN"] = "wrong-secret-only-not-for-production-1234567"
response = http.get("/api/watchlists", headers=headers(alice))
assert response.status_code == 503 and "wrong-secret" not in response.text
assert read_resource(f"otui://watchlist/{watchlist_id}", alice.id)["error"]["code"] == "storage_unavailable"
os.environ["D1_BRIDGE_TOKEN"] = "test-secret-only-not-for-production-1234567890"
assert http.delete(f"/api/watchlists/{watchlist_id}", headers=headers(alice)).status_code == 200
assert read_resource(f"otui://watchlist/{watchlist_id}", alice.id)["error"]["code"] == "not_found"
d1.batch([D1Statement("DELETE FROM users WHERE id=?", [alice.id])])
assert repo.list(alice.id) == []
# Default SQLAlchemy mode remains usable, including its cross-domain actions.
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from backend.models.user import User, UserRole
from backend.agent.tools import portfolio_tools
from backend.mcp import resources
os.environ["OPENTERMINALUI_IDENTITY_STORE"] = "sqlalchemy"
from backend.cloudflare.watchlist_storage import get_d1_watchlists
try:
    get_d1_watchlists()
except RuntimeError:
    pass
else:
    raise AssertionError("D1 watchlists must not use a separate SQLAlchemy identity store")
os.environ["OPENTERMINALUI_WATCHLISTS_STORE"] = "sqlalchemy"
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
shared_db.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
with Session() as db:
    legacy_user = User(email="legacy@example.test", hashed_password="x", role=UserRole.VIEWER)
    db.add(legacy_user)
    db.commit()
    db.refresh(legacy_user)
    user_id = legacy_user.id
    legacy_headers = {"Authorization": "Bearer " + create_access_token(user_id, legacy_user.email, legacy_user.role.value)}
def sqlite_db():
    with Session() as db:
        yield db
portfolio_tools.SessionLocal = Session
resources.SessionLocal = Session
legacy_app = FastAPI()
legacy_app.add_middleware(AuthMiddleware)
legacy_app.state.db_session_factory = Session
legacy_app.dependency_overrides[get_db] = sqlite_db
legacy_app.include_router(router)
legacy_http = TestClient(legacy_app)
legacy_created = legacy_http.post("/api/watchlists", headers=legacy_headers, json={"name": "legacy", "symbols": [" spy "]})
assert legacy_created.status_code == 200 and legacy_created.json()["symbols"] == ["SPY"]
legacy_id = legacy_created.json()["id"]
assert legacy_http.post(f"/api/watchlists/{legacy_id}/symbols", headers=legacy_headers, json=["msft"]).json()["symbols"] == ["SPY", "MSFT"]
assert resources.read_resource(f"otui://watchlist/{legacy_id}", user_id)["data"]["symbols"] == ["SPY", "MSFT"]
assert portfolio_tools._watchlists(user_id)["items"][0]["id"] == legacy_id
with Session() as db:
    legacy_alert = SimpleNamespace(user_id=user_id, symbol="NASDAQ:AAPL")
    assert _run_add_to_watchlist(db, legacy_alert, {"watchlist_id": legacy_id}, False)["ok"]
    legacy_proposal = SimpleNamespace(user_id=user_id, payload={"watchlist_id": legacy_id, "symbol": "NVDA", "symbols": ["SPY"]})
    assert _confirm_watchlist_add(db, legacy_proposal)["symbol"] == "NVDA"
    assert _confirm_watchlist_remove(db, legacy_proposal)["removed"] == ["SPY"]
    db.commit()
engine.dispose()
print("D1 watchlists: HTTP/MCP ownership, concurrency, read consumers, cross-store guards and original storage verified")
