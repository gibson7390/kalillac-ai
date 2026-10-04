"""Create the per-account entitlement table.

One row per account: tier ('free' or 'paid', enforced by a CHECK
constraint), the source that set it, timestamps, and an optional expiry.
No billing ids, prices, payment state, or usage counters.

Invariant: exactly one row per account. Accounts that existed before this
migration are backfilled with a free row (source 'migration', no expiry);
accounts registered afterwards get theirs from registration.

Revision ID: 0002_account_entitlements
Revises: 0001_account_tables
Create Date: 2026-10-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from kalillac_db.migration_roles import migration_app_role


revision = "0002_account_entitlements"
down_revision = "0001_account_tables"
branch_labels = None
depends_on = None

SCHEMA = "kalillac"

# The runtime role (KALILLAC_MIGRATION_APP_ROLE, default kalillac_app)
# receives table DML only: no schema CREATE, no ownership, and nothing on
# alembic_version. No default privileges are created.
APP_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"

# Source recorded on rows created by this migration's backfill.
BACKFILL_SOURCE = "migration"


def upgrade() -> None:
    op.create_table(
        "account_entitlements",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "tier",
            sa.String(length=16),
            server_default="free",
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            server_default="registration",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "tier IN ('free', 'paid')",
            # op.f marks the name final; otherwise the metadata naming
            # convention would prefix it a second time.
            name=op.f("ck_account_entitlements_tier_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_account_entitlements_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_account_entitlements"),
        schema=SCHEMA,
    )

    # Backfill: one free entitlement for every pre-existing account.
    # Portable SQL (SQLite and PostgreSQL); NOT EXISTS keeps it from ever
    # adding a second row for an account.
    op.execute(
        f"INSERT INTO {SCHEMA}.account_entitlements "
        "(user_id, tier, source, created_at, updated_at, expires_at) "
        f"SELECT u.id, 'free', '{BACKFILL_SOURCE}', "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL "
        f"FROM {SCHEMA}.users AS u "
        "WHERE NOT EXISTS ("
        f"SELECT 1 FROM {SCHEMA}.account_entitlements AS e "
        "WHERE e.user_id = u.id)"
    )

    # Roles exist only on PostgreSQL; SQLite test databases have none.
    if op.get_context().dialect.name == "postgresql":
        # Validated identifier; an invalid name aborts the migration.
        app_role = migration_app_role()

        op.execute(
            f"GRANT {APP_TABLE_PRIVILEGES} ON TABLE "
            f"{SCHEMA}.account_entitlements TO {app_role}"
        )


def downgrade() -> None:
    op.drop_table("account_entitlements", schema=SCHEMA)
