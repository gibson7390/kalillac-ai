"""Default-off, read-only V1 original-session authorization for two GET routes.

Off mode loads no database dependencies or schema. This does not activate V1
mutations, billing, chat revalidation or a production feature.
"""
from __future__ import annotations

import asyncio
import os
import threading

FLAG = "KALILLAC_V1_ACCOUNT_SECURITY_ENABLED"


class V1SecurityConfigError(RuntimeError):
    pass


class V1SecurityUnavailable(RuntimeError):
    pass


def _prepare(session):
    from sqlalchemy import text
    if session.get_bind().dialect.name != "postgresql" or session.new or session.dirty or session.deleted:
        raise V1SecurityUnavailable()
    # SQLAlchemy's logical transaction flag is also true in DBAPI autocommit
    # mode; in that mode FOR UPDATE would release its lock after the statement.
    connection = session.connection().connection.driver_connection
    if getattr(connection, "autocommit", True):
        raise V1SecurityUnavailable()
    session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
    session.execute(text("SET LOCAL lock_timeout = '3s'"))
    session.execute(text("SET LOCAL statement_timeout = '3s'"))
    if session.scalar(text("SELECT current_setting('transaction_isolation')")) != "read committed":
        raise V1SecurityUnavailable()
    if session.scalar(text("SELECT current_setting('transaction_read_only')")) != "off":
        raise V1SecurityUnavailable()


def _schema(session):
    from sqlalchemy import text
    required = {
        "users": {"id": "uuid", "email": "varchar", "created_at": "timestamptz",
                  "is_active": "bool", "email_verified_at": "timestamptz",
                  "registration_intent_id": "uuid", "quarantined_at": "timestamptz",
                  "sessions_valid_after": "timestamptz"},
        "account_sessions": {"id": "uuid", "user_id": "uuid", "token_hash": "varchar",
                             "created_at": "timestamptz", "expires_at": "timestamptz",
                             "revoked_at": "timestamptz"},
    }
    rows = session.execute(text(
        "SELECT table_name, column_name, udt_name FROM information_schema.columns "
        "WHERE table_schema = 'kalillac' AND table_name IN ('users', 'account_sessions')"
    )).all()
    actual = {(t, c): kind for t, c, kind in rows}
    if any(actual.get((t, c)) != kind for t, cols in required.items() for c, kind in cols.items()):
        raise V1SecurityUnavailable()
    if int(session.scalar(text("SELECT current_setting('server_version_num')"))) < 160000:
        raise V1SecurityUnavailable()
    if session.scalar(text("SELECT clock_timestamp()")) is None:
        raise V1SecurityUnavailable()
    # Parse/permission/transaction check without taking a user-row lock.
    session.execute(text("SELECT id FROM kalillac.users WHERE false FOR UPDATE"))


class V1AccountSecurity:
    def __init__(self, session_factory):
        self._factory = session_factory

    def validate(self):
        factory = self._factory()
        if factory is None:
            raise V1SecurityUnavailable()
        with factory() as session:
            if session.in_transaction():
                raise V1SecurityUnavailable()
            with session.begin(), session.no_autoflush:
                _prepare(session)
                _schema(session)

    def read(self, token_hash, work, stopped=None):
        from .auth import RequestRejected, resolve_original_identity, authorize_original_identity
        stopped = stopped if stopped is not None else threading.Event()
        try:
            if stopped.is_set():
                raise V1SecurityUnavailable()
            factory = self._factory()
            if factory is None:
                raise V1SecurityUnavailable()
            with factory() as session:
                if session.in_transaction():
                    raise V1SecurityUnavailable()
                with session.begin(), session.no_autoflush:
                    _prepare(session)
                    if stopped.is_set():
                        raise V1SecurityUnavailable()
                    identity = resolve_original_identity(session, token_hash)
                    if identity is None:
                        raise RequestRejected(401, "not_authenticated")
                    account = authorize_original_identity(session, identity)
                    if stopped.is_set():
                        raise V1SecurityUnavailable()
                    payload = work(session, account)
                    if stopped.is_set() or session.new or session.dirty or session.deleted:
                        raise V1SecurityUnavailable()
                    return payload
        except RequestRejected:
            raise
        except Exception:
            raise V1SecurityUnavailable() from None

    async def read_async(self, token_hash, work):
        import anyio
        from starlette.concurrency import run_in_threadpool
        stopped = threading.Event()
        task = asyncio.create_task(run_in_threadpool(self.read, token_hash, work, stopped))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stopped.set()
            # Cancellation is not evidence the DB worker stopped. Retain it
            # until its bounded transaction has actually rolled back/closed.
            with anyio.CancelScope(shield=True):
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled():
                    task.exception()
            raise


def load_v1_security(session_factory=None, environ=None):
    env = os.environ if environ is None else environ
    value = env.get(FLAG, "").strip().lower()
    if value in {"", "0", "false", "no", "off"}:
        return None
    if value not in {"1", "true", "yes", "on"}:
        raise V1SecurityConfigError(FLAG + " must be a boolean.")
    if any(env.get(n, "").strip().lower() not in {"1", "true", "yes", "on"}
           for n in ("KALILLAC_ACCOUNTS_ENABLED", "KALILLAC_DB_ENABLED")):
        raise V1SecurityConfigError(FLAG + " requires accounts and database support.")
    if session_factory is None:
        from kalillac_db.engine import get_session_factory
        session_factory = get_session_factory
    security = V1AccountSecurity(session_factory)
    try:
        security.validate()
    except Exception:
        raise V1SecurityConfigError(FLAG + " requires the PostgreSQL 16+ identity schema and fresh transactions.") from None
    return security
