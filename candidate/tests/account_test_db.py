"""In-memory test database for account tests.

There is no local PostgreSQL in the test environment, so SQLite stands in:
the private "kalillac" schema is an attached in-memory database, foreign
keys are enforced, and SQLAlchemy's pysqlite SAVEPOINT recipe is applied so
nested transactions behave as they do on PostgreSQL.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def make_sqlite_engine() -> Engine:
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):
        # Let SQLAlchemy, not pysqlite, emit BEGIN (needed for SAVEPOINT).
        dbapi_connection.isolation_level = None
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS kalillac")
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine, "begin")
    def _on_begin(connection):
        connection.exec_driver_sql("BEGIN")

    return engine


def make_sqlite_file_engine(directory: str) -> Engine:
    """File-backed SQLite with a real connection pool, so concurrent
    threads use separate connections (in-memory StaticPool shares one)."""

    main_path = os.path.join(directory, "main.db")
    schema_path = os.path.join(directory, "kalillac.db")

    engine = create_engine(
        f"sqlite:///{main_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None
        dbapi_connection.execute(
            "ATTACH DATABASE ? AS kalillac",
            (schema_path,),
        )
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine, "begin")
    def _on_begin(connection):
        # IMMEDIATE takes the write lock up front, as PostgreSQL's row lock
        # would serialize conflicting upserts.
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
