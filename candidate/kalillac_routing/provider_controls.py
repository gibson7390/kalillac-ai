"""Default-off service capacity and temporary-session abuse controls.

Counters are conservative reserved/potential attempts, NOT invoiced usage.
No provider/account/visitor-linked log, payload, tracking ID or background job.

With Financial Safeguards configured (ProviderControls.finance), each attempt
also reserves its worst-case micro-USD in the finance ledger in the SAME
transaction as its attempt-pool reservation, so both commit before dispatch
is allowed; a dispatched attempt is settled afterwards, a never-dispatched one
released. Without it every path below is exactly the attempt ceiling alone.

A chat request is also bounded in dollars: the reservations of its provider
attempts (model calls, tool-loop turns, searches, retries) are counted on its
ControlRequest, and an attempt that would take the request above its scope's
request ceiling is refused before anything is reserved or dispatched.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
import threading
import time
import uuid
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

class RequestCostCeiling(CeilingStop):
    """This request reached its own dollar authorization: one of Kalillac's
    per-request processing limits, not a capacity or provider failure."""
    status_code = 422
    code = "processing_limit_reached"

class AccountAttemptLimitReached(CeilingStop):
    """This account's requests started today's allowed number of provider attempts (successful or not)."""
    status_code = 429
    code = "account_processing_limit_reached"

@dataclass
class Lease:
    provider: str
    period_start: int
    anonymous: bool
    dispatch_deadline: float
    finance: object = None  # finance_ledger.FinanceLease when Financial Safeguards is on
    account: tuple | None = None  # (account id, UTC date) the attempt was counted on, for a refund
    dispatched: bool = False
    released: bool = False
    lock: object = field(default_factory=threading.Lock, repr=False)


class ProviderControls:
    def __init__(self, config, session_factory=None, clock=None, finance=None):
        self.config, self._factory, self._clock = config, session_factory, clock
        self.finance = finance  # kalillac_finance.config.FinancialConfig or None
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

    def reserve(self, provider, anonymous, charge=None, account=None):
        """Admit one attempt. account: (account id, paid) for a signed-in account's request; with
        account limits configured it is required for every request that is not anonymous, and the
        attempt is counted on that account's UTC day in the same transaction as the pool."""
        from kalillac_db.repositories.provider_usage import reserve, reserve_account_attempt
        if provider not in PROVIDERS or type(anonymous) is not bool:
            raise CeilingUnavailable()
        if (self.finance is None) != (charge is None):
            raise CeilingUnavailable()  # Never one ledger without the other.
        accounts = getattr(self.config, "accounts", None)
        if accounts is not None and not anonymous:
            if (type(account) is not tuple or len(account) != 2 or not isinstance(account[0], uuid.UUID)
                    or type(account[1]) is not bool):
                raise CeilingUnavailable()  # A signed-in request with no account: never unbounded.
        limit = self.config.providers[provider]
        def operation(session):
            checkpoint = {}
            def clock():
                checkpoint["monotonic"] = time.monotonic()
                return self._now(session)
            start, now = reserve(session, provider, limit, clock, anonymous)
            counted = None
            if accounts is not None and not anonymous:
                day = now.date()
                reserve_account_attempt(session, account[0], day, accounts.paid if account[1] else accounts.free)
                counted = (account[0], day)
            finance = None
            if charge is not None:
                from kalillac_db.repositories import finance_ledger
                if charge.provider != provider:
                    raise CeilingUnavailable()
                finance = finance_ledger.reserve(session, charge, now, self.finance.limits)
            end = checkpoint["monotonic"] + start + limit.period_seconds - now.timestamp()
            return Lease(provider, start, anonymous, end, finance, counted)
        return self._transaction(operation)

    def release(self, lease):
        """Refund a never-dispatched attempt. True only when the refund
        committed (with Financial Safeguards on: the reservation returned)."""
        from kalillac_db.repositories.provider_usage import release, release_account_attempt
        with lease.lock:
            if lease.dispatched or lease.released: return False
            lease.released = True  # Exactly one refund attempt, no hidden retry.
        def operation(session):
            release(session, lease)
            if lease.account is not None:
                release_account_attempt(session, *lease.account)
            if lease.finance is not None:
                from kalillac_db.repositories import finance_ledger
                return finance_ledger.release(session, lease.finance, self._now(session))
            return True
        # Cleanup must still run after cancellation; no request-budget admission.
        try:
            return self._transaction(operation, check_budget=False) is True
        except Exception as error:
            # Unknown cleanup remains charged; never alter the chat contract.
            print("WARN: PROVIDER_CEILING_REFUND_UNCONFIRMED", type(error).__name__)
            return False

    def settle(self, lease, value, returned):
        """Settle a DISPATCHED attempt's financial reservation exactly once.

        A failure leaves the reservation outstanding, so it stays charged.
        Returns the charged µUSD when the settlement committed, else None.
        """
        finance = lease.finance
        if finance is None:
            return None
        with lease.lock:
            if not lease.dispatched or finance.resolved: return None
            finance.resolved = True
        try:
            from kalillac_finance.runtime import settlement_for
            from kalillac_db.repositories import finance_ledger
            settlement = settlement_for(self.finance.prices, finance.charge, value, returned)
            settled = self._transaction(lambda session: finance_ledger.settle(
                session, finance, settlement, self._now(session), self.finance.limits), check_budget=False)
            return settlement.charged if settled is True else None
        except Exception as error:
            print("WARN: FINANCE_SETTLEMENT_UNCONFIRMED", type(error).__name__)
            return None


