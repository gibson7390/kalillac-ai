"""Data access for account identity and sign-in sessions.

Functions take a caller-owned SQLAlchemy Session and never commit; the
caller's transaction scope decides.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import select, update, text
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


def revoke_all_account_sessions(session: Session, user_id: uuid.UUID, now: datetime) -> None:
    """Revoke every unrevoked device for one account, including expired sessions."""
    session.execute(
        update(AccountSession)
        .where(AccountSession.user_id == user_id, AccountSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )


class AccountAuthorizationUnavailable(RuntimeError):
    """The fresh PostgreSQL authorization contract could not be established."""


def original_session_binding(session: Session, token_hash: str):
    # Scalar columns avoid both stale ORM identity-map state and deferred loads.
    with session.no_autoflush:
        return session.execute(
            select(AccountSession.user_id, AccountSession.id, AccountSession.token_hash)
            .where(AccountSession.token_hash == token_hash)
        ).one_or_none()


def locked_original_account(session: Session, user_id, session_id, token_hash):
    """Caller owns a clean READ COMMITTED transaction; no writes or commits."""
    if session.get_bind().dialect.name != "postgresql" or session.new or session.dirty or session.deleted:
        raise AccountAuthorizationUnavailable()
    with session.no_autoflush:
        if getattr(session.connection().connection.driver_connection, "autocommit", True):
            raise AccountAuthorizationUnavailable()
        if session.scalar(text("SELECT current_setting('transaction_isolation')")) != "read committed":
            raise AccountAuthorizationUnavailable()
        locked = session.scalar(select(User.id).where(User.id == user_id).with_for_update())
        # Separate statement AFTER acquiring the account lock: neither now()
        # nor a timestamp evaluated before a blocking SELECT is fresh enough.
        checked_at = session.scalar(text("SELECT clock_timestamp()"))
        if locked is None:
            return None, checked_at
        row = session.execute(
            select(
                User.id, User.email, User.created_at, User.is_active, User.email_verified_at,
                User.quarantined_at, User.sessions_valid_after,
                AccountSession.created_at.label("session_created_at"),
                AccountSession.expires_at, AccountSession.revoked_at,
            ).join(AccountSession, AccountSession.user_id == User.id)
            .where(User.id == user_id, AccountSession.user_id == user_id,
                   AccountSession.id == session_id, AccountSession.token_hash == token_hash)
        ).one_or_none()
        return row, checked_at
