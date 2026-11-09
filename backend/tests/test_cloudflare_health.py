from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.cloudflare.sqlalchemy_sqlite_prototype import SnapshotCoordinatorError
from backend.main import app
import backend.shared.db as db


def test_health_reflects_snapshot_coordinator_state(monkeypatch):
    class Coordinator:
        poisoned = False

        def check(self):
            if self.poisoned:
                raise SnapshotCoordinatorError("private internal details")

    client = TestClient(app)
    monkeypatch.setattr(db, "snapshot_storage", None)
    response = client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}

    coordinator = Coordinator()
    monkeypatch.setattr(db, "snapshot_storage", SimpleNamespace(coordinator=coordinator))
    assert client.get("/health").status_code == 200
    coordinator.poisoned = True
    response = client.get("/health")
    assert response.status_code == 503
    assert response.headers["X-OpenTerminalUI-Recovery"] == "d1-snapshot"
    assert response.json() == {"detail": "Database recovery required"}
    assert "private internal details" not in response.text
