"""Default-off, read-only V1 original-session authorization for two GET routes.

Off mode loads no database dependencies or schema. This does not activate V1
mutations, billing, chat revalidation or a production feature.
"""
from __future__ import annotations

import asyncio
import os
import socket
import stat
import threading

FLAG = "KALILLAC_V1_ACCOUNT_SECURITY_ENABLED"
# The authorization-failure diagnostic is one fixed ASCII line offered to this
# descriptor (stderr). It never passes through logging: handlers, filters and
# formatters there belong to the application, and logging reports their
# failures to stderr with tracebacks this path does not control.
DIAGNOSTIC_FD = 2
# Reopening a pipe, FIFO or terminal through procfs yields a new open file
# description, whose O_NONBLOCK is not shared with DIAGNOSTIC_FD (dup() would be).
_REOPEN = "/proc/self/fd/{}"
_DIAGNOSTIC = "V1 account authorization unavailable: {} sqlstate={}\n"


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


def _failure(exc):
    """Exception class and SQLSTATE only: never messages, SQL, parameters or values.

    Never raises. Any attribute of a failing driver object may be a throwing
    property, so each read is guarded and only exact, validated str values
    (no subclass with overridden methods) are kept.
    """
    try:
        name = type(exc).__name__
        name = name[:64] if type(name) is str and name.isascii() and name.isidentifier() else "-"
    except Exception:
        name = "-"
    try:
        orig = getattr(exc, "orig", None)
        state = getattr(exc if orig is None else orig, "sqlstate", None)
        if not (type(state) is str and len(state) == 5 and state.isascii() and state.isalnum()):
            state = "-"
    except Exception:
        state = "-"
    return name, state


def _emit(line):
    """One attempt that never waits for a reader: written now or dropped.

    The shared descriptor's flags are never changed and nothing is retained
    (no thread, queue or open descriptor), so a stalled reader cannot stall
    the request or accumulate anything. The line is far below PIPE_BUF, so a
    nonblocking pipe write is all or nothing.
    """
    mode = os.fstat(DIAGNOSTIC_FD).st_mode
    if stat.S_ISREG(mode):
        # A file has no reader to wait for.
        os.write(DIAGNOSTIC_FD, line)
    elif stat.S_ISSOCK(mode):
        # journald's stream under systemd: a per-call nonblocking send. Wrapping
        # without dup() changes no flag unless a default timeout is set, which
        # would make the wrapper switch the shared socket to nonblocking.
        if socket.getdefaulttimeout() is not None:
            return
        wrapper = socket.socket(fileno=DIAGNOSTIC_FD)
        try:
            wrapper.send(line, socket.MSG_DONTWAIT | socket.MSG_NOSIGNAL)
        finally:
            wrapper.detach()
    else:
        fd = os.open(_REOPEN.format(DIAGNOSTIC_FD), os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)


def _report(failure):
    """Operational signal only: the two validated tokens in a fixed format.
    A failing sink or write is suppressed and never replaces the 503 the
    caller is about to raise."""
    try:
        _emit(_DIAGNOSTIC.format(*failure).encode("ascii"))
    except Exception:
        pass


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
        except Exception as exc:
            failure = None if stopped.is_set() else _failure(exc)
        # Reported outside the handler, so nothing raised while reporting can
        # chain the original exception (message, SQL, parameters). The
        # response stays 503 account_unavailable.
        if failure is not None:
            _report(failure)
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


def _enabled(env):
    value = env.get(FLAG, "").strip().lower()
    if value in {"", "0", "false", "no", "off"}:
        return False
    if value not in {"1", "true", "yes", "on"}:
        raise V1SecurityConfigError(FLAG + " must be a boolean.")
    if any(env.get(n, "").strip().lower() not in {"1", "true", "yes", "on"}
           for n in ("KALILLAC_ACCOUNTS_ENABLED", "KALILLAC_DB_ENABLED")):
        raise V1SecurityConfigError(FLAG + " requires accounts and database support.")
    return True


def load_v1_startup(session_factory=None, environ=None):
    """Application startup: V1 also requires the qualified private-authority
    catalog. Legacy and merely prepared (0012) schemas refuse; off mode returns
    None before any database or schema import."""
    env = os.environ if environ is None else environ
    if not _enabled(env):
        return None
    if session_factory is None:
        from kalillac_db.engine import get_session_factory
        session_factory = get_session_factory
    from .v1_schema import V1SchemaRefused, require_qualified_authority
    try:
        require_qualified_authority(session_factory)
    except V1SchemaRefused as refused:
        raise V1SecurityConfigError(
            FLAG + " requires a qualified V1 private-authority catalog (" + refused.reason + ").") from None
    return load_v1_security(session_factory, env)


def load_v1_security(session_factory=None, environ=None):
    env = os.environ if environ is None else environ
    if not _enabled(env):
        return None
    if session_factory is None:
        from kalillac_db.engine import get_session_factory
        session_factory = get_session_factory
    security = V1AccountSecurity(session_factory)
    try:
        security.validate()
    except Exception:
        raise V1SecurityConfigError(FLAG + " requires the PostgreSQL 16+ identity schema and fresh transactions.") from None
    return security
