"""The first Alembic migration matches the account models.

No PostgreSQL server exists in the test environment, so:
- the migration is applied and reverted on SQLite (with the "kalillac"
  schema attached) and compared with the models via Alembic autogenerate;
- the PostgreSQL DDL is rendered offline to check dialect-specific output.
A real `alembic upgrade head` on staging PostgreSQL is still required.
"""

from __future__ import annotations

import io
from pathlib import Path
import re

import pytest

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from kalillac_db import migration_roles
from kalillac_db.models import Base

from account_test_db import make_sqlite_engine


CANDIDATE_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _default_role_environment(monkeypatch):
    # Every test starts from the production defaults unless it overrides.
    monkeypatch.delenv(migration_roles.OWNER_ROLE_ENV, raising=False)
    monkeypatch.delenv(migration_roles.APP_ROLE_ENV, raising=False)


def _config(**attributes) -> Config:
    config = Config(str(CANDIDATE_DIR / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(CANDIDATE_DIR / "migrations"),
    )
    config.attributes.update(attributes)
    return config


def test_single_head_revision():
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_config())

    assert script.get_heads() == ["0004_account_billing"]
    assert script.get_revision("0004_account_billing").down_revision == (
        "0003_account_usage_daily"
    )
    assert script.get_revision("0003_account_usage_daily").down_revision == (
        "0002_account_entitlements"
    )
    assert script.get_revision("0002_account_entitlements").down_revision == (
        "0001_account_tables"
    )


def test_upgrade_matches_models_and_downgrade_removes_tables():
    engine = make_sqlite_engine()

    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "head")

        tables = set(inspect(connection).get_table_names(schema="kalillac"))
        assert {"users", "account_sessions", "alembic_version"} <= tables

        diff = compare_metadata(
            MigrationContext.configure(
                connection,
                opts={
                    "include_schemas": True,
                    "compare_type": True,
                    "version_table_schema": "kalillac",
                },
            ),
            Base.metadata,
        )
        assert diff == []

        command.downgrade(_config(connection=connection), "base")

        tables = set(inspect(connection).get_table_names(schema="kalillac"))
        assert "users" not in tables
        assert "account_sessions" not in tables

    engine.dispose()


ACCOUNT_TABLES = (
    "users",
    "account_sessions",
    "account_entitlements",
    "account_usage_daily",
    "account_billing",
    "stripe_webhook_events",
)


def _expected_grants(role: str) -> list[str]:
    """Exactly one DML grant per account table, in migration order."""
    return [
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kalillac.{table} "
        f"TO {role};"
        for table in ACCOUNT_TABLES
    ]


def _render_postgresql_upgrade(revision_range: str = "head") -> str:
    config = _config()
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://offline@localhost/kalillac",
    )
    buffer = io.StringIO()
    config.output_buffer = buffer

    command.upgrade(config, revision_range, sql=True)
    return buffer.getvalue()


def test_postgresql_ddl_renders_offline():
    sql = _render_postgresql_upgrade()

    assert "CREATE TABLE kalillac.alembic_version" in sql
    assert "CREATE TABLE kalillac.users" in sql
    assert "CREATE TABLE kalillac.account_sessions" in sql
    assert "id UUID NOT NULL" in sql
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in sql
    assert "is_active BOOLEAN DEFAULT true NOT NULL" in sql
    assert "CONSTRAINT uq_users_email UNIQUE (email)" in sql
    assert (
        "CONSTRAINT fk_account_sessions_user_id_users FOREIGN KEY(user_id) "
        "REFERENCES kalillac.users (id) ON DELETE CASCADE"
    ) in sql
    assert "CREATE INDEX ix_account_sessions_user_id" in sql

    # The schema is provisioned outside migrations.
    assert "CREATE SCHEMA" not in sql


# --- PostgreSQL role ownership and grants ------------------------------------


class _FakeDialect:
    def __init__(self, name):
        self.name = name


class _FakeConnection:
    def __init__(self, dialect_name, fail=False):
        self.dialect = _FakeDialect(dialect_name)
        self.statements = []
        self._fail = fail

    def exec_driver_sql(self, statement):
        self.statements.append(statement)

        if self._fail:
            raise RuntimeError("permission denied to set role")


