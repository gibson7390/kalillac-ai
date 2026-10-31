"""Spaces: one account's named workspace.

A Space belongs to exactly one account. Its instructions are a single field;
v1 keeps no instruction history. Deleting the account deletes its Spaces.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
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


SPACE_NAME_MAX = 80
SPACE_INSTRUCTIONS_MAX = 8000


class Space(Base):
    __tablename__ = "spaces"
    __table_args__ = (
        # Children reference (id, user_id), so a child can never sit under
        # another account's Space.
        UniqueConstraint("id", "user_id", name="uq_spaces_id_user_id"),
        CheckConstraint(
            f"length(trim(name)) BETWEEN 1 AND {SPACE_NAME_MAX}", name="name_length"
        ),
        CheckConstraint(
            f"length(instructions) <= {SPACE_INSTRUCTIONS_MAX}", name="instructions_length"
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        # Scanned backward for ORDER BY updated_at DESC, id DESC (keyset pages).
        Index("ix_spaces_user_id_updated_at_id", "user_id", "updated_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("kalillac.users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(SPACE_NAME_MAX), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    instructions_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
