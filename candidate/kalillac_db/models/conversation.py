"""A conversation inside one Space.

user_id repeats the Space owner and the composite foreign key on
(space_id, user_id) makes the database reject any other owner. The
active_request_id / active_until pair is the one-send-at-a-time lease.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .user import utc_now


CONVERSATION_TITLE_MAX = 120
CONVERSATION_MESSAGES_MAX = 400


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["space_id", "user_id"],
            ["kalillac.spaces.id", "kalillac.spaces.user_id"],
            name="fk_conversations_space_id_user_id_spaces",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "user_id", name="uq_conversations_id_user_id"),
        CheckConstraint(
            f"length(trim(title)) BETWEEN 1 AND {CONVERSATION_TITLE_MAX}", name="title_length"
        ),
        CheckConstraint(
            f"message_count BETWEEN 0 AND {CONVERSATION_MESSAGES_MAX}", name="message_count_range"
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(active_request_id IS NULL) = (active_until IS NULL)", name="lease_complete"
        ),
        # Scanned backward for ORDER BY updated_at DESC, id DESC (keyset pages).
        Index("ix_conversations_user_id_space_id_updated_at_id", "user_id", "space_id", "updated_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    space_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    title: Mapped[str] = mapped_column(String(CONVERSATION_TITLE_MAX), nullable=False)
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    active_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
