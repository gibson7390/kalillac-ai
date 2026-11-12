"""Financial Safeguards ledger: buckets, reservations and unsafe latches.

Revision ID: 0015_finance_ledger
Revises: 0014_v1_session_issuance

Three tables of financial and control metadata only (µUSD amounts, UTC
period dates, provider and scope names, states, timestamps). No account,
visitor, cookie, address, key, prompt or response is stored or referenced.

Runtime grants follow the existing convention (migration_app_role) but are
narrower than full DML: the application can never DELETE financial history,
and can only INSERT an unsafe latch, never update or delete one, so a latch
survives restarts and application code until an operator clears it.

Unused until KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED is true. Downgrade drops
the three tables (development and test use).
"""
from alembic import op
import sqlalchemy as sa

from kalillac_db.migration_roles import migration_app_role

revision = "0015_finance_ledger"
down_revision = "0014_v1_session_issuance"
branch_labels = depends_on = None

GRANTS = {"finance_ledger_buckets": "SELECT, INSERT, UPDATE",
          "finance_reservations": "SELECT, INSERT, UPDATE",
          "finance_unsafe_latches": "SELECT, INSERT"}


def upgrade():
    op.create_table(
        "finance_ledger_buckets",
        sa.Column("period_kind", sa.String(5), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("scope", sa.String(6), nullable=False),
        sa.Column("reserved_microusd", sa.BigInteger(), nullable=False),
        sa.Column("settled_microusd", sa.BigInteger(), nullable=False),
        sa.Column("overrun_microusd", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("period_kind IN ('day', 'month')", name=op.f("ck_finance_ledger_buckets_period_kind_valid")),
        sa.CheckConstraint("scope IN ('global', 'free', 'paid')", name=op.f("ck_finance_ledger_buckets_scope_valid")),
        sa.CheckConstraint("reserved_microusd >= 0 AND settled_microusd >= 0 AND overrun_microusd >= 0",
                           name=op.f("ck_finance_ledger_buckets_amounts_valid")),
        sa.PrimaryKeyConstraint("period_kind", "period_start", "scope", name="pk_finance_ledger_buckets"),
        schema="kalillac",
    )
    op.create_table(
        "finance_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(8), nullable=False),
        sa.Column("scope", sa.String(4), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("reserved_microusd", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(8), nullable=False),
        sa.Column("charged_microusd", sa.BigInteger(), nullable=True),
        sa.Column("overrun_microusd", sa.BigInteger(), nullable=False),
        sa.Column("bound_exceeded", sa.Boolean(), nullable=False),
        sa.Column("basis", sa.String(8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("provider IN ('openai', 'brave', 'tavily')", name=op.f("ck_finance_reservations_provider_valid")),
        sa.CheckConstraint("scope IN ('free', 'paid')", name=op.f("ck_finance_reservations_scope_valid")),
        sa.CheckConstraint("state IN ('reserved', 'released', 'settled')", name=op.f("ck_finance_reservations_state_valid")),
        sa.CheckConstraint("basis IS NULL OR basis IN ('usage', 'missing', 'invalid', 'partial', 'unknown')",
                           name=op.f("ck_finance_reservations_basis_valid")),
        sa.CheckConstraint("reserved_microusd >= 0 AND (charged_microusd IS NULL OR charged_microusd >= 0) "
                           "AND overrun_microusd >= 0", name=op.f("ck_finance_reservations_amounts_valid")),
        sa.CheckConstraint("(state = 'settled') = (charged_microusd IS NOT NULL AND basis IS NOT NULL)",
                           name=op.f("ck_finance_reservations_settlement_complete")),
        sa.PrimaryKeyConstraint("id", name="pk_finance_reservations"),
        schema="kalillac",
    )
    op.create_table(
        "finance_unsafe_latches",
        sa.Column("scope", sa.String(6), nullable=False),
        sa.Column("reason", sa.String(18), nullable=False),
        sa.Column("latched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("scope IN ('global', 'free', 'paid')", name=op.f("ck_finance_unsafe_latches_scope_valid")),
        sa.CheckConstraint("reason IN ('overrun', 'bound_exceeded', 'headroom_exhausted')",
                           name=op.f("ck_finance_unsafe_latches_reason_valid")),
        sa.PrimaryKeyConstraint("scope", name="pk_finance_unsafe_latches"),
        schema="kalillac",
    )
    if op.get_context().dialect.name == "postgresql":
        # Calendar months are stored by their first day.
        op.create_check_constraint("month_start_valid", "finance_ledger_buckets",
                                   "period_kind <> 'month' OR EXTRACT(DAY FROM period_start) = 1", schema="kalillac")
        op.create_check_constraint("month_start_valid", "finance_reservations",
                                   "EXTRACT(DAY FROM month) = 1 AND date_trunc('month', day)::date = month",
                                   schema="kalillac")
        role = migration_app_role()
        for table, privileges in GRANTS.items():
            op.execute(f"GRANT {privileges} ON TABLE kalillac.{table} TO {role}")


def downgrade():
    op.drop_table("finance_unsafe_latches", schema="kalillac")
    op.drop_table("finance_reservations", schema="kalillac")
    op.drop_table("finance_ledger_buckets", schema="kalillac")
