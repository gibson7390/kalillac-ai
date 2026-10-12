"""Request-local daily quotas. No provider calls, transcripts or request IDs."""
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, select, text
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from kalillac_db.engine import get_session_factory
from kalillac_db.repositories.accounts import get_signed_in_user
from kalillac_db.repositories.entitlements import get_entitlement
from kalillac_db.repositories.quota import (
    QuotaError, QuotaBusy, QuotaIdentityInvalid, QuotaUnavailable,
    lock_account, reserve_slot, complete_slot, release_slot, record_sent_slot, matching_slot, utc,
)
from kalillac_routing.request_budget import RequestBudgetError
from .entitlements import effective_entitlement
from .router import load_account_settings
from .tokens import hash_session_token

RESERVATION_TTL = timedelta(minutes=15)


@dataclass(frozen=True)
class Admission:
    user_id: object = None
    reservation: object = None


class UsageQuota:
    def __init__(self, limits, session_factory=get_session_factory, settings=None, clock=None, ttl=RESERVATION_TTL):
        if ttl.total_seconds() <= 0:
            raise ValueError("Reservation lifetime must be positive.")
        self.limits, self._factory = limits, session_factory
        self.settings, self._clock, self.ttl = settings or load_account_settings(), clock, ttl

    def new_request(self):
        return QuotaRequest(self)

    def _now(self, session):
        if self._clock is not None:
            return utc(self._clock())
        if session.get_bind().dialect.name == "postgresql":
            return utc(session.scalar(select(func.clock_timestamp())))
        return datetime.now(timezone.utc)

    def _limit(self, session, user_id, now):
        access = effective_entitlement(get_entitlement(session, user_id), now)
        return self.limits.paid_daily_chats if access.higher_usage_access else self.limits.free_daily_chats

    def _transaction(self, operation):
        try:
            factory = self._factory()
            if factory is None:
                raise QuotaUnavailable()
            with factory() as session, session.begin():
                if session.get_bind().dialect.name == "postgresql":
                    # Fresh snapshots after the account lock are required even
                    # if the database's default isolation is REPEATABLE READ.
                    session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                    # Per-transaction bounds, not changes to server settings.
                    session.execute(text("SET LOCAL lock_timeout = '3s'"))
                    session.execute(text("SET LOCAL statement_timeout = '3s'"))
                elif session.get_bind().dialect.name != "sqlite":
                    raise QuotaUnavailable()
                return operation(session)
        except (QuotaError, RequestBudgetError):
            raise
        except Exception as exc:
            print(f"WARN: USAGE_QUOTA_DATABASE_FAILED {type(exc).__name__}")
            raise QuotaUnavailable() from None

    def reserve(self, token_hash, request):
        if token_hash is None:
            return Admission()  # Genuine anonymous: no DB or entitlement work.
        def operation(session):
            request.ensure_open()
            user = get_signed_in_user(session, token_hash, self._now(session))
            if user is None:
                raise QuotaIdentityInvalid()
            lock_account(session, user.id)
            now = self._now(session)
            limit = self._limit(session, user.id, now)
            ttl = self.ttl
            if request.budget is not None:
                ttl = max(ttl, timedelta(seconds=max(0, request.budget.deadline - time.monotonic()) + 5))
            lease = reserve_slot(session, user.id, now.date(), limit, now, ttl)
            request.ensure_open()
            return Admission(user.id, lease)
        return self._transaction(operation)

    def validate(self, admission, request):
        def operation(session):
            request.ensure_open()
            lock_account(session, admission.user_id)
            row = matching_slot(session, admission.reservation)
            if row is None or row.state != "pending" or utc(row.expires_at) <= self._now(session):
                raise QuotaUnavailable()
            request.ensure_open()
        return self._transaction(operation)

    def complete(self, admission, request):
        def operation(session):
            request.ensure_open()
            lock_account(session, admission.user_id)
            now = self._now(session)
            day = complete_slot(session, admission.reservation, now, self._limit(session, admission.user_id, now))
            request.ensure_open()
            return day
        return self._transaction(operation)

    def record_sent(self, admission):
        def operation(session):
            lock_account(session, admission.user_id)
            return record_sent_slot(session, admission.reservation)
        return self._transaction(operation)

    def release(self, admission):
        def operation(session):
            lock_account(session, admission.user_id)
            release_slot(session, admission.reservation)
        return self._transaction(operation)