def test_postgresql_migration_assumes_owner_role():
    connection = _FakeConnection("postgresql")

    migration_roles.assume_migration_owner(connection)

    assert connection.statements == ["SET ROLE kalillac_owner"]


def test_failed_set_role_aborts_instead_of_falling_back():
    connection = _FakeConnection("postgresql", fail=True)

    with pytest.raises(RuntimeError, match="permission denied"):
        migration_roles.assume_migration_owner(connection)


def test_sqlite_migration_does_not_set_role():
    connection = _FakeConnection("sqlite")

    migration_roles.assume_migration_owner(connection)

    assert connection.statements == []


def test_online_migration_assumes_role_before_creating_objects(monkeypatch):
    calls = []
    original = migration_roles.assume_migration_owner

    def recording(connection):
        # Nothing has been created yet when the role is assumed.
        calls.append(
            set(inspect(connection).get_table_names(schema="kalillac"))
        )
        original(connection)

    monkeypatch.setattr(migration_roles, "assume_migration_owner", recording)
    engine = make_sqlite_engine()

    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "head")
        tables = set(inspect(connection).get_table_names(schema="kalillac"))

    engine.dispose()

    assert calls == [set()]
    assert {"users", "account_sessions", "alembic_version"} <= tables


def test_failed_set_role_aborts_online_migration(monkeypatch):
    def refuse(connection):
        raise RuntimeError("permission denied to set role kalillac_owner")

    monkeypatch.setattr(migration_roles, "assume_migration_owner", refuse)
    engine = make_sqlite_engine()

    with pytest.raises(RuntimeError, match="permission denied"):
        with engine.begin() as connection:
            command.upgrade(_config(connection=connection), "head")

    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names(schema="kalillac"))

    engine.dispose()

    assert "users" not in tables
    assert "account_sessions" not in tables


def test_postgresql_ddl_grants_dml_on_users():
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kalillac.users "
        "TO kalillac_app;"
    ) in _render_postgresql_upgrade()


def test_postgresql_ddl_grants_dml_on_account_sessions():
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        "kalillac.account_sessions TO kalillac_app;"
    ) in _render_postgresql_upgrade()


def test_postgresql_ddl_grants_nothing_else():
    sql = _render_postgresql_upgrade()
    grants = [
        line.strip()
        for line in sql.splitlines()
        if re.match(r"\s*(GRANT|REVOKE)\b", line, re.IGNORECASE)
    ]

    # Exactly the per-table DML grants: nothing on alembic_version, no
    # schema CREATE, no role membership.
    assert grants == _expected_grants("kalillac_app")
    assert not any("alembic_version" in grant for grant in grants)
    assert not re.search(r"\bON\s+SCHEMA\b", sql, re.IGNORECASE)
    assert not re.search(r"GRANT\s+CREATE", sql, re.IGNORECASE)

    for forbidden in (
        "ALTER DEFAULT PRIVILEGES",
        "OWNER TO",
        "SUPERUSER",
        "CREATE ROLE",
        "ALTER ROLE",
    ):
        assert forbidden not in sql.upper(), forbidden


def test_sqlite_migration_issues_no_grants():
    engine = make_sqlite_engine()
    statements = []

    from sqlalchemy import event

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "head")

    engine.dispose()

    assert statements
    assert not any("GRANT" in statement.upper() for statement in statements)


# --- environment-specific migration roles ------------------------------------


STAGING_OWNER = "kalillac_staging_owner"
STAGING_APP = "kalillac_staging_app"

INVALID_ROLE_NAMES = [
    "kalillac_app; DROP TABLE kalillac.users",
    "kalillac_app TO PUBLIC",
    'kalillac"app',
    "Kalillac_App",
    "1kalillac",
    "kalillac-app",
    "kalillac.app",
    "a" * 64,
]


def _grant_lines(sql):
    return [
        line.strip()
        for line in sql.splitlines()
        if re.match(r"\s*GRANT\b", line, re.IGNORECASE)
    ]


def test_defaults_set_role_kalillac_owner():
    connection = _FakeConnection("postgresql")

    migration_roles.assume_migration_owner(connection)

    assert migration_roles.migration_owner_role() == "kalillac_owner"
    assert connection.statements == ["SET ROLE kalillac_owner"]


