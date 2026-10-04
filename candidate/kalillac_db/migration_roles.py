"""PostgreSQL role handling for Alembic migrations.

The migration login role (kalillac_migrate) is NOINHERIT and holds explicit
membership in kalillac_owner. Before migrating it must assume that role, so
every migrated object (including alembic_version) is owned by the NOLOGIN
kalillac_owner rather than by the login role. If SET ROLE fails, the error
propagates and the migration aborts; there is no fallback.
"""

from __future__ import annotations

from sqlalchemy.engine import Connection


MIGRATION_OWNER_ROLE = "kalillac_owner"


def assume_migration_owner(connection: Connection) -> None:
    """SET ROLE to the schema owner on PostgreSQL; no-op elsewhere."""

    if connection.dialect.name != "postgresql":
        return

    connection.exec_driver_sql(f"SET ROLE {MIGRATION_OWNER_ROLE}")
