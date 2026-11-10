"""Migration 0010: exactly three Spaces tables (no memory table), database-enforced ownership,
constraints, cascades and clean downgrade. Runs on an isolated in-memory
SQLite database and on offline-rendered PostgreSQL DDL; no shared, staging
or production database is touched."""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from account_test_db import make_sqlite_engine
from test_account_migration import _config, _render_postgresql_upgrade

PREVIOUS = "0009_account_email_verification"
HEAD = "0010_spaces"
SPACES_TABLES = {"spaces", "conversations", "messages"}


@pytest.fixture
def connection():
    engine = make_sqlite_engine()
    with engine.begin() as conn:
        command.upgrade(_config(connection=conn), HEAD)
        yield conn
    engine.dispose()


def _id():
    return uuid.uuid4().hex


def _user(conn, email=None):
    user_id = _id()
    conn.exec_driver_sql("INSERT INTO kalillac.users (id, email, password_hash) VALUES (?, ?, ?)",
                         (user_id, email or f"{user_id}@example.test", "hash"))
    return user_id


def _space(conn, user_id, name="Space", instructions=""):
    space_id = _id()
    conn.exec_driver_sql("INSERT INTO kalillac.spaces (id, user_id, name, instructions) VALUES (?, ?, ?, ?)",
                         (space_id, user_id, name, instructions))
    return space_id