def test_defaults_grant_dml_to_kalillac_app():
    assert _grant_lines(_render_postgresql_upgrade()) == _expected_grants(
        "kalillac_app"
    )


def test_blank_settings_mean_defaults(monkeypatch):
    monkeypatch.setenv(migration_roles.OWNER_ROLE_ENV, "  ")
    monkeypatch.setenv(migration_roles.APP_ROLE_ENV, "")

    assert migration_roles.migration_owner_role() == "kalillac_owner"
    assert migration_roles.migration_app_role() == "kalillac_app"


def test_staging_override_sets_role_staging_owner(monkeypatch):
    monkeypatch.setenv(migration_roles.OWNER_ROLE_ENV, STAGING_OWNER)
    connection = _FakeConnection("postgresql")

    migration_roles.assume_migration_owner(connection)

    assert connection.statements == [f"SET ROLE {STAGING_OWNER}"]


def test_staging_override_grants_dml_to_staging_app(monkeypatch):
    monkeypatch.setenv(migration_roles.APP_ROLE_ENV, STAGING_APP)

    sql = _render_postgresql_upgrade()

    assert _grant_lines(sql) == _expected_grants(STAGING_APP)
    assert "kalillac_app;" not in sql
    # The schema name does not change per environment.
    assert "CREATE TABLE kalillac.users" in sql
    assert "CREATE TABLE kalillac.alembic_version" in sql


@pytest.mark.parametrize("invalid", INVALID_ROLE_NAMES)
def test_invalid_owner_role_is_rejected(monkeypatch, invalid):
    monkeypatch.setenv(migration_roles.OWNER_ROLE_ENV, invalid)
    connection = _FakeConnection("postgresql")

    with pytest.raises(migration_roles.InvalidRoleName):
        migration_roles.assume_migration_owner(connection)

    # Nothing reached the database, and there was no fallback.
    assert connection.statements == []


@pytest.mark.parametrize("invalid", INVALID_ROLE_NAMES)
def test_invalid_app_role_is_rejected(monkeypatch, invalid):
    monkeypatch.setenv(migration_roles.APP_ROLE_ENV, invalid)

    with pytest.raises(migration_roles.InvalidRoleName):
        _render_postgresql_upgrade()


def test_invalid_owner_role_aborts_online_migration_before_any_ddl(monkeypatch):
    monkeypatch.setenv(migration_roles.OWNER_ROLE_ENV, "bad role")
    attempted = []

    def postgres_like_owner_switch(connection):
        attempted.append(1)
        # Validate exactly as the PostgreSQL path does.
        migration_roles.migration_owner_role()

    monkeypatch.setattr(
        migration_roles,
        "assume_migration_owner",
        postgres_like_owner_switch,
    )
    engine = make_sqlite_engine()

    with pytest.raises(migration_roles.InvalidRoleName):
        with engine.begin() as connection:
            command.upgrade(_config(connection=connection), "head")

    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names(schema="kalillac"))

    engine.dispose()

    assert attempted == [1]
    assert "users" not in tables


def test_sqlite_unaffected_by_role_settings(monkeypatch):
    # Invalid names are only validated where roles exist (PostgreSQL), so a
    # SQLite migration neither sets a role nor grants, whatever the settings.
    monkeypatch.setenv(migration_roles.OWNER_ROLE_ENV, "not valid!")
    monkeypatch.setenv(migration_roles.APP_ROLE_ENV, "not valid!")
    engine = make_sqlite_engine()

    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "head")
        tables = set(inspect(connection).get_table_names(schema="kalillac"))

    engine.dispose()

    assert {"users", "account_sessions", "alembic_version"} <= tables


# --- 0002: account entitlements --------------------------------------------------


ENTITLEMENTS_ONLY = "0001_account_tables:0002_account_entitlements"


EARLY_USER_IDS = (
    "00000000000000000000000000000001",
    "00000000000000000000000000000002",
)


def _entitlement_rows(connection):
    from sqlalchemy import text

    return connection.execute(
        text(
            "SELECT user_id, tier, source, created_at, updated_at, expires_at "
            "FROM kalillac.account_entitlements ORDER BY user_id"
        )
    ).all()


