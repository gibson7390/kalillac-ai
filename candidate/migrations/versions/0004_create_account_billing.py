"""Create Stripe billing linkage and webhook idempotency tables.

account_billing: one row per account with Stripe customer/subscription
references, subscription state, and the server-generated pending-Checkout
attempt. No card, payment-method, invoice, amount, secret, or conversation
data.

stripe_webhook_events: processed Stripe event ids (with type and time) for
idempotency. Raw webhook payloads are never stored.

No backfill: accounts have no billing row until a verified webhook arrives.

Revision ID: 0004_account_billing
Revises: 0003_account_usage_daily
Create Date: 2026-10-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from kalillac_db.migration_roles import migration_app_role


revision = "0004_account_billing"
down_revision = "0003_account_usage_daily"
branch_labels = None
depends_on = None

SCHEMA = "kalillac"

# The runtime role (KALILLAC_MIGRATION_APP_ROLE, default kalillac_app)
# receives table DML only: no schema CREATE, no ownership, and nothing on
# alembic_version. No default privileges are created.
APP_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"

NEW_TABLES = ("account_billing", "stripe_webhook_events")


def upgrade() -> None:
    op.create_table(
        "account_billing",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("stripe_customer_id", sa.String(length=255), nullable=True),
        sa.Column(
            "stripe_subscription_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column("stripe_price_id", sa.String(length=255), nullable=True),
        sa.Column("subscription_status", sa.String(length=32), nullable=True),
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "current_period_end",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # Pending Checkout (server-generated; see account_billing model).
        sa.Column("checkout_attempt_id", sa.Uuid(), nullable=True),
        sa.Column(
            "checkout_customer_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "checkout_attempt_created_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("checkout_price_id", sa.String(length=255), nullable=True),
        sa.Column(
            "checkout_success_url",
            sa.String(length=2048),
            nullable=True,
        ),
        sa.Column(
            "checkout_cancel_url",
            sa.String(length=2048),
            nullable=True,
        ),
        sa.Column(
            "stripe_checkout_session_id",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "checkout_session_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_account_billing_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_account_billing"),
        sa.UniqueConstraint(
            "stripe_customer_id",
            name="uq_account_billing_stripe_customer_id",
        ),
        sa.UniqueConstraint(
            "stripe_subscription_id",
            name="uq_account_billing_stripe_subscription_id",
        ),
        sa.UniqueConstraint(
            "stripe_checkout_session_id",
            name="uq_account_billing_stripe_checkout_session_id",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "stripe_webhook_events",
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_stripe_webhook_events"),
        schema=SCHEMA,
    )

    # Roles exist only on PostgreSQL; SQLite test databases have none.
    if op.get_context().dialect.name == "postgresql":
        # Validated identifier; an invalid name aborts the migration.
        app_role = migration_app_role()

        for table in NEW_TABLES:
            op.execute(
                f"GRANT {APP_TABLE_PRIVILEGES} ON TABLE {SCHEMA}.{table} "
                f"TO {app_role}"
            )


def downgrade() -> None:
    op.drop_table("stripe_webhook_events", schema=SCHEMA)
    op.drop_table("account_billing", schema=SCHEMA)
