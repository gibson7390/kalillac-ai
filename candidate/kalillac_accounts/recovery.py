"""Contained recovery service. No chat, Stripe, entitlement or usage access.

Request work runs only after its public response. Gateways are code-injected;
raw tokens/links are local variables used only for delivery, never returned.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import select, text

from kalillac_db.models import User
from kalillac_db.repositories import recovery as repository
from .recovery_tokens import hash_recovery_token, new_recovery_token

LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "30s"
TIMEOUT_STATEMENTS = (
    f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'",
    f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'",
)


class RecoveryUnavailable(Exception):
    def __init__(self):
        super().__init__("RECOVERY_UNAVAILABLE")


def utc_now():
    return datetime.now(timezone.utc)


def apply_transaction_limits(session):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        for statement in TIMEOUT_STATEMENTS:
            session.execute(text(statement))
    elif dialect != "sqlite":
        # SQLite is an explicit test-only exception, with its own bounded busy timeout.
        raise RecoveryUnavailable()


class RecoveryService:
    def __init__(self, session_factory, gateway, config, clock=utc_now):
        self._session_factory = session_factory
        self._gateway = gateway
        self._config = config
        self._clock = clock

    @contextmanager
    def transaction(self):
        factory = self._session_factory()
        if factory is None:
            raise RecoveryUnavailable()
        with factory() as session, session.begin():
            apply_transaction_limits(session)
            yield session

    def request_reset(self, email):
        """Publish the credential only if delivery and the issuance transaction succeed."""
        token = link = None
        delivery_failed = False
        try:
            with self.transaction() as session:
                user_id = session.scalar(select(User.id).where(User.email == email))
                user = repository.lock_user(session, user_id) if user_id is not None else None
                if user is None or not user.is_active or user.email != email:
                    return
                token = new_recovery_token()
                repository.issue_token(session, user.id, hash_recovery_token(token), self._clock())
                link = self._config.recovery_url + "#token=" + token
                try:
                    # No credential is committed before delivery. A failed send
                    # rolls issuance back even if a later cleanup query could not run.
                    # A future gateway must bound its own delivery call before activation.
                    self._gateway.send_password_reset(email, link)
                except Exception:
                    delivery_failed = True
                    raise
                finally:
                    token = link = None
        except Exception:
            print("ACCOUNT_RECOVERY_DELIVERY_FAILED" if delivery_failed else "ACCOUNT_RECOVERY_REQUEST_FAILED")
        finally:
            token = link = None

    def confirm_reset(self, token, password_hash):
        with self.transaction() as session:
            email = repository.confirm_token(
                session, hash_recovery_token(token), password_hash, self._clock,
            )
        # Commit already succeeded. Notification failure cannot undo the reset.
        try:
            self._gateway.send_password_changed(email)
        except Exception:
            print("ACCOUNT_RECOVERY_NOTIFICATION_FAILED")