def _assert_backfilled(connection):
    rows = _entitlement_rows(connection)

    # Exactly one row per pre-existing account, no more.
    assert [row.user_id for row in rows] == list(EARLY_USER_IDS)

    for row in rows:
        assert row.tier == "free"
        assert row.source == "migration"
        assert row.expires_at is None
        assert row.created_at is not None
        assert row.updated_at is not None


def test_entitlement_migration_backfills_existing_accounts_once():
    from sqlalchemy import text

    engine = make_sqlite_engine()

    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "0001_account_tables")
        tables = set(inspect(connection).get_table_names(schema="kalillac"))
        assert "account_entitlements" not in tables

        # Accounts that exist before entitlements (as on staging), one of
        # them signed in.
        for index, user_id in enumerate(EARLY_USER_IDS):
            connection.execute(
                text(
                    "INSERT INTO kalillac.users (id, email, password_hash) "
                    "VALUES (:id, :email, 'hash')"
                ),
                {"id": user_id, "email": f"early{index}@example.com"},
            )

        connection.execute(
            text(
                "INSERT INTO kalillac.account_sessions "
                "(id, user_id, token_hash, expires_at) "
                "VALUES ('00000000000000000000000000000099', :user_id, "
                "'digest', '2099-01-01 00:00:00')"
            ),
            {"user_id": EARLY_USER_IDS[0]},
        )

        command.upgrade(_config(connection=connection), "head")
        _assert_backfilled(connection)

        # Upgrading again (already at head) adds nothing.
        command.upgrade(_config(connection=connection), "head")
        _assert_backfilled(connection)

        # Downgrade removes only the entitlement table; accounts and
        # sign-in sessions are preserved.
        command.downgrade(_config(connection=connection), "0001_account_tables")
        tables = set(inspect(connection).get_table_names(schema="kalillac"))
        assert "account_entitlements" not in tables
        assert {"users", "account_sessions"} <= tables
        assert connection.execute(
            text("SELECT COUNT(*) FROM kalillac.users")
        ).scalar() == len(EARLY_USER_IDS)
        assert connection.execute(
            text("SELECT COUNT(*) FROM kalillac.account_sessions")
        ).scalar() == 1

        # Re-upgrading backfills again, still exactly one row per account.
        command.upgrade(_config(connection=connection), "head")
        _assert_backfilled(connection)

    engine.dispose()


def test_entitlement_backfill_with_no_accounts_inserts_nothing():
    engine = make_sqlite_engine()

    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "head")
        assert _entitlement_rows(connection) == []

    engine.dispose()


def test_entitlement_postgresql_ddl():
    sql = _render_postgresql_upgrade(ENTITLEMENTS_ONLY)

    assert "CREATE TABLE kalillac.account_entitlements" in sql
    assert "user_id UUID NOT NULL" in sql
    assert "tier VARCHAR(16) DEFAULT 'free' NOT NULL" in sql
    assert "source VARCHAR(32) DEFAULT 'registration' NOT NULL" in sql
    assert "expires_at TIMESTAMP WITH TIME ZONE" in sql
    assert (
        "CONSTRAINT ck_account_entitlements_tier_valid "
        "CHECK (tier IN ('free', 'paid'))"
    ) in sql
    assert (
        "CONSTRAINT fk_account_entitlements_user_id_users FOREIGN KEY(user_id) "
        "REFERENCES kalillac.users (id) ON DELETE CASCADE"
    ) in sql
    assert "CONSTRAINT pk_account_entitlements PRIMARY KEY (user_id)" in sql

    # 0002 creates only the entitlement table: no other tables, no schema.
    assert sql.count("CREATE TABLE kalillac.") == 1
    assert "CREATE SCHEMA" not in sql

    # The backfill: one free 'migration' row per existing account, guarded
    # against duplicates.
    assert (
        "INSERT INTO kalillac.account_entitlements "
        "(user_id, tier, source, created_at, updated_at, expires_at) "
        "SELECT u.id, 'free', 'migration', CURRENT_TIMESTAMP, "
        "CURRENT_TIMESTAMP, NULL FROM kalillac.users AS u "
        "WHERE NOT EXISTS (SELECT 1 FROM kalillac.account_entitlements AS e "
        "WHERE e.user_id = u.id);"
    ) in sql
    assert sql.index("CREATE TABLE kalillac.account_entitlements") < sql.index(
        "INSERT INTO kalillac.account_entitlements"
    )
    assert sql.index("INSERT INTO kalillac.account_entitlements") < sql.index(
        "GRANT SELECT"
    )
    assert "UPDATE kalillac.alembic_version" in sql
    assert "'0002_account_entitlements'" in sql


