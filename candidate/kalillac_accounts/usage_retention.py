"""Deleting old per-account usage records (data lifecycle, item 3).

`account_usage_daily` (per account and UTC day: successful responses and the character counts of
requests and replies, never their text) and `account_usage_slots` (one reservation row per response)
are written by the quota and never pruned. This removes rows older than an explicitly configured number
of days (`keep_days`, required, no default; the owner's decision D-11), as the runtime login, which owns
these writes. It is run by an operator command (tools/data_lifecycle.py usage); the application never
calls it.

* keep_days is at least MIN_KEEP_DAYS: today and yesterday are live (a response reserved before midnight
  completes after it), and the owner report reads the last 7 days.
* "Today" is the database's UTC date, the same clock the quota uses.
* One transaction, fixed lock and statement timeouts; rows are chosen by date only, so concurrent runs and
  the quota's work on today's rows never conflict, and a repeated run deletes nothing more.
* Dry run (apply=False) counts exactly what would go and deletes nothing. Reports carry counts only.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import delete, func, select, text

from kalillac_db.models import AccountUsageDaily, AccountUsageSlot

MIN_KEEP_DAYS = 8
LOCK_TIMEOUT = "5s"
STATEMENT_TIMEOUT = "60s"


def clean_usage(session_factory, *, keep_days: int, apply: bool = False, today: date | None = None) -> dict:
    if type(keep_days) is not int or keep_days < MIN_KEEP_DAYS:
        raise ValueError(f"keep_days must be an explicit whole number of days, at least {MIN_KEEP_DAYS}")
    with session_factory() as session, session.begin():
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
            today = today or session.scalar(text("SELECT (now() AT TIME ZONE 'UTC')::date"))
        if today is None:
            raise ValueError("today is required without PostgreSQL")
        cutoff = today - timedelta(days=keep_days)
        old_daily = AccountUsageDaily.usage_date < cutoff
        old_slots = AccountUsageSlot.usage_date < cutoff
        counts = {"daily_rows": session.scalar(select(func.count()).select_from(AccountUsageDaily).where(old_daily)),
                  "slot_rows": session.scalar(select(func.count()).select_from(AccountUsageSlot).where(old_slots))}
        if apply:
            counts["deleted_daily_rows"] = session.execute(delete(AccountUsageDaily).where(old_daily)).rowcount
            counts["deleted_slot_rows"] = session.execute(delete(AccountUsageSlot).where(old_slots)).rowcount
    return {"applied": apply, "keep_days": keep_days, "kept_from": cutoff.isoformat(), **counts}
