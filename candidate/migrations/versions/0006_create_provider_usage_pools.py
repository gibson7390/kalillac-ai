"""Numeric service pools; authoring only, no application identity/content."""
from alembic import op
import sqlalchemy as sa
from kalillac_db.migration_roles import migration_app_role

revision = "0006_provider_usage_pools"
down_revision = "0005_account_usage_slots"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "provider_usage_pools",
        sa.Column("provider", sa.String(8), nullable=False),
        sa.Column("period_start", sa.BigInteger(), nullable=False),
        sa.Column("period_seconds", sa.BigInteger(), nullable=False),
        sa.Column("total_limit", sa.BigInteger(), nullable=False),
        sa.Column("anonymous_limit", sa.BigInteger(), nullable=False),
        sa.Column("total_attempts", sa.BigInteger(), nullable=False),
        sa.Column("anonymous_attempts", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("provider IN ('openai', 'brave', 'tavily')", name=op.f("ck_provider_usage_pools_provider_valid")),
        sa.CheckConstraint("period_seconds > 0 AND period_start >= 0", name=op.f("ck_provider_usage_pools_period_valid")),
        sa.CheckConstraint("total_limit >= 0 AND anonymous_limit >= 0 AND anonymous_limit <= total_limit", name=op.f("ck_provider_usage_pools_limits_valid")),
        sa.CheckConstraint("total_attempts >= 0 AND anonymous_attempts >= 0 AND anonymous_attempts <= total_attempts AND total_attempts <= total_limit AND anonymous_attempts <= anonymous_limit", name=op.f("ck_provider_usage_pools_counts_valid")),
        sa.PrimaryKeyConstraint("provider", name="pk_provider_usage_pools"),
        schema="kalillac",
    )
    if op.get_context().dialect.name == "postgresql":
        role = migration_app_role()
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE kalillac.provider_usage_pools TO {role}")


def downgrade():
    op.drop_table("provider_usage_pools", schema="kalillac")