@pytest.mark.parametrize(
    "app_role_setting, expected_role",
    [(None, "kalillac_app"), (STAGING_APP, STAGING_APP)],
)
def test_entitlement_migration_grants_only_dml_on_its_table(
    monkeypatch,
    app_role_setting,
    expected_role,
):
    if app_role_setting:
        monkeypatch.setenv(migration_roles.APP_ROLE_ENV, app_role_setting)

    sql = _render_postgresql_upgrade(ENTITLEMENTS_ONLY)

    assert _grant_lines(sql) == [
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        f"kalillac.account_entitlements TO {expected_role};"
    ]
    assert not re.search(r"\bON\s+SCHEMA\b", sql, re.IGNORECASE)
    assert not re.search(r"GRANT\s+CREATE", sql, re.IGNORECASE)

    for forbidden in (
        "ALTER DEFAULT PRIVILEGES",
        "OWNER TO",
        "SUPERUSER",
        "CREATE ROLE",
        "ALTER ROLE",
        "REVOKE",
    ):
        assert forbidden not in sql.upper(), forbidden


@pytest.mark.parametrize("app_role_setting", [None, STAGING_APP])
def test_no_migration_grants_anything_on_alembic_version(
    monkeypatch,
    app_role_setting,
):
    if app_role_setting:
        monkeypatch.setenv(migration_roles.APP_ROLE_ENV, app_role_setting)

    sql = _render_postgresql_upgrade()
    statements = [statement.strip() for statement in sql.split(";")]

    assert not any(
        "alembic_version" in statement
        and re.match(r"(GRANT|REVOKE|ALTER)\b", statement, re.IGNORECASE)
        for statement in statements
    )


def test_entitlement_migration_invalid_app_role_is_rejected(monkeypatch):
    monkeypatch.setenv(migration_roles.APP_ROLE_ENV, "bad role")

    with pytest.raises(migration_roles.InvalidRoleName):
        _render_postgresql_upgrade(ENTITLEMENTS_ONLY)


def test_entitlement_migration_staging_owner_role(monkeypatch):
    # Owner assumption is shared by every online migration, 0002 included.
    monkeypatch.setenv(migration_roles.OWNER_ROLE_ENV, STAGING_OWNER)
    connection = _FakeConnection("postgresql")

    migration_roles.assume_migration_owner(connection)

    assert connection.statements == [f"SET ROLE {STAGING_OWNER}"]


# --- 0003: aggregate daily usage ---------------------------------------------------


USAGE_ONLY = "0002_account_entitlements:0003_account_usage_daily"


def test_usage_migration_steps_up_and_down_preserving_accounts():
    from sqlalchemy import text

    engine = make_sqlite_engine()

    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "0002_account_entitlements")
        connection.execute(
            text(
                "INSERT INTO kalillac.users (id, email, password_hash) "
                "VALUES ('00000000000000000000000000000003', "
                "'u@example.com', 'hash')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO kalillac.account_entitlements (user_id, tier) "
                "VALUES ('00000000000000000000000000000003', 'paid')"
            )
        )

        command.upgrade(_config(connection=connection), "head")
        tables = set(inspect(connection).get_table_names(schema="kalillac"))
        assert "account_usage_daily" in tables

        # No backfill: usage starts empty.
        assert connection.execute(
            text("SELECT COUNT(*) FROM kalillac.account_usage_daily")
        ).scalar() == 0

        command.downgrade(
            _config(connection=connection),
            "0002_account_entitlements",
        )
        tables = set(inspect(connection).get_table_names(schema="kalillac"))
        assert "account_usage_daily" not in tables
        assert {"users", "account_sessions", "account_entitlements"} <= tables

        # Accounts and entitlements survive the downgrade unchanged.
        assert connection.execute(
            text("SELECT tier FROM kalillac.account_entitlements")
        ).scalar() == "paid"

    engine.dispose()


