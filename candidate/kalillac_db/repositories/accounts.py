"""Data access for account identity and sign-in sessions.

Functions take a caller-owned SQLAlchemy Session and never commit; the
caller's transaction scope decides.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import AccountSession, User


class EmailAlreadyRegistered(Exception):
    """The normalized email already belongs to an account."""


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes; PostgreSQL returns aware ones.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def get_user_by_email(session: Session, email: str) -> User | None:
    return session.scalar(select(User).where(User.email == email))


def create_user(session: Session, email: str, password_hash: str) -> User:
    if get_user_by_email(session, email) is not None:
        raise EmailAlreadyRegistered()

    user = User(email=email, password_hash=password_hash)

    try:
        # The savepoint keeps a concurrent duplicate from poisoning the
        # caller's transaction; the unique constraint is the real guard.
        with session.begin_nested():
            session.add(user)
            session.flush()
    except IntegrityError as exc:
        raise EmailAlreadyRegistered() from exc

    return user


def set_password_hash(session: Session, user: User, password_hash: str) -> None:
    user.password_hash = password_hash
    session.flush()


def create_account_session(
    session: Session,
    user_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
) -> AccountSession:
    account_session = AccountSession(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    session.add(account_session)
    session.flush()
    return account_session


def get_signed_in_user(
    session: Session,
    token_hash: str,
    now: datetime,
) -> User | None:
    """Return the active user for a live, unrevoked, unexpired session."""

    row = session.execute(
        select(AccountSession, User)
        .join(User, User.id == AccountSession.user_id)
        .where(AccountSession.token_hash == token_hash)
    ).first()

    if row is None:
        return None

    account_session, user = row

    if account_session.revoked_at is not None:
        return None

    if _as_utc(account_session.expires_at) <= now:
        return None

    if not user.is_active:
        return None

    return user


def revoke_account_session(
    session: Session,
    token_hash: str,
    now: datetime,
) -> None:
    session.execute(
        update(AccountSession)
        .where(
            AccountSession.token_hash == token_hash,
            AccountSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