class QuotaRequest:
    """Only existing account identity, lease, counts and cancellation state.

    A late database worker owns cleanup when the handler has already stopped.
    Cleanup is idempotent and fenced against reuse; it never invokes providers.
    """
    def __init__(self, service):
        self.service, self.admission = service, None
        self.budget, self.disconnected = None, None
        self.request_chars = 0
        self._lock = threading.Lock()
        self._closed = self._accepted = self._in_work = self._cleanup_claimed = False

    def ensure_open(self):
        if self._closed:
            raise QuotaUnavailable()
        if self.budget is not None:
            self.budget.ensure_open()

    def _run(self, operation):
        with self._lock:
            self._in_work = True
        try:
            return operation()
        finally:
            with self._lock:
                self._in_work = False
                cleanup = self._claim_cleanup()
            if cleanup:
                self.cleanup()

    def _claim_cleanup(self):
        if self._closed and not self._accepted and not self._cleanup_claimed and self.admission is not None and self.admission.reservation is not None:
            self._cleanup_claimed = True
            return True
        return False

    def is_anonymous(self, request):
        return not request.cookies.get(self.service.settings.cookie_name)

    async def resolve_request_account(self, request):
        token = request.cookies.get(self.service.settings.cookie_name)
        if not token:
            self.admission = Admission()
            return None
        digest = hash_session_token(token)
        del token
        def work():
            admission = self.service.reserve(digest, self)
            with self._lock:
                self.admission = admission
            return admission.user_id
        return await run_in_threadpool(self._run, work)

    def before_work(self):
        if self.admission is not None and self.admission.reservation is not None:
            self._run(lambda: self.service.validate(self.admission, self))

    async def complete(self):
        return await run_in_threadpool(self._run, lambda: self.service.complete(self.admission, self))

    async def record_sent(self):
        # The body is already sent. Like the original after-response meter,
        # this must not be cancelled just because the generation budget ended.
        return await run_in_threadpool(self._run, lambda: self.service.record_sent(self.admission))

    def close(self, accepted):
        with self._lock:
            self._closed, self._accepted = True, accepted
            return self._claim_cleanup() if not self._in_work else False

    def cleanup(self):
        try:
            self.service.release(self.admission)
        except Exception as exc:
            # Pending leases remain charged until lazy expiry recovery.
            # Ready/uncertain delivery stays charged, never free usage.
            print(f"WARN: USAGE_QUOTA_RELEASE_FAILED {type(exc).__name__}")


class QuotaAction:
    """Adapter reusing the app's bounded account-work admission."""
    def __init__(self, action):
        self.action = action

    async def resolve_request_account(self, ignored):
        return await self.action()


class QuotaResponse(JSONResponse):
    """Identical response bytes; compensate a known failed send only."""
    def __init__(self, original, state, release, record_sent):
        self.status_code, self.media_type = original.status_code, original.media_type
        self.background, self.body = None, original.body
        self._character_meter = original.background
        self.raw_headers = list(original.raw_headers)
        self._state, self._release, self._record_sent = state, release, record_sent

    async def __call__(self, scope, receive, send):
        sent = False
        async def checked_send(message):
            nonlocal sent
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                sent = True
        try:
            await super().__call__(scope, receive, checked_send)
        except BaseException:
            if not sent:
                self._release(self._state)
            raise

        await self._record_sent(self._state)
        if self._character_meter is not None:
            await self._character_meter()
