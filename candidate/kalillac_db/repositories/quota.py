"""Short account-row transactions for daily admission and settlement.

Callers lock the existing User row first, shared with billing. PostgreSQL's
FOR NO KEY UPDATE serializes all workers for one account. SQLite file tests use
BEGIN IMMEDIATE; they are not evidence of PostgreSQL locking.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import uuid
from sqlalchemy import func, select, update
from ..models import AccountUsageDaily, AccountUsageSlot, User
from .usage import get_daily_usage, increment_daily_usage


class QuotaError(Exception):
    status_code = 503
    code = "service_unavailable"


class QuotaBusy(QuotaError):
    """Capacity is held by in-flight (pending) leases that may still be
    released: a temporary condition, reported with the generic busy code."""
    status_code = 429
    code = "busy"


class QuotaDailyLimitReached(QuotaError):
    """The account's charged usage for the UTC day has reached its limit;
    no in-flight lease can free it."""
    status_code = 429
    code = "daily_limit_reached"


class QuotaIdentityInvalid(QuotaError):
    status_code = 401
    code = "not_authenticated"


class QuotaUnavailable(QuotaError):
    pass


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def lock_account(session, user_id):
    session.execute(select(User.id).where(User.id == user_id).with_for_update(key_share=True)).scalar_one()


@dataclass(frozen=True)
class Reservation:
    user_id: uuid.UUID
    usage_date: date
    slot: int
    expires_at: datetime
    daily_limit: int


def expire_pending(session, user_id, day, now):
    session.execute(update(AccountUsageSlot).where(
        AccountUsageSlot.user_id == user_id, AccountUsageSlot.usage_date == day,
        AccountUsageSlot.state == "pending", AccountUsageSlot.expires_at <= now,
    ).values(state="released", completed_date=None).execution_options(synchronize_session=False))
    # Ready but unsettled means delivery may have happened. Preserve charged
    # capacity without inventing a successful chat after a crash or outage.
    session.execute(update(AccountUsageSlot).where(
        AccountUsageSlot.user_id == user_id, AccountUsageSlot.completed_date == day,
        AccountUsageSlot.state == "ready", AccountUsageSlot.expires_at <= now,
    ).values(state="uncertain").execution_options(synchronize_session=False))


def capacity_breakdown(session, user_id, day):
    """(charged, pending) for the day. Charged usage is settled chats plus
    ready/uncertain leases (delivery may have happened; never refunded).
    Pending leases still hold capacity but can be released."""
    row = get_daily_usage(session, user_id, day)
    completed = row.successful_chats if row else 0
    def count(*condition):
        return session.scalar(select(func.count()).select_from(AccountUsageSlot).where(
            AccountUsageSlot.user_id == user_id, *condition))
    prepared = count(AccountUsageSlot.completed_date == day, AccountUsageSlot.state.in_(("ready", "uncertain")))
    pending = count(AccountUsageSlot.usage_date == day, AccountUsageSlot.state == "pending")
    return completed + prepared, pending


def ensure_capacity(session, user_id, day, limit):
    """Refuse when the day is full: daily_limit_reached once charged usage
    alone reaches the limit, otherwise busy while pending leases fill it."""
    charged, pending = capacity_breakdown(session, user_id, day)
    if charged >= limit:
        raise QuotaDailyLimitReached()
    if charged + pending >= limit:
        raise QuotaBusy()


def reserve_slot(session, user_id, day, limit, now, ttl):
    expire_pending(session, user_id, day, now)
    ensure_capacity(session, user_id, day, limit)
    row = session.scalar(select(AccountUsageSlot).where(
        AccountUsageSlot.user_id == user_id, AccountUsageSlot.usage_date == day,
        AccountUsageSlot.state == "released",
    ).order_by(AccountUsageSlot.slot).limit(1))
    expiry = now + ttl
    if row is None:
        maximum = session.scalar(select(func.max(AccountUsageSlot.slot)).where(
            AccountUsageSlot.user_id == user_id, AccountUsageSlot.usage_date == day))
        row = AccountUsageSlot(user_id=user_id, usage_date=day, slot=0 if maximum is None else maximum + 1)
        session.add(row)
    else:
        # A strictly newer lease fences stale completion/release after reuse,
        # including two operations with the same injected clock instant.
        expiry = max(expiry, utc(row.expires_at) + timedelta(microseconds=1))
    row.state, row.expires_at, row.completed_date = "pending", expiry, None
    session.flush()
    return Reservation(user_id, day, row.slot, expiry, limit)


def matching_slot(session, reservation):
    row = session.get(AccountUsageSlot, (reservation.user_id, reservation.usage_date, reservation.slot))
    if row is None or utc(row.expires_at) != utc(reservation.expires_at):
        return None
    return row


def complete_slot(session, reservation, now, completion_limit):
    row = matching_slot(session, reservation)
    if row is not None and row.state in {"ready", "uncertain", "completed"}:
        return row.completed_date  # Idempotent response preparation.
    if row is None or row.state != "pending" or utc(row.expires_at) <= now:
        raise QuotaUnavailable()
    day = now.date()
    expire_pending(session, reservation.user_id, day, now)
    # Same-day preparation moves already admitted capacity from pending to
    # ready without increasing it. Honor every valid lease, including a free
    # lease followed by an upgrade and downgrade before completion.
    if day != reservation.usage_date:
        # Preserve completion-day UTC counting; crossing midnight must not
        # bypass the new day's allowance or a now-expired paid entitlement.
        ensure_capacity(session, reservation.user_id, day, completion_limit)
    row.state, row.completed_date = "ready", day
    session.flush()
    return day


def record_sent_slot(session, reservation):
    """A normal response body was sent; settle once, without request content."""
    row = matching_slot(session, reservation)
    if row is None or row.state not in {"ready", "uncertain", "completed"}:
        raise QuotaUnavailable()
    if row.state != "completed":
        increment_daily_usage(session, reservation.user_id, row.completed_date, request_chars=0, response_chars=0)
        row.state = "completed"
        session.flush()
    return row.completed_date


def release_slot(session, reservation):
    row = matching_slot(session, reservation)
    if row is None or row.state == "released":
        return
    if row.state == "completed":
        return  # A confirmed normal body send is never refunded by cleanup.
    row.state, row.completed_date = "released", None
    session.flush()
