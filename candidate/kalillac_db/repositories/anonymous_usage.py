"""Short visitor-day transactions for the anonymous daily allowance.

Functions receive only a 64-character per-UTC-day digest, never a visitor
token. Each operation runs in one bounded transaction, and the caller locks
the visitor-day row first: PostgreSQL's FOR NO KEY UPDATE serializes all
workers for one visitor and day. SQLite file tests use BEGIN IMMEDIATE; they
are not evidence of PostgreSQL locking.

Capacity is counted on the completion UTC day. pending, ready, uncertain and
completed slots consume it; released slots do not. uncertain and completed
are never refunded. Slots are reused, so a visitor-day keeps at most `limit`
slot rows.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from sqlalchemy import delete, func, select, tuple_, update
from ..models import AnonymousUsageDay, AnonymousUsageSlot
from .quota import QuotaError, QuotaUnavailable, utc

CHARGED_STATES = ("pending", "ready", "uncertain", "completed")
PREPARED_STATES = ("ready", "uncertain", "completed")
PRUNE_BATCH = 500
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class AnonymousLimitReached(QuotaError):
    status_code = 429
    code = "anonymous_daily_limit_reached"


@dataclass(frozen=True, repr=False)
class AnonymousLease:
    visitor_digest: str
    usage_date: date
    slot: int
    expires_at: datetime

    def __repr__(self):
        # Never print the digest.
        return f"AnonymousLease(usage_date={self.usage_date.isoformat()}, slot={self.slot})"


def _check_digest(digest):
    # Only a per-day digest is accepted; a token passed by mistake fails closed.
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise QuotaUnavailable()


def _insert(session):
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def lock_visitor_day(session, digest, day, create=False):
    """Lock the visitor-day row, creating it first if asked. Returns whether
    the row exists. Concurrent first creators wait on the inserting
    transaction, then lock the committed row."""
    _check_digest(digest)
    if create:
        session.execute(_insert(session)(AnonymousUsageDay).values(visitor_digest=digest, usage_date=day)
                        .on_conflict_do_nothing(index_elements=["visitor_digest", "usage_date"]))
    found = session.scalar(select(AnonymousUsageDay.usage_date).where(
        AnonymousUsageDay.visitor_digest == digest, AnonymousUsageDay.usage_date == day,
    ).with_for_update(key_share=True))
    return found is not None


def _slots(digest, day):
    return (AnonymousUsageSlot.visitor_digest == digest, AnonymousUsageSlot.usage_date == day)


def recover_expired(session, digest, day, now):
    session.execute(update(AnonymousUsageSlot).where(
        *_slots(digest, day), AnonymousUsageSlot.state == "pending", AnonymousUsageSlot.expires_at <= now,
    ).values(state="released", completed_date=None).execution_options(synchronize_session=False))
    # Ready but unsettled means delivery may have happened: keep it charged.
    session.execute(update(AnonymousUsageSlot).where(
        *_slots(digest, day), AnonymousUsageSlot.state == "ready", AnonymousUsageSlot.expires_at <= now,
    ).values(state="uncertain").execution_options(synchronize_session=False))


def capacity_used(session, digest, day):
    return session.scalar(select(func.count()).select_from(AnonymousUsageSlot).where(
        *_slots(digest, day), AnonymousUsageSlot.state.in_(CHARGED_STATES)))


def _claim_slot(session, digest, day, now, ttl, state):
    row = session.scalar(select(AnonymousUsageSlot).where(
        *_slots(digest, day), AnonymousUsageSlot.state == "released",
    ).order_by(AnonymousUsageSlot.slot).limit(1))
    expiry = now + ttl
    if row is None:
        # Only reached when every existing slot is charged and below the limit.
        maximum = session.scalar(select(func.max(AnonymousUsageSlot.slot)).where(*_slots(digest, day)))
        row = AnonymousUsageSlot(visitor_digest=digest, usage_date=day, slot=0 if maximum is None else maximum + 1)
        session.add(row)
    else:
        # A strictly newer lease fences stale completion/release after reuse,
        # including two operations with the same clock instant.
        expiry = max(expiry, utc(row.expires_at) + timedelta(microseconds=1))
    row.state, row.expires_at = state, expiry
    row.completed_date = day if state == "ready" else None
    session.flush()
    return AnonymousLease(digest, day, row.slot, expiry)


def reserve_anonymous_slot(session, digest, day, limit, now, ttl):
    """Caller holds the visitor-day lock. Raises AnonymousLimitReached."""
    _check_digest(digest)
    recover_expired(session, digest, day, now)
    if capacity_used(session, digest, day) >= limit:
        raise AnonymousLimitReached()
    return _claim_slot(session, digest, day, now, ttl, "pending")


def matching_anonymous_slot(session, lease):
    row = session.get(AnonymousUsageSlot, (lease.visitor_digest, lease.usage_date, lease.slot))
    if row is None or utc(row.expires_at) != utc(lease.expires_at):
        return None
    return row


def anonymous_slot_state(session, lease):
    row = matching_anonymous_slot(session, lease)
    return None if row is None else row.state


def validate_anonymous_slot(session, lease, now):
    row = matching_anonymous_slot(session, lease)
    if row is None or row.state != "pending" or utc(row.expires_at) <= now:
        raise QuotaUnavailable()


def prepare_anonymous_slot(session, lease, now):
    """Same-day preparation: pending -> ready on the lease's own day.
    Idempotent once prepared. Returns the (unchanged) lease."""
    row = matching_anonymous_slot(session, lease)
    if row is not None and row.state in PREPARED_STATES:
        return lease
    if row is None or row.state != "pending" or utc(row.expires_at) <= now or now.date() != lease.usage_date:
        raise QuotaUnavailable()
    row.state, row.completed_date = "ready", lease.usage_date
    session.flush()
    return lease


def roll_over_anonymous_slot(session, old, new_digest, new_day, limit, now, ttl):
    """Preparation after the reservation's UTC day. The caller holds both
    visitor-day locks, old day first. Releases the old pending lease and
    returns a replacement lease that is already ready on the new day.
    Nothing links the two days' rows."""
    _check_digest(new_digest)
    if new_day <= old.usage_date:
        raise QuotaUnavailable()
    row = matching_anonymous_slot(session, old)
    if row is None or row.state != "pending" or utc(row.expires_at) <= now:
        raise QuotaUnavailable()
    row.state, row.completed_date = "released", None
    session.flush()
    recover_expired(session, new_digest, new_day, now)
    if capacity_used(session, new_digest, new_day) >= limit:
        raise AnonymousLimitReached()
    return _claim_slot(session, new_digest, new_day, now, ttl, "ready")


