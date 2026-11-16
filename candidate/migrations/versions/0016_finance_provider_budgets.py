"""Financial Safeguards: provider budgets and the operator-action record.

Revision ID: 0016_finance_provider_budgets
Revises: 0015_finance_ledger

Two tables of financial and control metadata only (µUSD amounts, UTC period
dates, provider and scope names, action names, timestamps). No account,
visitor, cookie, address, key, prompt, response or operator identity is
stored or referenced.

finance_provider_buckets holds each provider's exposure per UTC day and
month, beside the scope buckets of 0015, so a provider's own dollar ceiling
can be enforced under the same row locks.

finance_operator_actions is the append-only record of explicit operator
actions (resolving a stale reservation, clearing an unsafe latch).

Runtime grants follow 0015: the application can never DELETE financial
history. It may insert and read an operator action (the reconciliation
command runs with the application's own database environment) but never
update or delete one. Clearing a latch still needs the owner role, because
only the owner may delete from finance_unsafe_latches.

The provider buckets start empty, which is exact only while
finance_reservations is empty: this revision refuses to run on PostgreSQL
if any reservation exists (Financial Safeguards have never been enabled, so
none does). The refusal is SQL the database itself evaluates, after locking
the reservations against new rows, so it holds equally when the migration
runs online and when its SQL is rendered offline (alembic --sql) and applied
later: offline rendering cannot see the ledger, so the check must travel
with the script (Codex audit D1).

Unused until KALILLAC_FINANCIAL_SAFEGUARDS_ENABLED is true. Downgrade drops
the two tables (development and test use).
"""
from alembic import op
import sqlalchemy as sa

from kalillac_db.migration_roles import migration_app_role

revision = "0016_finance_provider_budgets"
down_revision = "0015_finance_ledger"
branch_labels = depends_on = None

GRANTS = {"finance_provider_buckets": "SELECT, INSERT, UPDATE",
          "finance_operator_actions": "SELECT, INSERT"}

LEDGER_NOT_EMPTY = "finance_reservations is not empty: provider buckets cannot start at zero."
# Evaluated by the server, online or from rendered SQL. SHARE mode blocks new reservations until the
# migration commits, so none can appear between the check and the new tables.
EMPTY_LEDGER_GUARD = (
    "LOCK TABLE kalillac.finance_reservations IN SHARE MODE",
    "DO $finance_provider_budgets_guard$\nBEGIN\n"
    " IF EXISTS (SELECT 1 FROM kalillac.finance_reservations) THEN\n"
    "  RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = '" + LEDGER_NOT_EMPTY + "';\n"
    " END IF;\nEND $finance_provider_budgets_guard$",
)


def upgrade():
    postgresql = op.get_context().dialect.name == "postgresql"
    if postgresql:
        for statement in EMPTY_LEDGER_GUARD:
            op.execute(statement)
    op.create_table(
        "finance_provider_buckets",
        sa.Column("period_kind", sa.String(5), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(8), nullable=False),
        sa.Column("reserved_microusd", sa.BigInteger(), nullable=False),
        sa.Column("settled_microusd", sa.BigInteger(), nullable=False),
        sa.Column("overrun_microusd", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("period_kind IN ('day', 'month')",
                           name=op.f("ck_finance_provider_buckets_period_kind_valid")),
        sa.CheckConstraint("provider IN ('openai', 'brave', 'tavily')",
                           name=op.f("ck_finance_provider_buckets_provider_valid")),
        sa.CheckConstraint("reserved_microusd >= 0 AND settled_microusd >= 0 AND overrun_microusd >= 0",
                           name=op.f("ck_finance_provider_buckets_amounts_valid")),
        sa.PrimaryKeyConstraint("period_kind", "period_start", "provider", name="pk_finance_provider_buckets"),
        schema="kalillac",
    )
    op.create_table(
        "finance_operator_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(13), nullable=False),
        sa.Column("scope", sa.String(6), nullable=True),
        sa.Column("reason", sa.String(18), nullable=True),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.Column("charged_microusd", sa.BigInteger(), nullable=False),
        sa.Column("performed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('resolve_stale', 'clear_latch')",
                           name=op.f("ck_finance_operator_actions_action_valid")),
        sa.CheckConstraint("scope IS NULL OR scope IN ('global', 'free', 'paid')",
                           name=op.f("ck_finance_operator_actions_scope_valid")),
        sa.CheckConstraint("reason IS NULL OR reason IN ('overrun', 'bound_exceeded', 'headroom_exhausted')",
                           name=op.f("ck_finance_operator_actions_reason_valid")),
        sa.CheckConstraint("charged_microusd >= 0", name=op.f("ck_finance_operator_actions_charged_valid")),
        sa.CheckConstraint("(action = 'clear_latch') = (reason IS NOT NULL)",
                           name=op.f("ck_finance_operator_actions_latch_reason_recorded")),
        sa.PrimaryKeyConstraint("id", name="pk_finance_operator_actions"),
        schema="kalillac",
    )
    if postgresql:
        # Calendar months are stored by their first day.
        op.create_check_constraint("month_start_valid", "finance_provider_buckets",
                                   "period_kind <> 'month' OR EXTRACT(DAY FROM period_start) = 1", schema="kalillac")
        role = migration_app_role()
        for table, privileges in GRANTS.items():
            op.execute(f"GRANT {privileges} ON TABLE kalillac.{table} TO {role}")


def downgrade():
    op.drop_table("finance_operator_actions", schema="kalillac")
    op.drop_table("finance_provider_buckets", schema="kalillac")
