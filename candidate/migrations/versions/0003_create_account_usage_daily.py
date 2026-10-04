"""Create the aggregate daily usage table.

One row per account per UTC date with only numeric totals: successful
chats, request characters, and response characters (character counts, not
tokens). Non-negative CHECK constraints. No conversation text, session ids,
search data, tier, or billing data. No backfill: usage starts at zero.

Revision ID: 0003_account_usage_daily
Revises: 0002_account_entitlements
Create Date: 2026-10-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from kalillac_db.migration_roles import migration_app_role


revision = "0003_account_usage_daily"
down_revision = "0002_account_entitlements"
branch_labels = None
depends_on = None

SCHEMA = "kalillac"

# The runtime role (KALILLAC_MIGRATION_APP_ROLE, default kalillac_app)
# receives table DML only: no schema CREATE, no ownership, and nothing on
# alembic_version. No default privileges are created.
APP_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"

COUNTERS = ("successful_chats", "request_chars", "response_chars")


def upgrade() -> None:
    op.create_table(
        "account_usage_daily",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        *(
            sa.Column(
                counter,
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
            for counter in COUNTERS
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
        *(
            sa.CheckConstraint(
                f"{counter} >= 0",
                # op.f marks the name final so the naming convention does
                # not prefix it a second time.
                name=op.f(f"ck_account_usage_daily_{counter}_non_negative"),
            )
            for counter in COUNTERS
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_account_usage_daily_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "usage_date",
            name="pk_account_usage_daily",
        ),
        schema=SCHEMA,
    )

    # Roles exist only on PostgreSQL; SQLite test databases have none.
    if op.get_context().dialect.name == "postgresql":
        # Validated identifier; an invalid name aborts the migration.
        app_role = migration_app_role()

        op.execute(
            f"GRANT {APP_TABLE_PRIVILEGES} ON TABLE "
            f"{SCHEMA}.account_usage_daily TO {app_role}"
        )


def downgrade() -> None:
    op.drop_table("account_usage_daily", schema=SCHEMA)
