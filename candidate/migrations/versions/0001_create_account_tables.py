"""Create account identity and sign-in session tables.

Identity only: no conversation, saved-chat, memory, plan, price, or
entitlement columns.

Revision ID: 0001_account_tables
Revises:
Create Date: 2026-10-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from kalillac_db.migration_roles import migration_app_role


revision = "0001_account_tables"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "kalillac"

# The runtime role (KALILLAC_MIGRATION_APP_ROLE, default kalillac_app)
# receives table DML only: no schema CREATE, no ownership, and nothing on
# alembic_version. Grants are explicit per table; there are deliberately no
# default privileges.
APP_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
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
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        schema=SCHEMA,
    )

    op.create_table(
        "account_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_account_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_account_sessions"),
        sa.UniqueConstraint(
            "token_hash",
            name="uq_account_sessions_token_hash",
        ),
        schema=SCHEMA,
    )

    op.create_index(
        "ix_account_sessions_user_id",
        "account_sessions",
        ["user_id"],
        unique=False,
        schema=SCHEMA,
    )

    # Roles exist only on PostgreSQL; SQLite test databases have none.
    if op.get_context().dialect.name == "postgresql":
        # Validated identifier; an invalid name aborts the migration.
        app_role = migration_app_role()

        for table in ("users", "account_sessions"):
            op.execute(
                f"GRANT {APP_TABLE_PRIVILEGES} ON TABLE {SCHEMA}.{table} "
                f"TO {app_role}"
            )


def downgrade() -> None:
    op.drop_index(
        "ix_account_sessions_user_id",
        table_name="account_sessions",
        schema=SCHEMA,
    )
    op.drop_table("account_sessions", schema=SCHEMA)
    op.drop_table("users", schema=SCHEMA)
