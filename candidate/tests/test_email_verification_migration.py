"""Migration 0009: unverified-by-default state, minimal token table and grants, clean downgrade."""
import io
from datetime import timedelta
import re
import uuid

from alembic import command
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
import pytest

from account_test_db import make_sqlite_engine
from verification_test_support import NOW
from kalillac_db.models import AccountEmailVerificationToken, User
from test_account_migration import _config, _render_postgresql_upgrade

PREVIOUS = "0008_account_recovery_tokens"
HEAD = "0009_account_email_verification"


def test_only_approved_fields_and_relationship():
    table = AccountEmailVerificationToken.__table__
    assert set(table.columns.keys()) == {
        "id", "user_id", "email_hash", "token_hash", "created_at", "expires_at", "consumed_at",
    }
    assert table.c.email_hash.type.length == table.c.token_hash.type.length == 64
    assert table.c.token_hash.unique and not table.c.email_hash.unique
    assert table.c.user_id.index
    assert [name for name, column in table.c.items() if column.nullable] == ["consumed_at"]
    assert {fk.target_fullname for fk in table.foreign_keys} == {"kalillac.users.id"}
    assert all(fk.ondelete == "CASCADE" for fk in table.foreign_keys)
    assert User.__table__.c.email_verified_at.nullable
    assert User.__table__.c.email_verified_at.server_default is None


def test_upgrade_keeps_accounts_unverified_and_downgrade_restores_schema():
    engine = make_sqlite_engine()
    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), PREVIOUS)
        previous_tables = set(inspect(connection).get_table_names("kalillac"))
        previous_columns = [c["name"] for c in inspect(connection).get_columns("users", schema="kalillac")]
        user_id = uuid.uuid4().hex
        connection.exec_driver_sql(
            "INSERT INTO kalillac.users (id,email,password_hash) VALUES (?, ?, ?)",
            (user_id, "migration@example.com", "existing-hash"),
        )
        before = connection.exec_driver_sql("SELECT * FROM kalillac.users").fetchall()
        command.upgrade(_config(connection=connection), HEAD)
        assert set(inspect(connection).get_table_names("kalillac")) == previous_tables | {
            "account_email_verification_tokens"}
        assert [c["name"] for c in inspect(connection).get_columns("users", schema="kalillac")] == [
            *previous_columns, "email_verified_at"]
        assert inspect(connection).get_indexes("account_email_verification_tokens", schema="kalillac") == [
            {"name": "ix_account_email_verification_tokens_user_id", "column_names": ["user_id"],
             "unique": 0, "dialect_options": {}}
        ]
        after = connection.exec_driver_sql("SELECT * FROM kalillac.users").fetchall()
        assert [tuple(row)[:-1] for row in after] == [tuple(row) for row in before]
        assert [tuple(row)[-1] for row in after] == [None]
        command.downgrade(_config(connection=connection), PREVIOUS)
        assert set(inspect(connection).get_table_names("kalillac")) == previous_tables
        assert [c["name"] for c in inspect(connection).get_columns("users", schema="kalillac")] == previous_columns
        assert connection.exec_driver_sql("SELECT * FROM kalillac.users").fetchall() == before
    engine.dispose()


def _token_row(user, **overrides):
    row = dict(id=uuid.uuid4(), user_id=user, email_hash="d" * 64, token_hash="a" * 64,
               created_at=NOW, expires_at=NOW + timedelta(hours=24))
    row.update(overrides)
    return row


