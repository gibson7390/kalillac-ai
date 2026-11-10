"""Anonymous daily allowance tables: per-UTC-day digest leases only.

Stores only a 64-character per-UTC-day visitor digest, the UTC usage date,
numeric lease slots, lease state and timestamps. No raw visitor token, IP
address or IP-derived value, session id, user agent, account id or
conversation content. Authoring only; activation requires a separately
reviewed schema rollout.

A completed lease carries completed_date equal to its own usage_date. Phase 2
must handle a request crossing UTC midnight by atomically releasing or
replacing the old day's pending slot and reserving a slot under the new
day's digest, without storing a stable cross-day identifier.
"""
from alembic import op
import sqlalchemy as sa
from kalillac_db.migration_roles import migration_app_role

revision = "0007_anonymous_usage"
down_revision = "0006_provider_usage_pools"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "anonymous_usage_days",
        sa.Column("visitor_digest", sa.String(64), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.CheckConstraint("length(visitor_digest) = 64", name=op.f("ck_anonymous_usage_days_digest_length")),
        sa.PrimaryKeyConstraint("visitor_digest", "usage_date", name="pk_anonymous_usage_days"),
        schema="kalillac",
    )
    op.create_index("ix_anonymous_usage_days_usage_date", "anonymous_usage_days", ["usage_date"],
                    schema="kalillac")
    op.create_table(
        "anonymous_usage_slots",
        sa.Column("visitor_digest", sa.String(64), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("slot", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("completed_date", sa.Date(), nullable=True),
        sa.CheckConstraint("slot >= 0", name=op.f("ck_anonymous_usage_slots_slot_non_negative")),
        sa.CheckConstraint("state IN ('pending', 'ready', 'uncertain', 'completed', 'released')",
                           name=op.f("ck_anonymous_usage_slots_state_valid")),
        # A lease is completed only on its own usage_date (each UTC day has its
        # own digest); IS NOT NULL keeps a NULL date from passing as unknown.
        sa.CheckConstraint(
            "(state IN ('pending', 'released') AND completed_date IS NULL) OR "
            "(state IN ('ready', 'uncertain', 'completed') AND completed_date IS NOT NULL "
            "AND completed_date = usage_date)",
            name=op.f("ck_anonymous_usage_slots_completion_date_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["visitor_digest", "usage_date"],
            ["kalillac.anonymous_usage_days.visitor_digest", "kalillac.anonymous_usage_days.usage_date"],
            ondelete="CASCADE", name="fk_anonymous_usage_slots_visitor_digest_anonymous_usage_days",
        ),
        sa.PrimaryKeyConstraint("visitor_digest", "usage_date", "slot", name="pk_anonymous_usage_slots"),
        schema="kalillac",
    )
    if op.get_context().dialect.name == "postgresql":
        role = migration_app_role()
        for table in ("anonymous_usage_days", "anonymous_usage_slots"):
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kalillac.{table} TO {role}")


def downgrade():
    op.drop_table("anonymous_usage_slots", schema="kalillac")
    op.drop_index("ix_anonymous_usage_days_usage_date", table_name="anonymous_usage_days", schema="kalillac")
    op.drop_table("anonymous_usage_days", schema="kalillac")
