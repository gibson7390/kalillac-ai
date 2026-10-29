"""Migration 0008 preserves the prior schema and grants only required token DML."""
import io
from datetime import timedelta
import re
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
import pytest

from account_test_db import make_sqlite_engine
from recovery_test_support import NOW
from kalillac_db.models import AccountRecoveryToken, Base
from test_account_migration import _config, _render_postgresql_upgrade


def test_only_approved_recovery_fields_and_relationship():
    table = AccountRecoveryToken.__table__
    assert set(table.columns.keys()) == {
        "id", "user_id", "purpose", "token_hash", "created_at", "expires_at", "consumed_at",
    }
    assert table.c.token_hash.type.length == 64
    assert table.c.token_hash.unique
    assert table.c.user_id.index
    assert table.c.consumed_at.nullable
    assert {fk.target_fullname for fk in table.foreign_keys} == {"kalillac.users.id"}
    assert all(fk.ondelete == "CASCADE" for fk in table.foreign_keys)


def test_upgrade_downgrade_preserves_existing_rows_and_indexes():
    engine = make_sqlite_engine()
    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "0007_anonymous_usage")
        previous = set(inspect(connection).get_table_names("kalillac"))
        user_id = uuid.uuid4().hex
        connection.exec_driver_sql(
            "INSERT INTO kalillac.users (id,email,password_hash) VALUES (?, ?, ?)",
            (user_id, "migration@example.com", "existing-hash"),
        )
        before = connection.exec_driver_sql("SELECT * FROM kalillac.users").fetchall()
        command.upgrade(_config(connection=connection), "head")
        assert set(inspect(connection).get_table_names("kalillac")) == previous | {"account_recovery_tokens"}
        assert inspect(connection).get_indexes("account_recovery_tokens", schema="kalillac") == [
            {"name": "ix_account_recovery_tokens_user_id", "column_names": ["user_id"], "unique": 0, "dialect_options": {}}
        ]
        assert connection.execute(select(AccountRecoveryToken.__table__)).fetchall() == []
        assert connection.exec_driver_sql("SELECT * FROM kalillac.users").fetchall() == before
        command.downgrade(_config(connection=connection), "0007_anonymous_usage")
        assert set(inspect(connection).get_table_names("kalillac")) == previous
        assert connection.exec_driver_sql("SELECT * FROM kalillac.users").fetchall() == before
    engine.dispose()


@pytest.mark.parametrize("field,value", [
    ("purpose", "email_verification"), ("purpose", None),
    ("token_hash", "a" * 63), ("token_hash", "a" * 65), ("token_hash", "g" * 64),
    ("token_hash", "A" * 64), ("token_hash", None),
    ("expires_at", NOW), ("expires_at", NOW - timedelta(seconds=1)),
    ("user_id", uuid.UUID(int=0)),
])
def test_constraints_reject_malformed_tokens(field, value):
    engine = make_sqlite_engine()
    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "head")
        user = uuid.uuid4()
        connection.exec_driver_sql(
            "INSERT INTO kalillac.users (id,email,password_hash) VALUES (?, ?, ?)",
            (user.hex, "constraint@example.com", "hash"),
        )
        row = dict(id=uuid.uuid4(), user_id=user, purpose="password_reset", token_hash="a" * 64,
                   created_at=NOW, expires_at=NOW + timedelta(minutes=30))
        row[field] = value
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(AccountRecoveryToken.__table__.insert().values(**row))
    engine.dispose()


def test_token_hash_unique_and_user_delete_cascades():
    engine = make_sqlite_engine()
    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "head")
        user = uuid.uuid4()
        connection.exec_driver_sql(
            "INSERT INTO kalillac.users (id,email,password_hash) VALUES (?, ?, ?)",
            (user.hex, "unique@example.com", "hash"),
        )
        row = dict(id=uuid.uuid4(), user_id=user, purpose="password_reset", token_hash="b" * 64,
                   created_at=NOW, expires_at=NOW + timedelta(minutes=30))
        connection.execute(AccountRecoveryToken.__table__.insert().values(**row))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(AccountRecoveryToken.__table__.insert().values(**{**row, "id": uuid.uuid4()}))
        connection.exec_driver_sql("DELETE FROM kalillac.users WHERE id=?", (user.hex,))
        assert connection.execute(select(AccountRecoveryToken.__table__)).fetchall() == []
    engine.dispose()


@pytest.mark.parametrize("role", ["kalillac_app", "kalillac_staging_app"])
def test_offline_postgresql_ddl_constraints_index_and_minimal_grant(monkeypatch, role):
    monkeypatch.setenv("KALILLAC_MIGRATION_APP_ROLE", role)
    sql = _render_postgresql_upgrade("0007_anonymous_usage:0008_account_recovery_tokens")
    for required in (
        "CREATE TABLE kalillac.account_recovery_tokens",
        "id UUID NOT NULL", "purpose = 'password_reset'", "length(token_hash) = 64",
        "UNIQUE (token_hash)", "ON DELETE CASCADE",
        "CREATE INDEX ix_account_recovery_tokens_user_id",
    ):
        assert required in sql
    assert re.findall(r"GRANT [^;]+;", sql) == [
        f"GRANT SELECT, INSERT, UPDATE ON TABLE kalillac.account_recovery_tokens TO {role};"
    ]
    assert "CREATE SCHEMA" not in sql and "ALTER ROLE" not in sql


def test_offline_downgrade_drops_only_recovery_table():
    config = _config()
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline@localhost/kalillac")
    config.output_buffer = io.StringIO()
    command.downgrade(config, "0008_account_recovery_tokens:0007_anonymous_usage", sql=True)
    sql = config.output_buffer.getvalue()
    assert "DROP INDEX kalillac.ix_account_recovery_tokens_user_id" in sql
    assert "DROP TABLE kalillac.account_recovery_tokens" in sql
    assert "DROP TABLE kalillac.users" not in sql
