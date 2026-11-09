"""Failure and integrity checks for encrypted provider settings."""
import os

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from backend.config import provider_key_store as store
import backend.shared.db as database


@pytest.fixture
def configured(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "secrets.sqlite").as_posix())
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE provider_secrets (name TEXT PRIMARY KEY, ciphertext TEXT NOT NULL)"))
    monkeypatch.setenv("OPENTERMINALUI_PROVIDER_KEYS_STORE", "database")
    monkeypatch.setenv("OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(database, "snapshot_storage", object())
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setenv("FMP_API_KEY", "original-process-value")
    monkeypatch.setenv("FRED_API_KEY", "original-process-value")
    yield engine
    engine.dispose()


def test_encrypted_values_restore_and_clear_overrides_process(configured, monkeypatch):
    store.save_provider_keys({"FMP_API_KEY": "secret-value", "FRED_API_KEY": ""})
    with configured.connect() as connection:
        rows = dict(connection.execute(text("SELECT name,ciphertext FROM provider_secrets")).all())
    assert "secret-value" not in rows["FMP_API_KEY"]
    monkeypatch.setenv("FMP_API_KEY", "new-process-value")
    monkeypatch.setenv("FRED_API_KEY", "new-process-value")
    store.restore_provider_keys()
    assert os.environ["FMP_API_KEY"] == "secret-value"
    assert "FRED_API_KEY" not in os.environ


def test_corrupt_or_swapped_ciphertext_fails_before_any_environment_change(configured, monkeypatch):
    store.save_provider_keys({"FMP_API_KEY": "first-secret", "FRED_API_KEY": "second-secret"})
    with configured.begin() as connection:
        token = connection.execute(text("SELECT ciphertext FROM provider_secrets WHERE name='FMP_API_KEY'")).scalar_one()
        connection.execute(text("UPDATE provider_secrets SET ciphertext=:token WHERE name='FRED_API_KEY'"), {"token": token})
    monkeypatch.setenv("FMP_API_KEY", "unchanged-fmp")
    monkeypatch.setenv("FRED_API_KEY", "unchanged-fred")
    with pytest.raises(RuntimeError, match="cannot decrypt"):
        store.restore_provider_keys()
    assert os.environ["FMP_API_KEY"] == "unchanged-fmp"
    assert os.environ["FRED_API_KEY"] == "unchanged-fred"


def test_failed_database_write_does_not_update_environment(configured):
    with configured.begin() as connection:
        connection.execute(text("CREATE TRIGGER reject_fred BEFORE INSERT ON provider_secrets "
                                "WHEN NEW.name='FRED_API_KEY' BEGIN SELECT RAISE(ABORT,'rejected'); END"))
    with pytest.raises(IntegrityError):
        store.save_provider_keys({"FMP_API_KEY": "never-applied", "FRED_API_KEY": "rejected"})
    assert os.environ["FMP_API_KEY"] == "original-process-value"
    with configured.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM provider_secrets")).scalar_one() == 0


def test_missing_key_and_non_snapshot_mode_fail_closed(configured, monkeypatch):
    monkeypatch.delenv("OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY")
    with pytest.raises(RuntimeError, match="encryption key"):
        store.save_provider_keys({"FMP_API_KEY": "test"})
    monkeypatch.setenv("OPENTERMINALUI_PROVIDER_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(database, "snapshot_storage", None)
    with pytest.raises(RuntimeError, match="restricted"):
        store.restore_provider_keys()


def test_migration_matches_model_and_is_repeatable(monkeypatch):
    from importlib import import_module
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect
    from backend.models.provider_secret import ProviderSecret

    migration = import_module("backend.alembic.versions.0015_provider_secrets")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        columns = {item["name"]: item for item in inspect(connection).get_columns("provider_secrets")}
        assert set(columns) == set(ProviderSecret.__table__.columns.keys())
        assert all(columns[column.name]["nullable"] == column.nullable for column in ProviderSecret.__table__.columns)
        migration.downgrade()
        migration.downgrade()
        assert not inspect(connection).has_table("provider_secrets")
    engine.dispose()