@pytest.mark.parametrize("field,value", [
    ("email_hash", "a" * 63), ("email_hash", "a" * 65), ("email_hash", "g" * 64),
    ("email_hash", "A" * 64), ("email_hash", None), ("email_hash", "person@example.com"),
    ("token_hash", "a" * 63), ("token_hash", "a" * 65), ("token_hash", "g" * 64),
    ("token_hash", "A" * 64), ("token_hash", None),
    ("expires_at", NOW), ("expires_at", NOW - timedelta(seconds=1)), ("expires_at", None),
    ("user_id", uuid.UUID(int=0)), ("user_id", None),
])
def test_constraints_reject_malformed_rows(field, value):
    engine = make_sqlite_engine()
    with engine.begin() as connection:
        command.upgrade(_config(connection=connection), "head")
        user = uuid.uuid4()
        connection.exec_driver_sql(
            "INSERT INTO kalillac.users (id,email,password_hash) VALUES (?, ?, ?)",
            (user.hex, "constraint@example.com", "hash"),
        )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(AccountEmailVerificationToken.__table__.insert().values(
                **_token_row(user, **{field: value})))
        connection.execute(AccountEmailVerificationToken.__table__.insert().values(**_token_row(user)))
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
        row = _token_row(user, token_hash="b" * 64)
        connection.execute(AccountEmailVerificationToken.__table__.insert().values(**row))
        # The same email digest may appear on several tokens.
        connection.execute(AccountEmailVerificationToken.__table__.insert().values(
            **{**row, "id": uuid.uuid4(), "token_hash": "e" * 64}))
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(AccountEmailVerificationToken.__table__.insert().values(
                **{**row, "id": uuid.uuid4()}))
        connection.exec_driver_sql("DELETE FROM kalillac.users WHERE id=?", (user.hex,))
        assert connection.execute(select(AccountEmailVerificationToken.__table__)).fetchall() == []
    engine.dispose()


@pytest.mark.parametrize("role", ["kalillac_app", "kalillac_staging_app"])
def test_offline_postgresql_ddl_and_exact_grant(monkeypatch, role):
    monkeypatch.setenv("KALILLAC_MIGRATION_APP_ROLE", role)
    sql = _render_postgresql_upgrade(f"{PREVIOUS}:{HEAD}")
    for required in (
        "ALTER TABLE kalillac.users ADD COLUMN email_verified_at TIMESTAMP WITH TIME ZONE",
        "CREATE TABLE kalillac.account_email_verification_tokens",
        "id UUID NOT NULL", "email_hash VARCHAR(64) NOT NULL", "token_hash VARCHAR(64) NOT NULL",
        "length(email_hash) = 64", "length(token_hash) = 64", "expires_at > created_at",
        "CONSTRAINT uq_account_email_verification_tokens_token_hash UNIQUE (token_hash)",
        "REFERENCES kalillac.users (id) ON DELETE CASCADE",
        "CREATE INDEX ix_account_email_verification_tokens_user_id",
    ):
        assert required in sql, required
    assert "email_verified_at TIMESTAMP WITH TIME ZONE NOT NULL" not in sql
    assert "DEFAULT" not in sql.split("ADD COLUMN email_verified_at", 1)[1].split(";", 1)[0]
    assert "UPDATE kalillac.users" not in sql
    assert re.findall(r"GRANT [^;]+;", sql) == [
        f"GRANT SELECT, INSERT, UPDATE ON TABLE kalillac.account_email_verification_tokens TO {role};"
    ]
    assert "DELETE FROM" not in sql and ", DELETE" not in sql
    assert "CREATE SCHEMA" not in sql and "ALTER ROLE" not in sql


def test_offline_downgrade_removes_only_verification_schema():
    config = _config()
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline@localhost/kalillac")
    config.output_buffer = io.StringIO()
    command.downgrade(config, f"{HEAD}:{PREVIOUS}", sql=True)
    sql = config.output_buffer.getvalue()
    assert "DROP INDEX kalillac.ix_account_email_verification_tokens_user_id" in sql
    assert "DROP TABLE kalillac.account_email_verification_tokens" in sql
    assert "ALTER TABLE kalillac.users DROP COLUMN email_verified_at" in sql
    assert len(re.findall(r"^(?:DROP|ALTER) ", sql, re.MULTILINE)) == 3
    assert "DROP TABLE kalillac.users" not in sql and "kalillac.account_recovery_tokens" not in sql