class ControlRequest:
    def __init__(self, service):
        from kalillac_finance.scopes import TrafficClass
        self.service, self.anonymous = service, True
        self.traffic_class = TrafficClass.ANONYMOUS
        self.account_id = None  # The signed-in account this request runs for (its attempt limit).
        self.lock = threading.Lock()
        self.closed = self.started = self.finished = False
        self.session = self.session_lock = None
        self.authorized_microusd = 0  # Financial Safeguards: this request's own exposure.

    def ensure_open(self):
        budget = current_budget()
        if budget is not None: budget.ensure_open()
        with self.lock:
            if self.closed: raise CeilingUnavailable()

    def authorize_cost(self, amount, ceiling):
        """Count one attempt's reservation toward this request, or refuse it
        before anything is reserved when the request ceiling would be passed."""
        with self.lock:
            if self.closed: raise CeilingUnavailable()
            admitted = (type(amount) is int and type(ceiling) is int and amount >= 0
                        and self.authorized_microusd + amount <= ceiling)
            if admitted: self.authorized_microusd += amount
        if not admitted:
            print("WARN: FINANCE_REQUEST_CEILING_REFUSED", "traffic=" + self.traffic_class.value)
            raise RequestCostCeiling()

    def adjust_cost(self, delta):
        """A confirmed refund (negative) or a confirmed settlement difference."""
        with self.lock:
            self.authorized_microusd = max(0, self.authorized_microusd + delta)

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


def current_request():
    """This request's control state (the binding a PaidEntitlement is issued for), or None."""
    return _REQUEST.get()


def account_status(authenticated, paid_entitlement=None, account_id=None):
    """The request's only traffic classification (see kalillac_finance.traffic), and the account whose
    daily provider-attempt limit its attempts count against."""
    from kalillac_finance.traffic import resolve_traffic_class
    state = _REQUEST.get()
    if state is not None:
        state.anonymous = not authenticated
        state.account_id = _account_uuid(account_id) if authenticated else None
        state.traffic_class = resolve_traffic_class(
            authenticated=authenticated is True, request=state, paid_entitlement=paid_entitlement)



def _account_uuid(value):
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _attempt_account(request):
    """(account id, paid) for a signed-in request, or None (anonymous, or no account known)."""
    from kalillac_finance.scopes import TrafficClass
    if request.anonymous or request.account_id is None:
        return None
    return request.account_id, request.traffic_class is TrafficClass.PAID_ACCOUNT


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
        finance = getattr(service, "finance", None)
        account = _attempt_account(request)
        if finance is None:
            lease = service.reserve(selected, request.anonymous, account=account)
        else:
            from kalillac_finance.runtime import charge_for
            try:
                charge = charge_for(finance.prices, selected, args, request.traffic_class)
                ceiling = finance.limits.max_request(charge.scope)
            except Exception:
                print("WARN: FINANCE_RESERVATION_UNBOUNDED", selected)
                raise CeilingUnavailable() from None
            # The request's own dollar ceiling, before either ledger is touched.
            request.authorize_cost(charge.reserved, ceiling)
            try:
                lease = service.reserve(selected, request.anonymous, charge, account=account)
            except BaseException:
                request.adjust_cost(-charge.reserved)  # Nothing was reserved.
                raise
        token = _LEASE.set(lease)
        returned, value = False, None
        try:
            request.ensure_open()
            value = function(*args, **kwargs)
            returned = True
            return value
        finally:
            # Claiming release fences late transport callbacks before refund.
            released = service.release(lease)
            if finance is not None:
                charged = service.settle(lease, value, returned)
                # Only a CONFIRMED refund or settlement changes what the request
                # has authorized; an unconfirmed one stays counted in full.
                if released is True:
                    request.adjust_cost(-charge.reserved)
                elif type(charged) is int:
                    request.adjust_cost(charged - charge.reserved)
            _LEASE.reset(token)
    return guarded


def financial_openai(function):
    """With Financial Safeguards on, pin the OpenAI service tier the prices are
    for (Standard: "default"); otherwise the payload passes through untouched."""
    @wraps(function)
    def pinned(payload, *args, **kwargs):
        if getattr(SERVICE, "finance", None) is not None and type(payload) is dict:
            payload = {**payload, "service_tier": "default"}
        return function(payload, *args, **kwargs)
    return pinned


def dispatch():
    if SERVICE is None: return
    request, lease = _REQUEST.get(), _LEASE.get()
    if request is None or lease is None: raise CeilingUnavailable()
    request.ensure_open()
    if getattr(SERVICE, "finance", None) is not None and lease.finance is None:
        raise CeilingUnavailable()  # Both leases, or no network operation.
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
