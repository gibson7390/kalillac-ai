"""Atomic service-pool admission; short transactions, no provider work."""
from sqlalchemy import select
from ..models import ProviderUsagePool


def locked_pool(session, provider, limit, now):
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    start = int(now.timestamp()) // limit.period_seconds * limit.period_seconds
    session.execute(insert(ProviderUsagePool).values(provider=provider, period_start=start, period_seconds=limit.period_seconds, total_limit=limit.total, anonymous_limit=limit.anonymous, total_attempts=0, anonymous_attempts=0).on_conflict_do_nothing(index_elements=["provider"]))
    row = session.scalar(select(ProviderUsagePool).where(ProviderUsagePool.provider == provider).with_for_update())
    return row


def reserve(session, provider, limit, now, anonymous):
    from kalillac_routing.provider_controls import CeilingBusy, CeilingUnavailable
    clock = now
    now = clock()
    row = locked_pool(session, provider, limit, now)
    now = clock()  # Fresh clock after the shared row lock.
    # Do not let disagreeing workers/configuration silently reset a pool.
    if (row.period_seconds, row.total_limit, row.anonymous_limit) != (limit.period_seconds, limit.total, limit.anonymous):
        raise CeilingUnavailable()
    start = int(now.timestamp()) // limit.period_seconds * limit.period_seconds
    if start < row.period_start:
        raise CeilingUnavailable()  # Backwards authoritative clock, fail closed.
    if start > row.period_start:
        row.period_start, row.total_attempts, row.anonymous_attempts = start, 0, 0
    if row.total_attempts >= limit.total or (anonymous and row.anonymous_attempts >= limit.anonymous):
        raise CeilingBusy()
    row.total_attempts += 1
    row.anonymous_attempts += int(anonymous)
    session.flush()
    return start, now


def release(session, lease):
    row = session.scalar(select(ProviderUsagePool).where(ProviderUsagePool.provider == lease.provider).with_for_update())
    # A late local rejection cannot refund another accounting period.
    if row is not None and row.period_start == lease.period_start:
        row.total_attempts -= 1
        row.anonymous_attempts -= int(lease.anonymous)
        session.flush()
