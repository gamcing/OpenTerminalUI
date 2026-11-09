import pytest

from backend.cloudflare import snapshot_storage


@pytest.fixture
def container_env(monkeypatch):
    settings = {
        "OPENTERMINALUI_ENV": "production", "OPENTERMINALUI_DATABASE_STORE": "d1_snapshot",
        "OPENTERMINALUI_D1_SNAPSHOT_EXPERIMENT": "1", "OPENTERMINALUI_CF_CONTAINER": "1",
        "OPENTERMINALUI_D1_CONTAINER_EXPERIMENT": "1", "D1_GATEWAY_URL": "http://openterminal-d1.internal",
        "D1_BRIDGE_TOKEN": "a" * 32,
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    for key in ("OPENTERMINALUI_IDENTITY_STORE", "OPENTERMINALUI_SAVED_VIEWS_STORE",
                "OPENTERMINALUI_WATCHLISTS_STORE", "OPENTERMINALUI_USER_LAYOUTS_STORE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(snapshot_storage, "LocalSnapshotStorage", lambda client, name, **kwargs: (client, name, kwargs))


def test_explicit_container_snapshot_uses_private_origin(container_env):
    client, name, kwargs = snapshot_storage.local_snapshot_storage_from_env()
    assert kwargs == {"fence_on_start": True}
    assert client._endpoint == "http://openterminal-d1.internal/batch"
    assert name == "openterminalui-local"


@pytest.mark.parametrize("name,value", [
    ("OPENTERMINALUI_ENV", "test"),
    ("OPENTERMINALUI_CF_CONTAINER", "0"),
    ("OPENTERMINALUI_D1_CONTAINER_EXPERIMENT", "0"),
    ("OPENTERMINALUI_D1_SNAPSHOT_EXPERIMENT", "0"),
    ("D1_GATEWAY_URL", "http://127.0.0.1:8899"),
    ("D1_GATEWAY_URL", "http://openterminal-d1.internal:80"),
    ("D1_GATEWAY_URL", "http://user@openterminal-d1.internal"),
    ("D1_GATEWAY_URL", "http://openterminal-d1.internal/path"),
    ("D1_BRIDGE_TOKEN", "short"),
    ("OPENTERMINALUI_IDENTITY_STORE", "d1"),
    ("OPENTERMINALUI_SAVED_VIEWS_STORE", "d1"),
])
def test_unsafe_container_configuration_fails_closed(container_env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError):
        snapshot_storage.local_snapshot_storage_from_env()
