"""Email-verification state and token lifecycle; no tokens, links, content or provider data.

Adds nullable users.email_verified_at (every existing account stays
unverified; no row is rewritten) and account_email_verification_tokens.

Revision ID: 0009_account_email_verification
Revises: 0008_account_recovery_tokens
"""
from alembic import op
import sqlalchemy as sa

from kalillac_db.migration_roles import migration_app_role

revision = "0009_account_email_verification"
down_revision = "0008_account_recovery_tokens"
branch_labels = depends_on = None


def _lowercase_hex64(column):
    remainder = column
    for digit in "0123456789abcdef":
        remainder = f"replace({remainder}, '{digit}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


def upgrade():
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        schema="kalillac",
    )
    op.create_table(
        "account_email_verification_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("email_hash", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            _lowercase_hex64("email_hash"),
            name=op.f("ck_account_email_verification_tokens_email_hash_hex"),
        ),
        sa.CheckConstraint(
            _lowercase_hex64("token_hash"),
            name=op.f("ck_account_email_verification_tokens_token_hash_hex"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_account_email_verification_tokens_expiry_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["kalillac.users.id"], ondelete="CASCADE",
            name="fk_account_email_verification_tokens_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_account_email_verification_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_account_email_verification_tokens_token_hash"),
        schema="kalillac",
    )
    op.create_index(
        "ix_account_email_verification_tokens_user_id", "account_email_verification_tokens",
        ["user_id"], schema="kalillac",
    )
    if op.get_context().dialect.name == "postgresql":
        # Runtime never deletes tokens; user deletion cascades. No DDL or extra DML.
        # users.email_verified_at is covered by the existing users table grant.
        op.execute(
            "GRANT SELECT, INSERT, UPDATE ON TABLE kalillac.account_email_verification_tokens "
            f"TO {migration_app_role()}"
        )


def downgrade():
    op.drop_index(
        "ix_account_email_verification_tokens_user_id",
        table_name="account_email_verification_tokens", schema="kalillac",
    )
    op.drop_table("account_email_verification_tokens", schema="kalillac")
    op.drop_column("users", "email_verified_at", schema="kalillac")
