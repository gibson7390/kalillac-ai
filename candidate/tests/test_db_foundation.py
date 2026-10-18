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
    # Account identity, entitlement tier, aggregate daily usage, and Stripe
    # billing linkage are approved; saved chats and memory are not.
    assert set(Base.metadata.tables) == {
        "kalillac.users",
        "kalillac.account_sessions",
        "kalillac.account_entitlements",
        "kalillac.account_usage_daily",
        "kalillac.account_usage_slots",
        "kalillac.provider_usage_pools",
        "kalillac.anonymous_usage_days",
        "kalillac.anonymous_usage_slots",
        "kalillac.account_billing",
        "kalillac.stripe_webhook_events",
    }


ANONYMOUS_COLUMNS = {
    "kalillac.anonymous_usage_days": {"visitor_digest", "usage_date"},
    "kalillac.anonymous_usage_slots": {
        "visitor_digest", "usage_date", "slot", "expires_at", "state", "completed_date",
    },
}
# Fragments of any identity or content column the anonymous allowance must
# never store: raw tokens, IP addresses or IP-derived values, session ids,
# user agents, accounts, emails, cookies, messages, replies, prompts,
# history, searches or other free text.
PROHIBITED_FRAGMENTS = (
    "token", "ip", "addr", "host", "session", "agent", "user", "account", "email",
    "cookie", "fingerprint", "message", "reply", "prompt", "content", "history",
    "query", "search", "text", "name", "request", "conversation", "device",
)


def test_anonymous_tables_hold_only_digest_date_slot_state_and_timestamps():
    for table_name, expected in ANONYMOUS_COLUMNS.items():
        table = Base.metadata.tables[table_name]
        columns = set(table.columns.keys())
        assert columns == expected, table_name
        for column in columns:
            assert not any(fragment in column for fragment in PROHIBITED_FRAGMENTS), column
        # No column references an account, session or any table but the
        # anonymous visitor-day row itself.
        for key in table.foreign_keys:
            assert key.column.table.name == "anonymous_usage_days", key


def test_anonymous_digest_is_exactly_64_characters_and_bounded():
    from sqlalchemy import Date, DateTime, String
    days = Base.metadata.tables["kalillac.anonymous_usage_days"]
    slots = Base.metadata.tables["kalillac.anonymous_usage_slots"]

    for table in (days, slots):
        digest = table.c.visitor_digest
        assert isinstance(digest.type, String) and digest.type.length == 64
        assert digest.primary_key and not digest.nullable
        assert isinstance(table.c.usage_date.type, Date)
    assert any("length(visitor_digest) = 64" in str(c.sqltext) for c in days.constraints
               if hasattr(c, "sqltext"))
    assert isinstance(slots.c.expires_at.type, DateTime) and slots.c.expires_at.type.timezone
    assert isinstance(slots.c.completed_date.type, Date)
    # A finished lease is completed on its own usage_date only, never another
    # day's; open leases carry no completion date.
    checks = {c.name: str(c.sqltext) for c in slots.constraints if hasattr(c, "sqltext")}
    assert checks["ck_anonymous_usage_slots_completion_date_valid"] == (
        "(state IN ('pending', 'released') AND completed_date IS NULL) OR "
        "(state IN ('ready', 'uncertain', 'completed') AND completed_date IS NOT NULL "
        "AND completed_date = usage_date)"
    )
    assert [c.name for c in slots.primary_key.columns] == ["visitor_digest", "usage_date", "slot"]
    assert [c.name for c in days.primary_key.columns] == ["visitor_digest", "usage_date"]
    assert {index.name for index in days.indexes} == {"ix_anonymous_usage_days_usage_date"}


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