def _conversation(conn, space_id, user_id, **values):
    conversation_id = _id()
    columns = {"id": conversation_id, "space_id": space_id, "user_id": user_id, "title": "T", **values}
    conn.exec_driver_sql(
        f"INSERT INTO kalillac.conversations ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        tuple(columns.values()))
    return conversation_id


def _message(conn, conversation_id, user_id, seq, role="user", content="hi", request_id="auto"):
    message_id = _id()
    if request_id == "auto":
        request_id = _id() if role == "user" else None
    conn.exec_driver_sql(
        "INSERT INTO kalillac.messages (id, conversation_id, user_id, seq, role, content, client_request_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (message_id, conversation_id, user_id, seq, role, content, request_id))
    return message_id


def _rejected(conn, statement):
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            statement()


def _count(conn, table):
    return conn.exec_driver_sql(f"SELECT count(*) FROM kalillac.{table}").scalar()


def test_upgrade_adds_exactly_the_spaces_tables_and_downgrade_removes_them():
    engine = make_sqlite_engine()
    with engine.begin() as conn:
        command.upgrade(_config(connection=conn), PREVIOUS)
        before = set(inspect(conn).get_table_names("kalillac"))
        command.upgrade(_config(connection=conn), HEAD)
        after = set(inspect(conn).get_table_names("kalillac"))
        assert after - before == SPACES_TABLES
        columns = {t: [c["name"] for c in inspect(conn).get_columns(t, schema="kalillac")] for t in SPACES_TABLES}
        assert columns["spaces"] == ["id", "user_id", "name", "instructions", "version", "created_at",
                                     "updated_at", "instructions_updated_at"]
        assert columns["conversations"] == ["id", "space_id", "user_id", "title", "message_count", "last_message_at",
                                            "active_request_id", "active_until", "version", "created_at", "updated_at"]
        assert columns["messages"] == ["id", "conversation_id", "user_id", "seq", "role", "content",
                                       "client_request_id", "created_at"]
        # No soft-delete machinery anywhere.
        assert not any("deleted" in name for names in columns.values() for name in names)
        command.downgrade(_config(connection=conn), PREVIOUS)
        assert set(inspect(conn).get_table_names("kalillac")) == before
    engine.dispose()


def test_children_must_share_their_parents_owner(connection):
    owner, intruder = _user(connection), _user(connection)
    space = _space(connection, owner)
    _rejected(connection, lambda: _conversation(connection, space, intruder))
    conversation = _conversation(connection, space, owner)
    _rejected(connection, lambda: _message(connection, conversation, intruder, 1))
    # A parent that does not exist at all is rejected too.
    _rejected(connection, lambda: _conversation(connection, _id(), owner))
    assert _count(connection, "conversations") == 1 and _count(connection, "messages") == 0


def test_constraints(connection):
    owner = _user(connection)
    _rejected(connection, lambda: _space(connection, owner, name="   "))
    _rejected(connection, lambda: _space(connection, owner, name="x" * 81))
    _rejected(connection, lambda: _space(connection, owner, instructions="x" * 8001))
    space = _space(connection, owner, instructions="x" * 8000)
    _rejected(connection, lambda: _conversation(connection, space, owner, message_count=401))
    _rejected(connection, lambda: _conversation(connection, space, owner, active_request_id=_id()))   # lease half-set
    _rejected(connection, lambda: _conversation(connection, space, owner, title=""))
    conversation = _conversation(connection, space, owner)
    _rejected(connection, lambda: _message(connection, conversation, owner, 1, role="system"))
    _rejected(connection, lambda: _message(connection, conversation, owner, 1, content="x" * 4001))
    _rejected(connection, lambda: _message(connection, conversation, owner, 1, role="assistant", content="x" * 24001))
    _rejected(connection, lambda: _message(connection, conversation, owner, 1, request_id=None))       # user needs a key
    _rejected(connection, lambda: _message(connection, conversation, owner, 1, role="assistant", request_id=_id()))
    _rejected(connection, lambda: _message(connection, conversation, owner, 0))
    key = _id()
    _message(connection, conversation, owner, 1, request_id=key)
    _message(connection, conversation, owner, 2, role="assistant", content="x" * 24000)
    _rejected(connection, lambda: _message(connection, conversation, owner, 2))                         # seq unique
    _rejected(connection, lambda: _message(connection, conversation, owner, 3, request_id=key))         # key unique


def test_cascades(connection):
    owner, other = _user(connection), _user(connection)
    space, kept = _space(connection, owner), _space(connection, other)
    conversation = _conversation(connection, space, owner)
    _message(connection, conversation, owner, 1)
    _message(connection, conversation, owner, 2, role="assistant")
    _conversation(connection, kept, other)

    # Deleting a conversation removes its messages.
    connection.exec_driver_sql("DELETE FROM kalillac.conversations WHERE id = ?", (conversation,))
    assert _count(connection, "messages") == 0

    # Deleting a Space removes its conversations and their messages.
    conversation = _conversation(connection, space, owner)
    _message(connection, conversation, owner, 1)
    connection.exec_driver_sql("DELETE FROM kalillac.spaces WHERE id = ?", (space,))
    assert _count(connection, "messages") == 0
    assert connection.exec_driver_sql(
        "SELECT count(*) FROM kalillac.conversations WHERE space_id = ?", (space,)).scalar() == 0

    # Deleting an account removes everything it owns, and nothing else.
    space = _space(connection, owner)
    conversation = _conversation(connection, space, owner)
    _message(connection, conversation, owner, 1)
    connection.exec_driver_sql("DELETE FROM kalillac.users WHERE id = ?", (owner,))
    owned = connection.exec_driver_sql(
        "SELECT count(*) FROM kalillac.spaces WHERE user_id = ?", (owner,)).scalar()
    assert owned == 0 and _count(connection, "messages") == 0
    assert _count(connection, "spaces") == 1 and _count(connection, "conversations") == 1


def test_postgresql_ddl_enforces_ownership_and_grants_dml_only():
    sql = _render_postgresql_upgrade(f"{PREVIOUS}:{HEAD}")
    for expected in (
        "CONSTRAINT uq_spaces_id_user_id UNIQUE (id, user_id)",
        "CONSTRAINT uq_conversations_id_user_id UNIQUE (id, user_id)",
        "CONSTRAINT fk_spaces_user_id_users FOREIGN KEY(user_id) REFERENCES kalillac.users (id) ON DELETE CASCADE",
        "CONSTRAINT fk_conversations_space_id_user_id_spaces FOREIGN KEY(space_id, user_id) "
        "REFERENCES kalillac.spaces (id, user_id) ON DELETE CASCADE",
        "CONSTRAINT fk_messages_conversation_id_user_id_conversations FOREIGN KEY(conversation_id, user_id) "
        "REFERENCES kalillac.conversations (id, user_id) ON DELETE CASCADE",
        "CONSTRAINT uq_messages_conversation_id_seq UNIQUE (conversation_id, seq)",
        "CONSTRAINT uq_messages_conversation_id_client_request_id UNIQUE (conversation_id, client_request_id)",
        "CONSTRAINT ck_messages_role_allowed CHECK (role IN ('user', 'assistant'))",
        "active_until TIMESTAMP WITH TIME ZONE",
    ):
        assert expected in sql, expected
    grants = [line.strip() for line in sql.splitlines() if line.strip().upper().startswith(("GRANT", "REVOKE"))]
    assert grants == [f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kalillac.{t} TO kalillac_app;"
                      for t in ("spaces", "conversations", "messages")]
    for forbidden in ("ALTER DEFAULT PRIVILEGES", "OWNER TO", "ON SCHEMA", "deleted_at", "space_memories"):
        assert forbidden not in sql, forbidden
