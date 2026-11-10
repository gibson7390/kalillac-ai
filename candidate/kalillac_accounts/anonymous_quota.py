"""Request-local anonymous daily allowance. No provider calls or content.

A visitor is identified only by a random 256-bit token held in the browser's
cookie. Storage sees a SHA-256 digest of a fixed domain label, that token and
the UTC date, so one browser's days cannot be linked and the token is never
stored. The token stays in memory only for one request and is dropped once
the response is prepared; it is never logged or included in an exception.

Capacity is charged on the completion UTC day. A request prepared after its
reservation's UTC day releases the old-day lease and takes a ready lease
under the new day's digest in one transaction; nothing stored links them.
Database failures fail closed with QuotaUnavailable.
"""
import base64
import hashlib
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import func, select, text
from starlette.concurrency import run_in_threadpool
from kalillac_db.engine import get_session_factory
from kalillac_db.repositories.anonymous_usage import (
    AnonymousLimitReached, PREPARED_STATES, anonymous_slot_state, lock_visitor_day, prepare_anonymous_slot,
    prune_anonymous_days, record_sent_anonymous_slot, release_anonymous_slot, reserve_anonymous_slot,
    roll_over_anonymous_slot, validate_anonymous_slot,
)
from kalillac_db.repositories.quota import QuotaError, QuotaUnavailable, utc
from kalillac_routing.request_budget import RequestBudgetError

__all__ = [
    "AnonymousLimitReached", "AnonymousQuota", "AnonymousQuotaRequest", "VISITOR_COOKIE",
    "VISITOR_MAX_AGE", "day_digest", "new_visitor_token", "parse_visitor_token",
]

VISITOR_COOKIE = "__Host-kalillac_visitor"
VISITOR_MAX_AGE = 48 * 60 * 60  # Sliding; refreshed by later responses.
RESERVATION_TTL = timedelta(minutes=15)
PRUNE_INTERVAL_SECONDS = 60
_TOKEN_BYTES = 32
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # Unpadded base64url of 32 bytes.
_DIGEST_LABEL = b"kalillac.anonymous-daily-allowance.v1\x00"


def _encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_visitor_token():
    return _encode(secrets.token_bytes(_TOKEN_BYTES))


def parse_visitor_token(value):
    """Return the token's 32 bytes, or None for anything not produced by
    new_visitor_token(). The caller mints a new token on None."""
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        return None
    raw = base64.urlsafe_b64decode(value + "=")
    # Reject non-canonical spellings (unused low bits set in the last char).
    return raw if len(raw) == _TOKEN_BYTES and _encode(raw) == value else None


def day_digest(token, day):
    if not isinstance(token, bytes) or len(token) != _TOKEN_BYTES:
        raise ValueError("A visitor token must be 32 bytes.")
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError("A UTC date is required.")
    return hashlib.sha256(_DIGEST_LABEL + token + b"\x00" + day.isoformat().encode("ascii")).hexdigest()


