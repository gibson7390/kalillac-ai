import os

os.environ.pop("KALILLAC_DB_ENABLED", None)
os.environ.pop("KALILLAC_DB_PASSWORD", None)
os.environ.pop("KALILLAC_DATABASE_URL", None)

import pytest

from kalillac_db.config import (
    DatabaseConfigError,
    load_database_config,
    load_database_url,
)
from kalillac_db.engine import get_engine
import kalillac_db.engine as engine_module
from kalillac_db.models import Base


def test_database_disabled_by_default():
    assert load_database_config() is None


def test_import_does_not_create_engine():
    assert engine_module._ENGINE is None


def test_disabled_get_engine_returns_none():
    assert get_engine() is None
    assert engine_module._ENGINE is None


def test_metadata_uses_private_schema():
    assert Base.metadata.schema == "kalillac"


def test_only_approved_account_tables_exist():
    # Account identity is approved; saved chats, memory, plans, and
    # entitlements are not.
    assert set(Base.metadata.tables) == {
        "kalillac.users",
        "kalillac.account_sessions",
    }


def test_database_url_ignored_while_disabled(monkeypatch):
    monkeypatch.setenv(
        "KALILLAC_DATABASE_URL",
        "postgresql+psycopg://user:pw@db.example/kalillac",
    )

    assert load_database_url() is None
    assert get_engine() is None


def test_database_url_used_when_enabled(monkeypatch):
    url = "postgresql+psycopg://user:pw@db.example/kalillac"
    monkeypatch.setenv("KALILLAC_DB_ENABLED", "true")
    monkeypatch.setenv("KALILLAC_DATABASE_URL", url)

    assert load_database_url() == url
    # The URL replaces component settings; no password file is needed.
    assert load_database_config() is None


def test_database_url_requires_psycopg_driver(monkeypatch):
    monkeypatch.setenv("KALILLAC_DB_ENABLED", "true")
    monkeypatch.setenv("KALILLAC_DATABASE_URL", "sqlite:///kalillac.db")

    with pytest.raises(DatabaseConfigError):
        load_database_url()