def test_usage_postgresql_ddl():
    sql = _render_postgresql_upgrade(USAGE_ONLY)

    assert "CREATE TABLE kalillac.account_usage_daily" in sql
    assert "user_id UUID NOT NULL" in sql
    assert "usage_date DATE NOT NULL" in sql
    for counter in ("successful_chats", "request_chars", "response_chars"):
        assert f"{counter} BIGINT DEFAULT 0 NOT NULL" in sql
        assert (
            f"CONSTRAINT ck_account_usage_daily_{counter}_non_negative "
            f"CHECK ({counter} >= 0)"
        ) in sql
    assert (
        "CONSTRAINT pk_account_usage_daily PRIMARY KEY (user_id, usage_date)"
    ) in sql
    assert (
        "CONSTRAINT fk_account_usage_daily_user_id_users FOREIGN KEY(user_id) "
        "REFERENCES kalillac.users (id) ON DELETE CASCADE"
    ) in sql

    # Only the usage table; no backfill, schema, or content columns.
    assert sql.count("CREATE TABLE kalillac.") == 1
    assert "INSERT INTO kalillac.account_usage_daily" not in sql
    assert "CREATE SCHEMA" not in sql
    for forbidden in ("message", "history", "reply", "session_id", "query", "tier"):
        assert forbidden not in sql.lower(), forbidden


@pytest.mark.parametrize(
    "app_role_setting, expected_role",
    [(None, "kalillac_app"), (STAGING_APP, STAGING_APP)],
)
def test_usage_migration_grants_only_dml_on_its_table(
    monkeypatch,
    app_role_setting,
    expected_role,
):
    if app_role_setting:
        monkeypatch.setenv(migration_roles.APP_ROLE_ENV, app_role_setting)

    sql = _render_postgresql_upgrade(USAGE_ONLY)

    assert _grant_lines(sql) == [
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        f"kalillac.account_usage_daily TO {expected_role};"
    ]
    assert "alembic_version TO" not in sql
    assert not re.search(r"\bON\s+SCHEMA\b", sql, re.IGNORECASE)
    assert not re.search(r"GRANT\s+CREATE", sql, re.IGNORECASE)

    for forbidden in (
        "ALTER DEFAULT PRIVILEGES",
        "OWNER TO",
        "SUPERUSER",
        "CREATE ROLE",
        "ALTER ROLE",
        "REVOKE",
    ):
        assert forbidden not in sql.upper(), forbidden


def test_usage_migration_invalid_app_role_is_rejected(monkeypatch):
    monkeypatch.setenv(migration_roles.APP_ROLE_ENV, "bad role")

    with pytest.raises(migration_roles.InvalidRoleName):
        _render_postgresql_upgrade(USAGE_ONLY)


# --- 0004: Stripe billing linkage and webhook idempotency -------------------------


BILLING_ONLY = "0003_account_usage_daily:0004_account_billing"


def test_billing_migration_steps_up_and_down_preserving_accounts():
    from sqlalchemy import text

    engine = make_sqlite_engine()

    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "0003_account_usage_daily")
        connection.execute(
            text(
                "INSERT INTO kalillac.users (id, email, password_hash) "
                "VALUES ('00000000000000000000000000000004', "
                "'b@example.com', 'hash')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO kalillac.account_usage_daily "
                "(user_id, usage_date, successful_chats) "
                "VALUES ('00000000000000000000000000000004', '2026-10-01', 3)"
            )
        )

        command.upgrade(_config(connection=connection), "head")
        tables = set(inspect(connection).get_table_names(schema="kalillac"))
        assert {"account_billing", "stripe_webhook_events"} <= tables

        # No backfill.
        for table in ("account_billing", "stripe_webhook_events"):
            assert connection.execute(
                text(f"SELECT COUNT(*) FROM kalillac.{table}")
            ).scalar() == 0

        command.downgrade(
            _config(connection=connection),
            "0003_account_usage_daily",
        )
        tables = set(inspect(connection).get_table_names(schema="kalillac"))
        assert not {"account_billing", "stripe_webhook_events"} & tables

        # Accounts, entitlements, and usage survive the downgrade.
        assert {"users", "account_entitlements", "account_usage_daily"} <= tables
        assert connection.execute(
            text("SELECT successful_chats FROM kalillac.account_usage_daily")
        ).scalar() == 3

    engine.dispose()


