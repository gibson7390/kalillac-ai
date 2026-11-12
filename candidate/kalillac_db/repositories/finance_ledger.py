"""Financial ledger operations. Each runs inside the caller's transaction.

Invariants (every bucket, every period, every scope):
  * exposure = reserved + settled, in µUSD; admission requires
    exposure + amount <= the scope's ceiling for EVERY charged bucket (the
    scope's day and month, and GLOBAL's day and month), checked and
    incremented under row locks in one transaction that commits before
    the provider attempt may dispatch;
  * a charge lands in GLOBAL and exactly one of FREE or PAID, in the day and
    month of its reservation, and is settled or released in those same
    buckets even after a period rollover;
  * a reservation leaves `reserved` only by release (never dispatched: the
    whole amount returns) or settlement (dispatched: the actual cost, or the
    full reservation when the cost is unknown, moves to `settled`). An
    outstanding reservation, for example after a crash, stays charged until
    an explicit operator reconciliation;
  * an actual cost above its reservation is charged in full (never capped),
    accumulated as overrun on every charged bucket, and latches the scope
    unsafe; a GLOBAL overrun reaching the configured headroom, or GLOBAL
    exposure reaching the global ceiling, latches GLOBAL, which stops all.

Lock order, the same in every transaction (no deadlock):
  provider_usage_pools row (attempt ceiling, when present)
  -> this attempt's finance_reservations row (release/settle only)
  -> finance_ledger_buckets in (period kind: day < month, period start,
     scope: global < free < paid) order
  -> finance_unsafe_latches (read after the bucket locks; inserted last).
A latch is inserted while holding GLOBAL bucket locks that every admission
also takes, so any admission ordered after it reads it.
"""
from dataclasses import dataclass, field
import threading
import uuid

from sqlalchemy import select

from ..models import FinanceLedgerBucket, FinanceReservation, FinanceUnsafeLatch

_KIND_ORDER = {"day": 0, "month": 1}
_SCOPE_ORDER = {"global": 0, "free": 1, "paid": 2}


@dataclass
class FinanceLease:
    """The committed financial half of one provider-attempt lease."""

    reservation_id: uuid.UUID
    scope: str
    day: object
    month: object
    reserved: int
    charge: object = field(repr=False)
    resolved: bool = False
    lock: object = field(default_factory=threading.Lock, repr=False)


def _insert(session):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    return pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert


def bucket_keys(day, month, scope):
    keys = {("day", day, "global"), ("day", day, scope), ("month", month, "global"), ("month", month, scope)}
    return sorted(keys, key=lambda k: (_KIND_ORDER[k[0]], k[1], _SCOPE_ORDER[k[2]]))


def lock_buckets(session, keys):
    """Create-if-absent then lock each bucket, strictly in the given order."""
    insert = _insert(session)
    rows = {}
    for kind, start, scope in keys:
        session.execute(insert(FinanceLedgerBucket).values(
            period_kind=kind, period_start=start, scope=scope, reserved_microusd=0, settled_microusd=0,
            overrun_microusd=0).on_conflict_do_nothing(index_elements=["period_kind", "period_start", "scope"]))
        rows[(kind, start, scope)] = session.scalar(select(FinanceLedgerBucket).where(
            FinanceLedgerBucket.period_kind == kind, FinanceLedgerBucket.period_start == start,
            FinanceLedgerBucket.scope == scope).with_for_update().execution_options(populate_existing=True))
    return rows


def latched_scopes(session):
    return {row[0]: row[1] for row in session.execute(select(FinanceUnsafeLatch.scope, FinanceUnsafeLatch.reason))}


def latch(session, scope, reason, now, reservation_id=None):
    session.execute(_insert(session)(FinanceUnsafeLatch).values(
        scope=scope, reason=reason, latched_at=now, reservation_id=reservation_id).on_conflict_do_nothing(
        index_elements=["scope"]))
    print("WARN: FINANCE_UNSAFE_LATCHED", "scope=" + scope, "reason=" + reason)


def reserve(session, charge, now, limits):
    """Admit and durably reserve `charge` (runtime.Charge) at database time now."""
    from kalillac_routing.provider_controls import CeilingBusy, CeilingUnavailable
    day = now.date()
    month = day.replace(day=1)
    scope = charge.scope.value
    if scope not in ("free", "paid") or type(charge.reserved) is not int or charge.reserved < 0:
        raise CeilingUnavailable()
    rows = lock_buckets(session, bucket_keys(day, month, scope))
    latched = latched_scopes(session)
    if "global" in latched or scope in latched:
        print("WARN: FINANCE_ADMISSION_REFUSED", "scope=" + scope, "reason=latched")
        raise CeilingUnavailable()
    for (kind, _start, bucket_scope), row in rows.items():
        if row.reserved_microusd + row.settled_microusd + charge.reserved > limits.limit(kind, bucket_scope):
            raise CeilingBusy()
    for row in rows.values():
        row.reserved_microusd += charge.reserved
    reservation_id = uuid.uuid4()
    session.add(FinanceReservation(
        id=reservation_id, provider=charge.provider, scope=scope, day=day, month=month,
        reserved_microusd=charge.reserved, state="reserved", charged_microusd=None, overrun_microusd=0,
        bound_exceeded=False, basis=None, created_at=now, resolved_at=None))
    session.flush()
    return FinanceLease(reservation_id, scope, day, month, charge.reserved, charge)


def _outstanding(session, lease):
    return session.scalar(select(FinanceReservation).where(
        FinanceReservation.id == lease.reservation_id, FinanceReservation.state == "reserved",
    ).with_for_update().execution_options(populate_existing=True))


def release(session, lease, now):
    """A never-dispatched attempt: its whole reservation returns."""
    reservation = _outstanding(session, lease)
    if reservation is None:
        return False
    rows = lock_buckets(session, bucket_keys(reservation.day, reservation.month, reservation.scope))
    for row in rows.values():
        row.reserved_microusd -= reservation.reserved_microusd
    reservation.state, reservation.resolved_at = "released", now
    session.flush()
    return True


def settle(session, lease, settlement, now, limits):
    """A dispatched attempt: move its reservation to its settled charge."""
    reservation = _outstanding(session, lease)
    if reservation is None:
        return False
    if settlement.reserved != reservation.reserved_microusd:
        raise ValueError("settlement does not match the reservation")
    rows = lock_buckets(session, bucket_keys(reservation.day, reservation.month, reservation.scope))
    for row in rows.values():
        row.reserved_microusd -= reservation.reserved_microusd
        row.settled_microusd += settlement.charged
        row.overrun_microusd += settlement.overrun
    reservation.state, reservation.resolved_at = "settled", now
    reservation.charged_microusd, reservation.overrun_microusd = settlement.charged, settlement.overrun
    reservation.bound_exceeded, reservation.basis = bool(settlement.bound_exceeded), settlement.basis
    session.flush()
    if settlement.overrun > 0 or settlement.bound_exceeded:
        latch(session, reservation.scope, "overrun" if settlement.overrun > 0 else "bound_exceeded", now,
              reservation.id)
        for kind, start in (("day", reservation.day), ("month", reservation.month)):
            bucket = rows[(kind, start, "global")]
            if (bucket.overrun_microusd >= limits.headroom(kind)
                    or bucket.reserved_microusd + bucket.settled_microusd >= limits.limit(kind, "global")):
                latch(session, "global", "headroom_exhausted", now, reservation.id)
                break
    session.flush()
    return True
