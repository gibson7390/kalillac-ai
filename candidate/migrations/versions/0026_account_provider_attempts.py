"""Per-account billable provider attempts (Codex finding F3).

Revision ID: 0026_account_provider_attempts
Revises: 0025_v1_row_lock_anchor

One counter on account_usage_daily: every provider attempt admitted for a signed-in account's request on
that UTC day, whether the attempt succeeded or failed. A failed response does not count against the
customer's successful-response allowance (successful_chats), so without this a customer whose requests keep
failing could keep the providers working without limit; with provider ceilings on, each attempt is now
admitted against the account's own daily attempt limit (KALILLAC_ACCOUNT_DAILY_PROVIDER_ATTEMPTS_FREE /
_PAID) in the same transaction as the service pool. An attempt refused before dispatch is refunded.

A number only, like the table's other counters. The column is NOT NULL with a constant default, so on
PostgreSQL it is added without rewriting the table. The runtime role's existing table rights cover it
(0003; the V1 transition does not change them). Its rows are removed with the table's other usage rows by
the usage cleanup and with the account (ON DELETE CASCADE).
"""
from alembic import op
import sqlalchemy as sa

revision = "0026_account_provider_attempts"
down_revision = "0025_v1_row_lock_anchor"
branch_labels = depends_on = None

SCHEMA = "kalillac"
CONSTRAINT = "ck_account_usage_daily_provider_attempts_non_negative"


def upgrade():
    op.add_column("account_usage_daily",
                  sa.Column("provider_attempts", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
                  schema=SCHEMA)
    if op.get_context().dialect.name == "postgresql":
        op.create_check_constraint(op.f(CONSTRAINT), "account_usage_daily", "provider_attempts >= 0", schema=SCHEMA)


def downgrade():
    if op.get_context().dialect.name == "postgresql":
        op.drop_constraint(op.f(CONSTRAINT), "account_usage_daily", type_="check", schema=SCHEMA)
    op.drop_column("account_usage_daily", "provider_attempts", schema=SCHEMA)
