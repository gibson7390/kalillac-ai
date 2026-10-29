"""Contained email-verification service. No chat, Stripe, entitlement, usage,
session, password or recovery access.

Request work runs only after its public response. The gateway is
code-injected; raw tokens/links are local variables used only for delivery,
never returned, stored or logged.

Delivery happens inside the issuance transaction, while the account row is
locked: a failed send rolls the whole issuance back, so no failed-delivery
credential is ever committed, and a commit failure after delivery leaves the
delivered credential unusable. A future real gateway must therefore bound
its delivery time before activation.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import text

from kalillac_db.repositories import email_verification as repository
from kalillac_db.repositories.recovery import lock_user

# The same 32-byte, canonical unpadded URL-safe credential format as recovery.
from .recovery_tokens import (
    hash_recovery_token as hash_verification_token,
    new_recovery_token as new_verification_token,
    valid_recovery_token as valid_verification_token,
)

LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "30s"
TIMEOUT_STATEMENTS = (
    f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'",
    f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'",
)

__all__ = [
    "VerificationService", "VerificationUnavailable", "apply_transaction_limits",
    "hash_verification_token", "new_verification_token", "valid_verification_token",
]


class VerificationUnavailable(Exception):
    def __init__(self):
        super().__init__("VERIFICATION_UNAVAILABLE")


def utc_now():
    return datetime.now(timezone.utc)


def apply_transaction_limits(session):
    """Fixed, transaction-local bounds applied before any account read or lock."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        for statement in TIMEOUT_STATEMENTS:
            session.execute(text(statement))
    elif dialect != "sqlite":
        # SQLite is the explicit test-only exception; anything else fails closed.
        raise VerificationUnavailable()


class VerificationService:
    def __init__(self, session_factory, gateway, config, clock=utc_now):
        self._session_factory = session_factory
        self._gateway = gateway
        self._config = config
        self._clock = clock

    @contextmanager
    def transaction(self):
        factory = self._session_factory()
        if factory is None:
            raise VerificationUnavailable()
        with factory() as session, session.begin():
            apply_transaction_limits(session)
            yield session

    def request_verification(self, user_id):
        """Commit the credential only if delivery succeeded inside the transaction."""
        token = link = None
        delivery_failed = False
        try:
            with self.transaction() as session:
                user = lock_user(session, user_id)
                # Reread under the lock: nothing for inactive or verified accounts.
                if user is None or not user.is_active or user.email_verified_at is not None:
                    return
                token = new_verification_token()
                repository.issue_token(session, user, hash_verification_token(token), self._clock())
                link = self._config.verification_url + "#token=" + token
                try:
                    self._gateway.send_email_verification(user.email, link)
                except Exception:
                    delivery_failed = True
                    raise
                finally:
                    token = link = None
        except Exception:
            print(
                "ACCOUNT_EMAIL_VERIFICATION_DELIVERY_FAILED" if delivery_failed
                else "ACCOUNT_EMAIL_VERIFICATION_REQUEST_FAILED"
            )
        finally:
            token = link = None

    def confirm_verification(self, token):
        with self.transaction() as session:
            repository.confirm_token(session, hash_verification_token(token), self._clock)
