"""Email-verification credentials: digests and lifecycle only, never raw links/content.

email_hash is the SHA-256 of the account's normalized email when the token
was issued, so a token can only verify the address it was sent to.
"""
from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .user import utc_now


def lowercase_hex64_check(column: str) -> str:
    """Portable on PostgreSQL and SQLite, including rejection of non-hex characters."""
    remainder = column
    for digit in "0123456789abcdef":
        remainder = f"replace({remainder}, '{digit}', '')"
    return f"length({column}) = 64 AND length({remainder}) = 0"


class AccountEmailVerificationToken(Base):
    __tablename__ = "account_email_verification_tokens"
    __table_args__ = (
        CheckConstraint(lowercase_hex64_check("email_hash"), name="email_hash_hex"),
        CheckConstraint(lowercase_hex64_check("token_hash"), name="token_hash_hex"),
        CheckConstraint("expires_at > created_at", name="expiry_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("kalillac.users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    email_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
