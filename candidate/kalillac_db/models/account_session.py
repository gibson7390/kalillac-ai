"""Server-side account sign-in sessions.

This is authentication state only, unrelated to Kalillac's temporary chat
session. The browser holds an opaque random token; only its SHA-256 digest
is stored, so a database read cannot be replayed as a cookie. No IP
address, user agent, or conversation data is recorded.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .user import utc_now


class AccountSession(Base):
    __tablename__ = "account_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("kalillac.users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Hex SHA-256 of the cookie token.
    token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
