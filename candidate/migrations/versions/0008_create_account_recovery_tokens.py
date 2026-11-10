"""Password-reset token lifecycle; no tokens, links, content or provider data."""
from alembic import op
import sqlalchemy as sa

from kalillac_db.migration_roles import migration_app_role

revision = "0008_account_recovery_tokens"
down_revision = "0007_anonymous_usage"
branch_labels = depends_on = None


def upgrade():
    remainder = "token_hash"
    for digit in "0123456789abcdef":
        remainder = f"replace({remainder}, '{digit}', '')"
    op.create_table(
        "account_recovery_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("purpose = 'password_reset'", name=op.f("ck_account_recovery_tokens_purpose_valid")),
        sa.CheckConstraint(
            f"length(token_hash) = 64 AND length({remainder}) = 0",
            name=op.f("ck_account_recovery_tokens_token_hash_hex"),
        ),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_account_recovery_tokens_expiry_valid")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["kalillac.users.id"], ondelete="CASCADE",
            name="fk_account_recovery_tokens_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_account_recovery_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_account_recovery_tokens_token_hash"),
        schema="kalillac",
    )
    op.create_index(
        "ix_account_recovery_tokens_user_id", "account_recovery_tokens", ["user_id"], schema="kalillac",
    )
    if op.get_context().dialect.name == "postgresql":
        # Runtime never deletes tokens; user deletion cascades. No DDL or extra DML.
        op.execute(
            "GRANT SELECT, INSERT, UPDATE ON TABLE kalillac.account_recovery_tokens "
            f"TO {migration_app_role()}"
        )


def downgrade():
    op.drop_index("ix_account_recovery_tokens_user_id", table_name="account_recovery_tokens", schema="kalillac")
    op.drop_table("account_recovery_tokens", schema="kalillac")
