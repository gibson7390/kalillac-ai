"""Operator audit and reconciliation of the Financial Safeguards ledger.

Used only by tools/finance_ledger_admin.py; no HTTP route reaches this module
and the application never calls it. Every function runs inside the caller's
transaction and handles financial and control metadata only.

    read_snapshot   make the caller's transaction one REPEATABLE READ, READ
                    ONLY snapshot (its first statement);
    verify          recompute every scope and provider bucket from the
                    reservation rows and report each difference;
    summary         exposure, outstanding reservations, latches and
                    violations for a report;
    stale_ids       outstanding reservations older than a cutoff;
    resolve_stale   finalise ONE of them at its full reserved amount, with an
                    operator-action record (call once per transaction, so the
                    lock order is exactly the application's: reservation row,
                    then buckets);
    clear_latch     remove one unsafe latch whose reason the operator names,
                    with an operator-action record. Only the owner role can
                    delete a latch.

Nothing here refunds money on a guess: a reservation whose outcome is unknown
is always charged in full.

verify and summary compare several reads (reservations, scope buckets,
provider buckets, latches). Under READ COMMITTED each statement sees the
latest commit, so a reservation settled between two of those reads appears
in one and not the other, and a consistent ledger is reported as
inconsistent (Codex audit D3). On PostgreSQL both therefore refuse to run
outside a REPEATABLE READ (or SERIALIZABLE), READ ONLY transaction: every read
then sees the same committed state, and the report can change nothing.
"""
from datetime import timedelta
import uuid

from sqlalchemy import case, delete, func, select, text

from ..models import (FinanceLedgerBucket, FinanceOperatorAction, FinanceProviderBucket, FinanceReservation,
                      FinanceUnsafeLatch)
from . import finance_ledger

# No request can still be in flight after this long, so an older outstanding
# reservation belongs to a process that died or lost its database.
MIN_STALE_SECONDS = 3600
SCOPES = ("global", "free", "paid")
PROVIDERS = ("openai", "brave", "tavily")
LATCH_REASONS = ("overrun", "bound_exceeded", "headroom_exhausted")
ZERO = (0, 0, 0)


class SnapshotRequired(RuntimeError):
    """A ledger comparison was attempted outside one stable, read-only snapshot."""


