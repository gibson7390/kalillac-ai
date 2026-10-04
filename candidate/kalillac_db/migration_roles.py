"""PostgreSQL role handling for Alembic migrations.

The migration login role (for example kalillac_migrate) is NOINHERIT and
holds explicit membership in the owner role. Before migrating it must assume
that role, so every migrated object (including alembic_version) is owned by
the NOLOGIN owner rather than by the login role. If SET ROLE fails, the
error propagates and the migration aborts; there is no fallback.

Role names are per environment so staging can use its own database and
roles (kalillac_staging_owner / kalillac_staging_app):

- KALILLAC_MIGRATION_OWNER_ROLE: role assumed with SET ROLE
  (default kalillac_owner)
- KALILLAC_MIGRATION_APP_ROLE: runtime role granted table DML
  (default kalillac_app)

Each name is validated as a simple lowercase PostgreSQL identifier before it
is placed in SQL. An invalid name raises InvalidRoleName and the migration
fails; it never falls back to the default.
"""

from __future__ import annotations

import os
import re

from sqlalchemy.engine import Connection


DEFAULT_MIGRATION_OWNER_ROLE = "kalillac_owner"
DEFAULT_MIGRATION_APP_ROLE = "kalillac_app"

OWNER_ROLE_ENV = "KALILLAC_MIGRATION_OWNER_ROLE"
APP_ROLE_ENV = "KALILLAC_MIGRATION_APP_ROLE"

# Unquoted PostgreSQL identifier, lowercase only (unquoted names fold to
# lowercase, so uppercase would silently name a different role), at most
# 63 bytes (NAMEDATALEN - 1).
_ROLE_NAME_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")


class InvalidRoleName(ValueError):
    """A configured role name is not a safe simple identifier."""


def validate_role_name(name: str, setting: str) -> str:
    if not _ROLE_NAME_RE.fullmatch(name):
        raise InvalidRoleName(
            f"{setting} must be a lowercase PostgreSQL identifier "
            "(letters, digits, underscores; at most 63 characters)."
        )

    return name


def _configured_role(setting: str, default: str) -> str:
    # Unset or blank means "use the default"; any other value must be valid.
    value = os.getenv(setting, "").strip()
    return validate_role_name(value or default, setting)


def migration_owner_role() -> str:
    return _configured_role(OWNER_ROLE_ENV, DEFAULT_MIGRATION_OWNER_ROLE)


def migration_app_role() -> str:
    return _configured_role(APP_ROLE_ENV, DEFAULT_MIGRATION_APP_ROLE)


def assume_migration_owner(connection: Connection) -> None:
    """SET ROLE to the configured owner on PostgreSQL; no-op elsewhere."""

    if connection.dialect.name != "postgresql":
        return

    connection.exec_driver_sql(f"SET ROLE {migration_owner_role()}")
