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

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect

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


def test_postgresql_ddl_renders_offline():
    config = _config()
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://offline@localhost/kalillac",
    )
    buffer = io.StringIO()
    config.output_buffer = buffer

    command.upgrade(config, "head", sql=True)
    sql = buffer.getvalue()

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
