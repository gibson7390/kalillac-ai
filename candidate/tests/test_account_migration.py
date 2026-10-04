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

    assert script.get_heads() == ["0001_account_tables"]


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


def _render_postgresql_upgrade() -> str:
    config = _config()
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://offline@localhost/kalillac",
    )
    buffer = io.StringIO()
    config.output_buffer = buffer

    command.upgrade(config, "head", sql=True)
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

    # Exactly the two table DML grants: nothing on alembic_version, no
    # schema CREATE, no role membership.
    assert grants == [
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kalillac.users "
        "TO kalillac_app;",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "
        "kalillac.account_sessions TO kalillac_app;",
    ]
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
