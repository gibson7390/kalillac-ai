"""Record refunds and disputes for operator review.

Revision ID: 0017_billing_cases
Revises: 0016_finance_provider_budgets

billing_cases: one row per refunded charge or dispute that Stripe reported through a verified
webhook: Stripe's ids, Stripe's status and reason (fetched from Stripe), and Kalillac's review
status. No amount, currency, card, payment method, invoice, address, secret or conversation data:
Stripe is billing truth, and an operator reads those there. Kalillac issues no refund and changes no
entitlement because of a case.

The runtime role receives SELECT, INSERT and UPDATE, never DELETE: a case's history cannot be erased
by the application. The account link is cleared (ON DELETE SET NULL) if an account is deleted, so the
billing record outlives the identity, unlinked, as the retention proposal requires.

Unused until KALILLAC_BILLING_ENABLED is true. Downgrade drops the table (development and test use).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from kalillac_db.migration_roles import migration_app_role

revision = "0017_billing_cases"
down_revision = "0016_finance_provider_budgets"
branch_labels = depends_on = None

SCHEMA = "kalillac"
APP_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE"


def upgrade() -> None:
    op.create_table(
        "billing_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("provider_object_id", sa.String(length=255), nullable=False),
        sa.Column("charge_id", sa.String(length=255), nullable=True),
        sa.Column("customer_id", sa.String(length=255), nullable=True),
        sa.Column("provider_status", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.String(length=60), nullable=True),
        sa.Column("review_status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("last_event_id", sa.String(length=255), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.users.id"], name="fk_billing_cases_user_id_users",
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_billing_cases"),
        sa.UniqueConstraint("kind", "provider_object_id", name="uq_billing_cases_kind_object"),
        sa.CheckConstraint("kind IN ('refund', 'dispute')", name="ck_billing_cases_kind"),
        sa.CheckConstraint("review_status IN ('open', 'acknowledged', 'closed')", name="ck_billing_cases_review"),
        schema=SCHEMA,
    )
    op.create_index("ix_kalillac_billing_cases_user_id", "billing_cases", ["user_id"], schema=SCHEMA)

    # Roles exist only on PostgreSQL; SQLite test databases have none.
    if op.get_context().dialect.name == "postgresql":
        op.execute(f"GRANT {APP_TABLE_PRIVILEGES} ON TABLE {SCHEMA}.billing_cases TO {migration_app_role()}")


def downgrade() -> None:
    op.drop_index("ix_kalillac_billing_cases_user_id", table_name="billing_cases", schema=SCHEMA)
    op.drop_table("billing_cases", schema=SCHEMA)
