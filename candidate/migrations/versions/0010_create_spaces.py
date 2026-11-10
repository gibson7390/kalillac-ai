"""Spaces: account-owned Spaces, their conversations and completed messages.
Explicit Space memory is not part of this revision.

Ownership is enforced by the database: every child carries user_id and
references its parent through a composite (parent_id, user_id) foreign key,
so a row whose account differs from its parent's owner is rejected. Deletion
is hard deletion through ON DELETE CASCADE; there are no soft-delete columns.
Additive only: no existing table changes.

Revision ID: 0010_spaces
Revises: 0009_account_email_verification
"""
from alembic import op
import sqlalchemy as sa

from kalillac_db.migration_roles import migration_app_role

revision = "0010_spaces"
down_revision = "0009_account_email_verification"
branch_labels = depends_on = None

SCHEMA = "kalillac"
TABLES = ("spaces", "conversations", "messages")


def _timestamps():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def upgrade():
    op.create_table(
        "spaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("instructions", sa.Text(), server_default="", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(),
        sa.Column("instructions_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(trim(name)) BETWEEN 1 AND 80", name=op.f("ck_spaces_name_length")),
        sa.CheckConstraint("length(instructions) <= 8000", name=op.f("ck_spaces_instructions_length")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_spaces_version_positive")),
        sa.ForeignKeyConstraint(
            ["user_id"], [f"{SCHEMA}.users.id"], ondelete="CASCADE", name="fk_spaces_user_id_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_spaces"),
        sa.UniqueConstraint("id", "user_id", name="uq_spaces_id_user_id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_spaces_user_id_updated_at_id", "spaces",
        ["user_id", "updated_at", "id"], schema=SCHEMA,
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("space_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_request_id", sa.Uuid(), nullable=True),
        sa.Column("active_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        *_timestamps(),
        sa.CheckConstraint("length(trim(title)) BETWEEN 1 AND 120", name=op.f("ck_conversations_title_length")),
        sa.CheckConstraint("message_count BETWEEN 0 AND 400", name=op.f("ck_conversations_message_count_range")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_conversations_version_positive")),
        sa.CheckConstraint(
            "(active_request_id IS NULL) = (active_until IS NULL)", name=op.f("ck_conversations_lease_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["space_id", "user_id"], [f"{SCHEMA}.spaces.id", f"{SCHEMA}.spaces.user_id"],
            ondelete="CASCADE", name="fk_conversations_space_id_user_id_spaces",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        sa.UniqueConstraint("id", "user_id", name="uq_conversations_id_user_id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_conversations_user_id_space_id_updated_at_id", "conversations",
        ["user_id", "space_id", "updated_at", "id"], schema=SCHEMA,
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("client_request_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("seq >= 1", name=op.f("ck_messages_seq_positive")),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_messages_role_allowed")),
        sa.CheckConstraint(
            "(role = 'user' AND length(content) <= 4000) OR (role = 'assistant' AND length(content) <= 24000)",
            name=op.f("ck_messages_content_length"),
        ),
        sa.CheckConstraint(
            "(role = 'user' AND client_request_id IS NOT NULL) OR (role = 'assistant' AND client_request_id IS NULL)",
            name=op.f("ck_messages_request_id_on_user_only"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "user_id"], [f"{SCHEMA}.conversations.id", f"{SCHEMA}.conversations.user_id"],
            ondelete="CASCADE", name="fk_messages_conversation_id_user_id_conversations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_id_seq"),
        sa.UniqueConstraint(
            "conversation_id", "client_request_id", name="uq_messages_conversation_id_client_request_id",
        ),
        schema=SCHEMA,
    )

    if op.get_context().dialect.name == "postgresql":
        # Explicit table DML only: no DDL, no default privileges.
        app_role = migration_app_role()
        for table in TABLES:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {SCHEMA}.{table} TO {app_role}")


def downgrade():
    op.drop_table("messages", schema=SCHEMA)
    op.drop_index("ix_conversations_user_id_space_id_updated_at_id", table_name="conversations", schema=SCHEMA)
    op.drop_table("conversations", schema=SCHEMA)
    op.drop_index("ix_spaces_user_id_updated_at_id", table_name="spaces", schema=SCHEMA)
    op.drop_table("spaces", schema=SCHEMA)
