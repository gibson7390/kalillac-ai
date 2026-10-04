"""Account identity.

A user row is identity only. It carries no conversation, saved-chat,
memory, plan, price, or entitlement data: creating an account never makes
conversations persistent.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import Boolean, DateTime, String, Uuid, func, true
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    # Stored normalized (trimmed, lowercased) so uniqueness is
    # case-insensitive without a database extension.
    email: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        unique=True,
    )

    # Argon2id PHC string; never a plaintext password.
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