class AnonymousQuota:
    def __init__(self, limit, session_factory=get_session_factory, clock=None, ttl=RESERVATION_TTL,
                 monotonic=time.monotonic):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("The anonymous daily limit must be a positive integer.")
        if ttl.total_seconds() <= 0:
            raise ValueError("Reservation lifetime must be positive.")
        self.limit, self._factory, self._clock, self.ttl = limit, session_factory, clock, ttl
        self._monotonic, self._prune_lock, self._next_prune = monotonic, threading.Lock(), None

    def new_request(self, token):
        return AnonymousQuotaRequest(self, token)

    def _now(self, session):
        if self._clock is not None:
            return utc(self._clock())
        if session.get_bind().dialect.name == "postgresql":
            return utc(session.scalar(select(func.clock_timestamp())))
        return datetime.now(timezone.utc)

    def _ttl(self, request):
        ttl = self.ttl
        if request is not None and request.budget is not None:
            ttl = max(ttl, timedelta(seconds=max(0, request.budget.deadline - time.monotonic()) + 5))
        return ttl

    def _transaction(self, operation):
        try:
            factory = self._factory()
            if factory is None:
                raise QuotaUnavailable()
            with factory() as session, session.begin():
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                    session.execute(text("SET LOCAL lock_timeout = '3s'"))
                    session.execute(text("SET LOCAL statement_timeout = '3s'"))
                elif session.get_bind().dialect.name != "sqlite":
                    raise QuotaUnavailable()
                return operation(session)
        except (QuotaError, RequestBudgetError):
            raise
        except Exception as exc:
            # The type name only: database errors can quote digests.
            print(f"WARN: ANONYMOUS_QUOTA_DATABASE_FAILED {type(exc).__name__}")
            raise QuotaUnavailable() from None

    def reserve(self, token, request):
        def operation(session):
            request.ensure_open()
            day = self._now(session).date()
            digest = day_digest(token, day)
            lock_visitor_day(session, digest, day, create=True)
            now = self._now(session)  # Fresh after the visitor-day lock.
            lease = reserve_anonymous_slot(session, digest, day, self.limit, now, self._ttl(request))
            request.ensure_open()
            return lease
        lease = self._transaction(operation)
        self._maybe_prune()
        return lease

    def validate(self, lease, request):
        def operation(session):
            request.ensure_open()
            if not lock_visitor_day(session, lease.visitor_digest, lease.usage_date):
                raise QuotaUnavailable()
            validate_anonymous_slot(session, lease, self._now(session))
            request.ensure_open()
        return self._transaction(operation)

    def complete(self, lease, token, request):
        """Prepare the response. Returns the lease to hold from now on: the
        same lease, or its new-day replacement after UTC midnight."""
        def operation(session):
            request.ensure_open()
            if not lock_visitor_day(session, lease.visitor_digest, lease.usage_date):
                raise QuotaUnavailable()
            now = self._now(session)
            day = now.date()
            if day == lease.usage_date or anonymous_slot_state(session, lease) in PREPARED_STATES:
                result = prepare_anonymous_slot(session, lease, now)
            elif day < lease.usage_date:
                raise QuotaUnavailable()  # Authoritative clock went backwards.
            else:
                # Locks are taken in ascending date order: old day, then new.
                new_digest = day_digest(token, day)
                lock_visitor_day(session, new_digest, day, create=True)
                now = self._now(session)
                result = roll_over_anonymous_slot(session, lease, new_digest, day, self.limit, now, self.ttl)
            request.ensure_open()
            return result
        return self._transaction(operation)

    def record_sent(self, lease):
        def operation(session):
            if not lock_visitor_day(session, lease.visitor_digest, lease.usage_date):
                raise QuotaUnavailable()
            return record_sent_anonymous_slot(session, lease)
        return self._transaction(operation)

    def release(self, lease):
        def operation(session):
            if lock_visitor_day(session, lease.visitor_digest, lease.usage_date):
                release_anonymous_slot(session, lease, self._now(session))
        return self._transaction(operation)

    def _maybe_prune(self):
        moment = self._monotonic()
        with self._prune_lock:
            if self._next_prune is not None and moment < self._next_prune:
                return
            self._next_prune = moment + PRUNE_INTERVAL_SECONDS
        self.prune()

    def prune(self):
        """Best effort: one bounded batch of days older than yesterday."""
        try:
            return self._transaction(lambda session: prune_anonymous_days(session, self._now(session).date()))
        except Exception:
            print("WARN: ANONYMOUS_QUOTA_PRUNE_FAILED")
            return 0


class AnonymousQuotaRequest:
    """One request's lease, visitor token and cancellation state.

    The token is held only until the response is prepared. A late database
    worker owns cleanup when the handler has already stopped; cleanup is
    idempotent and fenced against reuse, and never refunds an uncertain or
    completed lease.
    """
    def __init__(self, service, token):
        if not isinstance(token, bytes) or len(token) != _TOKEN_BYTES:
            raise ValueError("A visitor token must be 32 bytes.")
        self.service, self.lease, self._token = service, None, token
        self.budget = self.disconnected = None
        self._lock = threading.Lock()
        self._closed = self._accepted = self._in_work = self._cleanup_claimed = False

    def __repr__(self):
        return "AnonymousQuotaRequest()"

    @property
    def holds_token(self):
        return self._token is not None

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
        if self._closed and not self._accepted and not self._cleanup_claimed and self.lease is not None:
            self._cleanup_claimed = True
            return True
        return False

    def reserve_now(self):
        def work():
            if self._token is None:
                raise QuotaUnavailable()
            lease = self.service.reserve(self._token, self)
            with self._lock:
                self.lease = lease
        return self._run(work)

    async def reserve(self):
        return await run_in_threadpool(self.reserve_now)

    def before_work(self):
        if self.lease is not None:
            self._run(lambda: self.service.validate(self.lease, self))

    def complete_now(self):
        def work():
            with self._lock:
                token, lease = self._token, self.lease
            if token is None or lease is None:
                raise QuotaUnavailable()
            try:
                replacement = self.service.complete(lease, token, self)
            finally:
                # Preparation is attempted once; the token is no longer needed.
                with self._lock:
                    self._token = None
                del token
            # Only a committed replacement becomes the lease cleanup targets.
            with self._lock:
                self.lease = replacement
            return replacement.usage_date
        return self._run(work)

    async def complete(self):
        return await run_in_threadpool(self.complete_now)

    def record_sent_now(self):
        return self._run(lambda: self.service.record_sent(self.lease))

    async def record_sent(self):
        return await run_in_threadpool(self.record_sent_now)

    def close(self, accepted):
        with self._lock:
            self._closed, self._accepted, self._token = True, accepted, None
            return self._claim_cleanup() if not self._in_work else False

    def cleanup(self):
        try:
            self.service.release(self.lease)
        except Exception as exc:
            # Pending leases remain charged until lazy expiry recovery.
            # Ready/uncertain delivery stays charged, never free usage.
            print(f"WARN: ANONYMOUS_QUOTA_RELEASE_FAILED {type(exc).__name__}")