def read_snapshot(session):
    """Make the caller's new transaction a single REPEATABLE READ, READ ONLY snapshot. It must be the
    transaction's first statement. SQLite (development and tests) has one writer: nothing to set."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


def _require_snapshot(session):
    if session.get_bind().dialect.name != "postgresql":
        return
    isolation, read_only = session.execute(text(
        "SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only')")).one()
    if isolation not in ("repeatable read", "serializable") or read_only != "on":
        raise SnapshotRequired("ledger comparisons need one REPEATABLE READ, READ ONLY snapshot")


def _expected(session):
    """{(table, period kind, period start, scope or provider): amounts} from reservations."""
    R = FinanceReservation
    reserved = func.coalesce(func.sum(case((R.state == "reserved", R.reserved_microusd), else_=0)), 0)
    settled = func.coalesce(func.sum(case((R.state == "settled", R.charged_microusd), else_=0)), 0)
    overrun = func.coalesce(func.sum(case((R.state == "settled", R.overrun_microusd), else_=0)), 0)
    expected = {}
    for kind, column in (("day", R.day), ("month", R.month)):
        rows = session.execute(select(column, R.scope, R.provider, reserved, settled, overrun)
                               .group_by(column, R.scope, R.provider))
        for start, scope, provider, *amounts in rows:
            for key in (("scope", kind, start, "global"), ("scope", kind, start, scope),
                        ("provider", kind, start, provider)):
                current = expected.get(key, ZERO)
                expected[key] = tuple(current[i] + int(amounts[i]) for i in range(3))
    return expected


def _actual(session):
    actual = {}
    for table, model, column in (("scope", FinanceLedgerBucket, FinanceLedgerBucket.scope),
                                 ("provider", FinanceProviderBucket, FinanceProviderBucket.provider)):
        for kind, start, name, *amounts in session.execute(select(
                model.period_kind, model.period_start, column, model.reserved_microusd, model.settled_microusd,
                model.overrun_microusd)):
            actual[(table, kind, start, name)] = tuple(int(v) for v in amounts)
    return actual


def verify(session):
    """Every bucket that differs from the sum of its reservations, as
    (table, period kind, period start ISO date, name, bucket amounts,
    reservation amounts). An empty list means the ledger is consistent.
    On PostgreSQL it requires read_snapshot() (SnapshotRequired otherwise)."""
    _require_snapshot(session)
    expected, actual = _expected(session), _actual(session)
    return [(key[0], key[1], key[2].isoformat(), key[3], actual.get(key, ZERO), expected.get(key, ZERO))
            for key in sorted(set(expected) | set(actual), key=lambda k: (k[0], k[1], k[2], k[3]))
            if actual.get(key, ZERO) != expected.get(key, ZERO)]


def summary(session, now, limits=None):
    """A report for the current UTC day and month. `limits` (FinancialLimits)
    adds each bucket's ceiling when the configuration is available. On
    PostgreSQL it requires read_snapshot() (SnapshotRequired otherwise)."""
    _require_snapshot(session)
    day = now.date()
    periods = {"day": day, "month": day.replace(day=1)}
    actual = _actual(session)

    def bucket(table, kind, name):
        reserved, settled, overrun = actual.get((table, kind, periods[kind], name), ZERO)
        row = {"reserved": reserved, "settled": settled, "overrun": overrun}
        if limits is not None:
            row["limit"] = limits.limit(kind, name) if table == "scope" else limits.provider_limit(kind, name)
        return row

    R = FinanceReservation
    count, amount, oldest = session.execute(select(
        func.count(), func.coalesce(func.sum(R.reserved_microusd), 0), func.min(R.created_at)
    ).where(R.state == "reserved")).one()
    return {
        "periods": {kind: start.isoformat() for kind, start in periods.items()},
        "scopes": {kind: {scope: bucket("scope", kind, scope) for scope in SCOPES} for kind in periods},
        "providers": {kind: {provider: bucket("provider", kind, provider) for provider in PROVIDERS}
                      for kind in periods},
        "outstanding": {"count": int(count), "microusd": int(amount),
                        "oldest": oldest.isoformat() if oldest is not None else None},
        "latches": finance_ledger.latched_scopes(session),
        "violations": verify(session),
    }


def stale_ids(session, now, older_than_seconds, batch=200):
    """Ids of outstanding reservations created more than older_than_seconds ago, oldest first."""
    if type(older_than_seconds) is not int or older_than_seconds < MIN_STALE_SECONDS:
        raise ValueError("A stale reservation must be at least one hour old.")
    if type(batch) is not int or not 1 <= batch <= 1000:
        raise ValueError("Invalid batch size.")
    R = FinanceReservation
    cutoff = now - timedelta(seconds=older_than_seconds)
    return list(session.scalars(select(R.id).where(R.state == "reserved", R.created_at < cutoff)
                                .order_by(R.created_at, R.id).limit(batch)))


def resolve_stale(session, reservation_id, now, older_than_seconds):
    """Finalise one stale outstanding reservation at its full reserved amount
    and record the action. Returns the µUSD amount, or None when it is no
    longer outstanding or not (or no longer) old enough."""
    if type(older_than_seconds) is not int or older_than_seconds < MIN_STALE_SECONDS:
        raise ValueError("A stale reservation must be at least one hour old.")
    R = FinanceReservation
    created = session.scalar(select(R.created_at).where(R.id == reservation_id, R.state == "reserved"))
    if created is None:
        return None
    if created.tzinfo is None:  # SQLite returns naive UTC.
        created = created.replace(tzinfo=now.tzinfo)
    if created >= now - timedelta(seconds=older_than_seconds):
        return None
    resolved = finance_ledger.settle_at_reservation(session, reservation_id, now)
    if resolved is None:
        return None
    scope, amount = resolved
    session.add(FinanceOperatorAction(id=uuid.uuid4(), action="resolve_stale", scope=scope, reason=None,
                                      reservation_id=reservation_id, charged_microusd=amount, performed_at=now))
    session.flush()
    return amount


def clear_latch(session, scope, expected_reason, now):
    """Remove the unsafe latch on `scope` only if its reason is the one the
    operator names, and record the action. Returns False when there is no
    such latch (nothing changes)."""
    if scope not in SCOPES or expected_reason not in LATCH_REASONS:
        raise ValueError("Unknown latch scope or reason.")
    L = FinanceUnsafeLatch
    found = session.execute(select(L.reason, L.reservation_id).where(L.scope == scope).with_for_update()).first()
    if found is None or found[0] != expected_reason:
        return False
    session.execute(delete(L).where(L.scope == scope, L.reason == expected_reason))
    session.add(FinanceOperatorAction(id=uuid.uuid4(), action="clear_latch", scope=scope, reason=expected_reason,
                                      reservation_id=found[1], charged_microusd=0, performed_at=now))
    session.flush()
    return True
