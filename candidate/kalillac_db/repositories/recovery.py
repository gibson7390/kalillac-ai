"""Caller-owned recovery transactions; account locking precedes every mutation."""
from datetime import timedelta

from sqlalchemy import select, update

from ..models import AccountRecoveryToken, User
from .accounts import revoke_all_account_sessions
from kalillac_accounts.recovery_tokens import TOKEN_LIFETIME_SECONDS

PURPOSE = "password_reset"


class InvalidRecoveryToken(Exception):
    def __init__(self):
        super().__init__("INVALID_OR_EXPIRED_TOKEN")


def lock_user(session, user_id):
    # Same users row locked by billing; no billing/entitlement/usage data is read.
    return session.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )


def invalidate_unused(session, user_id, now):
    session.execute(
        update(AccountRecoveryToken)
        .where(AccountRecoveryToken.user_id == user_id,
               AccountRecoveryToken.purpose == PURPOSE,
               AccountRecoveryToken.consumed_at.is_(None))
        .values(consumed_at=now)
    )


def issue_token(session, user_id, token_hash, now):
    """Caller already holds the account row lock."""
    invalidate_unused(session, user_id, now)
    row = AccountRecoveryToken(
        user_id=user_id, purpose=PURPOSE, token_hash=token_hash,
        created_at=now, expires_at=now + timedelta(seconds=TOKEN_LIFETIME_SECONDS),
    )
    session.add(row)
    session.flush()
    return row.id


def confirm_token(session, token_hash, password_hash, clock):
    user_id = session.scalar(
        select(AccountRecoveryToken.user_id).where(AccountRecoveryToken.token_hash == token_hash)
    )
    if user_id is None:
        raise InvalidRecoveryToken()
    user = lock_user(session, user_id)
    if user is None or not user.is_active:
        raise InvalidRecoveryToken()
    # Compute expiry time AFTER waiting for the account lock.
    now = clock()
    consumed = session.scalar(
        update(AccountRecoveryToken)
        .where(
            AccountRecoveryToken.token_hash == token_hash,
            AccountRecoveryToken.user_id == user_id,
            AccountRecoveryToken.purpose == PURPOSE,
            AccountRecoveryToken.consumed_at.is_(None),
            AccountRecoveryToken.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(AccountRecoveryToken.id)
    )
    if consumed is None:
        raise InvalidRecoveryToken()
    # Preserve all other identity fields, including updated_at. No account is recreated.
    session.execute(
        update(User).where(User.id == user_id)
        .values(password_hash=password_hash, updated_at=User.updated_at)
    )
    invalidate_unused(session, user_id, now)
    revoke_all_account_sessions(session, user_id, now)
    return user.email
