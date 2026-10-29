"""Caller-owned email-verification transactions; the users-row lock precedes every mutation.

Nothing here reads or writes billing, entitlement, usage, sessions,
passwords or recovery tokens.
"""
from datetime import timedelta
import hashlib

from sqlalchemy import select, update

from ..models import AccountEmailVerificationToken, User
from .recovery import lock_user

TOKEN_LIFETIME = timedelta(hours=24)


class InvalidVerificationToken(Exception):
    def __init__(self):
        super().__init__("INVALID_OR_EXPIRED_TOKEN")


def email_digest(normalized_email):
    return hashlib.sha256(normalized_email.encode("utf-8")).hexdigest()


def invalidate_unused(session, user_id, now):
    session.execute(
        update(AccountEmailVerificationToken)
        .where(AccountEmailVerificationToken.user_id == user_id,
               AccountEmailVerificationToken.consumed_at.is_(None))
        .values(consumed_at=now)
    )


def issue_token(session, user, token_hash, now):
    """Caller already holds the account row lock and has reread the user."""
    invalidate_unused(session, user.id, now)
    session.add(AccountEmailVerificationToken(
        user_id=user.id, email_hash=email_digest(user.email), token_hash=token_hash,
        created_at=now, expires_at=now + TOKEN_LIFETIME,
    ))
    session.flush()


def confirm_token(session, token_hash, clock):
    user_id = session.scalar(
        select(AccountEmailVerificationToken.user_id)
        .where(AccountEmailVerificationToken.token_hash == token_hash)
    )
    if user_id is None:
        raise InvalidVerificationToken()
    user = lock_user(session, user_id)
    if user is None or not user.is_active:
        raise InvalidVerificationToken()
    # Read the clock only after any lock wait.
    now = clock()
    consumed = session.scalar(
        update(AccountEmailVerificationToken)
        .where(
            AccountEmailVerificationToken.token_hash == token_hash,
            AccountEmailVerificationToken.user_id == user_id,
            # Bound to the address the token was sent to.
            AccountEmailVerificationToken.email_hash == email_digest(user.email),
            AccountEmailVerificationToken.consumed_at.is_(None),
            AccountEmailVerificationToken.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(AccountEmailVerificationToken.id)
    )
    if consumed is None:
        raise InvalidVerificationToken()
    # Only this user's verification time changes; updated_at and every other
    # identity field are preserved.
    session.execute(
        update(User).where(User.id == user_id, User.email_verified_at.is_(None))
        .values(email_verified_at=now, updated_at=User.updated_at)
    )
    invalidate_unused(session, user_id, now)