def record_sent_anonymous_slot(session, lease):
    """The final response body was sent; settle once."""
    row = matching_anonymous_slot(session, lease)
    if row is None or row.state not in PREPARED_STATES:
        raise QuotaUnavailable()
    if row.state != "completed":
        row.state = "completed"
        session.flush()
    return lease.usage_date


def release_anonymous_slot(session, lease, now):
    """Refund only a lease whose delivery is known not to have happened."""
    row = matching_anonymous_slot(session, lease)
    if row is None or row.state not in ("pending", "ready"):
        return  # Released already; uncertain and completed stay charged.
    if row.state == "ready" and utc(row.expires_at) <= now:
        # Expired before recovery ran: delivery may have happened.
        row.state = "uncertain"
    else:
        row.state, row.completed_date = "released", None
    session.flush()


def prune_anonymous_days(session, today, batch=PRUNE_BATCH):
    """Delete at most `batch` visitor-days older than yesterday, with their
    slots. Rows another transaction holds are skipped on PostgreSQL."""
    query = select(AnonymousUsageDay.visitor_digest, AnonymousUsageDay.usage_date).where(
        AnonymousUsageDay.usage_date < today - timedelta(days=1),
    ).order_by(AnonymousUsageDay.usage_date).limit(batch)
    if session.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    keys = [tuple(key) for key in session.execute(query).all()]
    if not keys:
        return 0
    session.execute(delete(AnonymousUsageSlot).where(
        tuple_(AnonymousUsageSlot.visitor_digest, AnonymousUsageSlot.usage_date).in_(keys),
    ).execution_options(synchronize_session=False))
    session.execute(delete(AnonymousUsageDay).where(
        tuple_(AnonymousUsageDay.visitor_digest, AnonymousUsageDay.usage_date).in_(keys),
    ).execution_options(synchronize_session=False))
    return len(keys)