def test_billing_postgresql_ddl():
    sql = _render_postgresql_upgrade(BILLING_ONLY)

    assert "CREATE TABLE kalillac.account_billing" in sql
    assert "CREATE TABLE kalillac.stripe_webhook_events" in sql
    assert "cancel_at_period_end BOOLEAN DEFAULT false NOT NULL" in sql
    assert "current_period_end TIMESTAMP WITH TIME ZONE" in sql
    assert "CONSTRAINT pk_account_billing PRIMARY KEY (user_id)" in sql
    assert (
        "CONSTRAINT fk_account_billing_user_id_users FOREIGN KEY(user_id) "
        "REFERENCES kalillac.users (id) ON DELETE CASCADE"
    ) in sql
    assert (
        "CONSTRAINT uq_account_billing_stripe_customer_id "
        "UNIQUE (stripe_customer_id)"
    ) in sql
    assert (
        "CONSTRAINT uq_account_billing_stripe_subscription_id "
        "UNIQUE (stripe_subscription_id)"
    ) in sql
    assert (
        "CONSTRAINT pk_stripe_webhook_events PRIMARY KEY (event_id)"
    ) in sql

    # Server-generated pending-Checkout state.
    assert "checkout_attempt_id UUID" in sql
    assert "checkout_customer_id VARCHAR(255)" in sql
    assert "checkout_attempt_created_at TIMESTAMP WITH TIME ZONE" in sql
    assert "checkout_price_id VARCHAR(255)" in sql
    assert "checkout_success_url VARCHAR(2048)" in sql
    assert "checkout_cancel_url VARCHAR(2048)" in sql
    assert "stripe_checkout_session_id VARCHAR(255)" in sql
    assert "checkout_session_expires_at TIMESTAMP WITH TIME ZONE" in sql
    assert (
        "CONSTRAINT uq_account_billing_stripe_checkout_session_id "
        "UNIQUE (stripe_checkout_session_id)"
    ) in sql

    # Only these two tables; no backfill, schema, payload, or payment columns.
    assert sql.count("CREATE TABLE kalillac.") == 2
    assert "INSERT INTO kalillac.account_billing" not in sql
    assert "CREATE SCHEMA" not in sql
    for forbidden in ("payload", "card", "payment", "invoice", "amount", "secret"):
        assert forbidden not in sql.lower(), forbidden


@pytest.mark.parametrize(
    "app_role_setting, expected_role",
    [(None, "kalillac_app"), (STAGING_APP, STAGING_APP)],
)
def test_billing_migration_grants_only_dml_on_its_tables(
    monkeypatch,
    app_role_setting,
    expected_role,
):
    if app_role_setting:
        monkeypatch.setenv(migration_roles.APP_ROLE_ENV, app_role_setting)

    sql = _render_postgresql_upgrade(BILLING_ONLY)

    assert _grant_lines(sql) == [
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        f"kalillac.account_billing TO {expected_role};",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        f"kalillac.stripe_webhook_events TO {expected_role};",
    ]
    assert "alembic_version TO" not in sql
    assert not re.search(r"\bON\s+SCHEMA\b", sql, re.IGNORECASE)
    assert not re.search(r"GRANT\s+CREATE", sql, re.IGNORECASE)

    for forbidden in (
        "ALTER DEFAULT PRIVILEGES",
        "OWNER TO",
        "SUPERUSER",
        "CREATE ROLE",
        "ALTER ROLE",
        "REVOKE",
    ):
        assert forbidden not in sql.upper(), forbidden


def test_billing_migration_invalid_app_role_is_rejected(monkeypatch):
    monkeypatch.setenv(migration_roles.APP_ROLE_ENV, "bad role")

    with pytest.raises(migration_roles.InvalidRoleName):
        _render_postgresql_upgrade(BILLING_ONLY)


def test_billing_migration_staging_owner_role(monkeypatch):
    monkeypatch.setenv(migration_roles.OWNER_ROLE_ENV, STAGING_OWNER)
    connection = _FakeConnection("postgresql")

    migration_roles.assume_migration_owner(connection)

    assert connection.statements == [f"SET ROLE {STAGING_OWNER}"]
