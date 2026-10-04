"""Alembic environment for Kalillac.

All Kalillac tables, and Alembic's own version table, live in the private
"kalillac" schema. The schema itself is provisioned outside migrations.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import URL, create_engine, pool

from kalillac_db import migration_roles
from kalillac_db.config import (
    DatabaseConfigError,
    load_database_config,
    load_database_url,
)
from kalillac_db.models import Base


SCHEMA = "kalillac"

config = context.config
target_metadata = Base.metadata


def _database_url() -> str | URL:
    # An explicit URL (tests, offline SQL rendering) wins over the
    # application environment.
    explicit = config.get_main_option("sqlalchemy.url")

    if explicit:
        return explicit

    url = load_database_url()

    if url:
        return url

    db = load_database_config()

    if db is None:
        raise DatabaseConfigError(
            "Set KALILLAC_DB_ENABLED and database settings before migrating."
        )

    return URL.create(
        drivername="postgresql+psycopg",
        username=db.user,
        password=db.password,
        host=db.host,
        port=db.port,
        database=db.database,
    )


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _migrate(connection) -> None:
    _configure(connection=connection)

    with context.begin_transaction():
        # Inside the migration transaction: on PostgreSQL the login role
        # assumes kalillac_owner so migrated objects are owner-owned. A
        # failed SET ROLE aborts the migration.
        migration_roles.assume_migration_owner(connection)
        context.run_migrations()


def run_migrations_online() -> None:
    # Tests pass an existing connection.
    connection = config.attributes.get("connection")

    if connection is not None:
        _migrate(connection)
        return

    engine = create_engine(
        _database_url(),
        poolclass=pool.NullPool,
        hide_parameters=True,
    )

    with engine.connect() as connection:
        _migrate(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
