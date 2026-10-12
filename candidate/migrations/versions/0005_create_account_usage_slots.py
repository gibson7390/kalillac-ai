"""Durable numeric account/day leases for optional daily enforcement.

No request/session/conversation identifiers or content. Authoring only;
activation requires a separately reviewed schema rollout.
"""
from alembic import op
import sqlalchemy as sa
from kalillac_db.migration_roles import migration_app_role

revision = "0005_account_usage_slots"
down_revision = "0004_account_billing"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "account_usage_slots",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("slot", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("completed_date", sa.Date(), nullable=True),
        sa.CheckConstraint("slot >= 0", name=op.f("ck_account_usage_slots_slot_non_negative")),
        sa.CheckConstraint("state IN ('pending', 'ready', 'uncertain', 'completed', 'released')", name=op.f("ck_account_usage_slots_state_valid")),
        sa.CheckConstraint("(state IN ('ready', 'uncertain', 'completed')) = (completed_date IS NOT NULL)", name=op.f("ck_account_usage_slots_completion_date_valid")),
        sa.ForeignKeyConstraint(["user_id"], ["kalillac.users.id"], ondelete="CASCADE", name="fk_account_usage_slots_user_id_users"),
        sa.PrimaryKeyConstraint("user_id", "usage_date", "slot", name="pk_account_usage_slots"),
        schema="kalillac",
    )
    if op.get_context().dialect.name == "postgresql":
        role = migration_app_role()
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kalillac.account_usage_slots TO {role}")


def downgrade():
    op.drop_table("account_usage_slots", schema="kalillac")
