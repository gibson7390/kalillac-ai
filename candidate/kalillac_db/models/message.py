"""One stored message of a completed exchange.

Only completed exchanges are stored: a user message and the assistant reply
are inserted together. Content is raw text, never HTML. There is no system
role, and the idempotency key is set on user messages only.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .user import utc_now


USER_MESSAGE_MAX = 4000
ASSISTANT_MESSAGE_MAX = 24000


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["kalillac.conversations.id", "kalillac.conversations.user_id"],
            name="fk_messages_conversation_id_user_id_conversations",
            ondelete="CASCADE",
        ),
        UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_id_seq"),
        UniqueConstraint(
            "conversation_id", "client_request_id", name="uq_messages_conversation_id_client_request_id"
        ),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint("role IN ('user', 'assistant')", name="role_allowed"),
        CheckConstraint(
            f"(role = 'user' AND length(content) <= {USER_MESSAGE_MAX})"
            f" OR (role = 'assistant' AND length(content) <= {ASSISTANT_MESSAGE_MAX})",
            name="content_length",
        ),
        CheckConstraint(
            "(role = 'user' AND client_request_id IS NOT NULL)"
            " OR (role = 'assistant' AND client_request_id IS NULL)",
            name="request_id_on_user_only",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    client_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
