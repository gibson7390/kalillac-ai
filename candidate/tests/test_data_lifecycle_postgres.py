"""Usage-record cleanup on real disposable PostgreSQL 16 after the operator transition (data lifecycle,
item 3): it runs as the runtime login under E1a's final grants, deletes exactly the rows older than the
kept window for every account, touches nothing else, keeps the owner report's window, tolerates
concurrent runs and the quota's own writes, and a repeated run deletes nothing."""
# ruff: noqa: F811  (fixtures imported from other test modules are used as parameters)
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from kalillac_accounts.usage_retention import MIN_KEEP_DAYS, clean_usage
from kalillac_db.models import AccountUsageDaily, AccountUsageSlot
from test_v1_lifecycle_postgres import EMAIL, PASSWORDS, live, post, writers  # noqa: F401
from test_v1_transition_postgres import world  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import data_lifecycle  # noqa: E402
import owner_report  # noqa: E402

AGES = (0, 1, 3, 7, 9, 10, 30, 400)          # days before today
OTHER = "second.account@example.test"


def register(w, email):
    assert post(w, "/api/account/register", {"email": email}).status_code == 202
    assert post(w, "/api/account/register/confirm", {"token": w.token(), "password": PASSWORDS[0]}).status_code == 200
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT id FROM kalillac.users WHERE email = :e"), {"e": email})


def today(w):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT (now() AT TIME ZONE 'UTC')::date"))


def seed(w, users):
    day0 = today(w)
    with w.runtime_factory() as s, s.begin():                     # the runtime login writes these, as the quota does
        for user in users:
            for age in AGES:
                day = day0 - timedelta(days=age)
                s.add(AccountUsageDaily(user_id=user, usage_date=day, successful_chats=age + 1,
                                        request_chars=100, response_chars=200))
                for slot in range(2):
                    s.add(AccountUsageSlot(user_id=user, usage_date=day, slot=slot, state="completed",
                                           completed_date=day,
                                           expires_at=datetime.now(timezone.utc) + timedelta(minutes=15)))
    return day0


def remaining(w):
    with w.admin_engine.connect() as c:
        daily = c.execute(text("SELECT user_id, usage_date FROM kalillac.account_usage_daily")).all()
        slots = c.execute(text("SELECT user_id, usage_date, slot FROM kalillac.account_usage_slots")).all()
    return daily, slots


def accounts_snapshot(w):
    with w.admin_engine.connect() as c:
        return c.execute(text("SELECT id, email, password_hash, updated_at FROM kalillac.users ORDER BY id")).all()


def test_usage_cleanup_runs_as_the_runtime_login_and_deletes_exactly_the_old_rows(live):
    w = live
    users = [register(w, EMAIL), register(w, OTHER)]
    day0 = seed(w, users)
    before_accounts = accounts_snapshot(w)
    keep = 9
    dry = clean_usage(lambda: w.runtime_factory(), keep_days=keep)
    old = sum(age > keep for age in AGES)                                    # 10, 30 and 400 days old
    assert dry == {"applied": False, "keep_days": keep, "kept_from": (day0 - timedelta(days=keep)).isoformat(),
                   "daily_rows": 2 * old, "slot_rows": 2 * 2 * old}
    daily, slots = remaining(w)
    assert len(daily) == 2 * len(AGES) and len(slots) == 2 * 2 * len(AGES)  # a dry run deletes nothing
    done = clean_usage(lambda: w.runtime_factory(), keep_days=keep, apply=True)
    assert done["deleted_daily_rows"] == dry["daily_rows"] and done["deleted_slot_rows"] == dry["slot_rows"]
    daily, slots = remaining(w)
    cutoff = day0 - timedelta(days=keep)
    assert all(day >= cutoff for _, day in daily) and all(day >= cutoff for _, day, _ in slots)
    assert {user for user, _ in daily} == set(users) and len(daily) == 2 * sum(age <= keep for age in AGES)
    # Nothing else changed, and no account table was written.
    assert accounts_snapshot(w) == before_accounts and w.names["app"] not in writers(w)
    again = clean_usage(lambda: w.runtime_factory(), keep_days=keep, apply=True)
    assert (again["deleted_daily_rows"], again["deleted_slot_rows"]) == (0, 0)
    # The owner report still reads its seven days.
    out = io.StringIO()
    assert owner_report.run(["report", "--json"], factory=w.runtime_factory, out=out) == 0
    usage = json.loads(out.getvalue())["sections"]["usage"]
    assert usage["today"]["chats"] >= 1 and "unavailable" not in json.dumps(usage)


@pytest.mark.parametrize("keep", [-1, 0, 1, MIN_KEEP_DAYS - 1, "9", 9.0])
def test_a_window_shorter_than_the_live_and_reported_days_is_refused(live, keep):
    w = live
    with pytest.raises(ValueError):
        clean_usage(lambda: w.runtime_factory(), keep_days=keep, apply=True)


def test_concurrent_cleanups_and_todays_quota_writes_do_not_conflict(live):
    w = live
    users = [register(w, EMAIL), register(w, OTHER)]
    day0 = seed(w, users)

    def todays_write(i):
        with w.runtime_factory() as s, s.begin():
            s.add(AccountUsageSlot(user_id=users[i % 2], usage_date=day0, slot=10 + i, state="pending",
                                   expires_at=datetime.now(timezone.utc) + timedelta(minutes=15)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = [pool.submit(clean_usage, lambda: w.runtime_factory(), keep_days=MIN_KEEP_DAYS, apply=True)
                for _ in range(4)]
        writes = [pool.submit(todays_write, i) for i in range(8)]
        reports = [f.result() for f in runs]
        [f.result() for f in writes]
    old = 2 * sum(age > MIN_KEEP_DAYS for age in AGES)
    assert sum(r["deleted_daily_rows"] for r in reports) == old           # each old row deleted exactly once
    daily, slots = remaining(w)
    assert len([s for s in slots if s[1] == day0]) == 2 * 2 + 8          # today's rows, including the new ones


def test_the_operator_command_needs_the_deployment_confirmed_and_reports_counts_only(live):
    w = live
    users = [register(w, EMAIL)]
    seed(w, users)
    env = {"KALILLAC_DEPLOYMENT_ID": "kalillac-test"}

    def command(*extra):
        out = io.StringIO()
        code = data_lifecycle.main(["usage", "--keep-days", "9", *extra], environ=env, out=out,
                                   factory=lambda: w.runtime_factory())
        return code, json.loads(out.getvalue())
    code, refused = command("--apply", "--confirm-deployment", "production")
    assert code == 2 and refused["result"] == "refused"
    assert len(remaining(w)[0]) == len(AGES)
    code, done = command("--apply", "--confirm-deployment", "kalillac-test")
    assert code == 0 and done["deleted_daily_rows"] == 3 and len(remaining(w)[0]) == len(AGES) - 3
    assert EMAIL not in json.dumps(done) and str(users[0]) not in json.dumps(done)
