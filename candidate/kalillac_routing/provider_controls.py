"""Default-off service capacity and temporary-session abuse controls.

Counters are conservative reserved/potential attempts, NOT invoiced usage.
No provider/account/visitor-linked log, payload, tracking ID or background job.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
import threading
import time
from .bounded_transport import TransportError
from .request_budget import RequestBudgetError, current_budget
from .provider_limits import PROVIDERS

SERVICE = None
_REQUEST = ContextVar("provider_control_request", default=None)
_LEASE = ContextVar("provider_control_attempt", default=None)

class CeilingStop(TransportError, RequestBudgetError):
    def __init__(self):
        Exception.__init__(self, "Provider capacity unavailable.")

    status_code = 503
    code = "service_unavailable"

class CeilingUnavailable(CeilingStop):
    pass

class CeilingBusy(CeilingStop):
    status_code = 429
    code = "busy"

@dataclass
class Lease:
    provider: str
    period_start: int
    anonymous: bool
    dispatch_deadline: float
    dispatched: bool = False
    released: bool = False
    lock: object = field(default_factory=threading.Lock, repr=False)


class ProviderControls:
    def __init__(self, config, session_factory=None, clock=None):
        self.config, self._factory, self._clock = config, session_factory, clock
        if set(config.providers) != set(PROVIDERS):
            raise ValueError("All provider ceilings must be explicit.")
        for limit in config.providers.values():
            if any(type(v) is not int for v in (limit.total, limit.anonymous, limit.period_seconds)) or not 0 <= limit.anonymous <= limit.total or limit.period_seconds <= 0:
                raise ValueError("Invalid provider ceiling.")

    def _now(self, session):
        from sqlalchemy import func, select
        if self._clock is not None:
            value = self._clock()
        elif session.get_bind().dialect.name == "postgresql":
            value = session.scalar(select(func.clock_timestamp()))
        else:
            value = datetime.now(timezone.utc)
        if value.tzinfo is None:
            raise CeilingUnavailable()
        return value.astimezone(timezone.utc)

    def _transaction(self, operation, check_budget=True):
        from sqlalchemy import text
        from kalillac_db.engine import get_session_factory
        budget = current_budget() if check_budget else None
        try:
            if budget is not None: budget.ensure_open()
            factory = (self._factory or get_session_factory)()
            if factory is None: raise CeilingUnavailable()
            with factory() as session, session.begin():
                dialect = session.get_bind().dialect.name
                if dialect == "postgresql":
                    session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                    session.execute(text("SET LOCAL lock_timeout = '3s'"))
                    session.execute(text("SET LOCAL statement_timeout = '3s'"))
                elif dialect != "sqlite":
                    raise CeilingUnavailable()
                value = operation(session)
                if budget is not None: budget.ensure_open()
                return value
        except (CeilingStop, RequestBudgetError):
            raise
        except Exception as error:
            print("WARN: PROVIDER_CEILING_DATABASE_FAILED", type(error).__name__)
            raise CeilingUnavailable() from None

    def reserve(self, provider, anonymous):
        from kalillac_db.repositories.provider_usage import reserve
        if provider not in PROVIDERS or type(anonymous) is not bool:
            raise CeilingUnavailable()
        limit = self.config.providers[provider]
        def operation(session):
            checkpoint = {}
            def clock():
                checkpoint["monotonic"] = time.monotonic()
                return self._now(session)
            start, now = reserve(session, provider, limit, clock, anonymous)
            end = checkpoint["monotonic"] + start + limit.period_seconds - now.timestamp()
            return Lease(provider, start, anonymous, end)
        return self._transaction(operation)

    def release(self, lease):
        from kalillac_db.repositories.provider_usage import release
        with lease.lock:
            if lease.dispatched or lease.released: return
            lease.released = True  # Exactly one refund attempt, no hidden retry.
        # Cleanup must still run after cancellation; no request-budget admission.
        try:
            self._transaction(lambda session: release(session, lease), check_budget=False)
        except Exception as error:
            # Unknown cleanup remains charged; never alter the chat contract.
            print("WARN: PROVIDER_CEILING_REFUND_UNCONFIRMED", type(error).__name__)


class ControlRequest:
    def __init__(self, service):
        self.service, self.anonymous = service, True
        self.lock = threading.Lock()
        self.closed = self.started = self.finished = False
        self.session = self.session_lock = None

    def ensure_open(self):
        budget = current_budget()
        if budget is not None: budget.ensure_open()
        with self.lock:
            if self.closed: raise CeilingUnavailable()

    def admit_session(self, state, lock):
        limit = self.service.config.abuse
        if not self.anonymous or limit is None: return
        with self.lock, lock:
            if self.closed or self.session is not None: raise CeilingUnavailable()
            now = time.monotonic()
            times = state.get("anonymous_burst", [])
            times = [v for v in times if now - v < limit.window_seconds]
            active = state.get("anonymous_active", 0)
            if len(times) >= limit.burst or active >= limit.concurrent:
                raise CeilingBusy()
            state["anonymous_burst"] = times + [now]  # Bounded by configured burst.
            state["anonymous_active"] = active + 1
            self.session, self.session_lock = state, lock

    def _release_session(self):
        if self.session is not None:
            with self.session_lock:
                self.session["anonymous_active"] -= 1
            self.session = self.session_lock = None

    def start(self):
        with self.lock:
            if self.closed: raise CeilingUnavailable()
            self.started = True

    def finish(self):
        with self.lock:
            self.finished = True
            self._release_session()

    def close(self):
        with self.lock:
            self.closed = True
            if not self.started or self.finished: self._release_session()


def begin_request():
    if SERVICE is None: return None, None
    state = ControlRequest(SERVICE)
    return state, _REQUEST.set(state)


def end_request(state, token):
    if state is not None:
        state.close()
        _REQUEST.reset(token)


def account_status(authenticated):
    state = _REQUEST.get()
    if state is not None: state.anonymous = not authenticated


def session_admit(state, lock):
    request = _REQUEST.get()
    if request is not None: request.admit_session(state, lock)


def guard_chat(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        request = _REQUEST.get()
        if request is None: return function(*args, **kwargs)
        request.start()
        try: return function(*args, **kwargs)
        finally: request.finish()
    return guarded


def protect(function, provider=None):
    @wraps(function)
    def guarded(*args, **kwargs):
        service = SERVICE
        if service is None: return function(*args, **kwargs)
        budget, request = current_budget(), _REQUEST.get()
        if budget is None or request is None: raise CeilingUnavailable()
        request.ensure_open()
        selected = provider or args[0]
        # Budgeted Tavily is protected by its shared search/extract seam.
        if provider is None and selected == "tavily": return function(*args, **kwargs)
        lease = service.reserve(selected, request.anonymous)
        token = _LEASE.set(lease)
        try:
            request.ensure_open()
            return function(*args, **kwargs)
        finally:
            # Claiming release fences late transport callbacks before refund.
            service.release(lease)
            _LEASE.reset(token)
    return guarded


def dispatch():
    if SERVICE is None: return
    request, lease = _REQUEST.get(), _LEASE.get()
    if request is None or lease is None: raise CeilingUnavailable()
    request.ensure_open()
    with lease.lock:
        if lease.released or lease.dispatched or time.monotonic() >= lease.dispatch_deadline:
            raise CeilingBusy()
        lease.dispatched = True  # Immediately before the HTTP client operation.


def guard_lookup(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        if SERVICE is not None and (current_budget() is None or _REQUEST.get() is None):
            raise CeilingUnavailable()  # No hidden SDK attempts under enforcement.
        return function(*args, **kwargs)
    return guarded
