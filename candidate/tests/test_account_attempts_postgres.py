"""Per-account daily provider attempts (Codex F3) on real disposable PostgreSQL 16, after the operator transition:
migration 0026 is the release head the transition accepts, the runtime login counts and refunds attempts under
E1a's final grants by the database's UTC day, concurrent attempts for one account admit exactly its limit,
other accounts and the successful-response counts are untouched, and the usage cleanup still removes old rows."""
# ruff: noqa: F811  (fixtures imported from other test modules are used as parameters)
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import text

from kalillac_accounts.usage_retention import clean_usage
from kalillac_routing.provider_controls import AccountAttemptLimitReached, CeilingUnavailable, ProviderControls
from kalillac_routing.provider_limits import AccountAttemptLimit, ControlConfig, ProviderLimit
from test_data_lifecycle_postgres import OTHER, register, today
from test_v1_lifecycle_postgres import EMAIL, live, writers  # noqa: F401
from test_v1_transition_postgres import HEAD, world  # noqa: F401

FREE, PAID = 4, 6


def controls(w):
    config = ControlConfig({p: ProviderLimit(10_000, 10_000, 86_400) for p in ("openai", "brave", "tavily")}, None,
                           AccountAttemptLimit(FREE, PAID))
    return ProviderControls(config, session_factory=lambda: w.runtime_factory)


def counts(w):
    with w.admin_engine.connect() as c:
        return {(str(user), day): (attempts, chats) for user, day, attempts, chats in c.execute(text(
            "SELECT user_id, usage_date, provider_attempts, successful_chats FROM kalillac.account_usage_daily"))}


def test_the_release_head_carries_the_counter_and_the_runtime_login_counts_and_refunds(live):
    w = live
    with w.admin_engine.connect() as c:
        assert c.scalar(text("SELECT version_num FROM kalillac.alembic_version")) == HEAD \
            == "0026_account_provider_attempts"
        assert c.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = "
                             "'ck_account_usage_daily_provider_attempts_non_negative'")) \
            == "CHECK ((provider_attempts >= 0))"
    free, paid = register(w, EMAIL), register(w, OTHER)
    day = today(w)
    service = controls(w)
    for _ in range(FREE):
        service.reserve("openai", False, account=(free, False))
    with pytest.raises(AccountAttemptLimitReached):
        service.reserve("tavily", False, account=(free, False))
    leases = [service.reserve("openai", False, account=(paid, True)) for _ in range(PAID)]
    with pytest.raises(AccountAttemptLimitReached):
        service.reserve("openai", False, account=(paid, True))
    assert counts(w) == {(str(free), day): (FREE, 0), (str(paid), day): (PAID, 0)}
    # A never-dispatched attempt goes back to the account's day; a dispatched one stays counted.
    assert service.release(leases[0]) is True
    leases[1].dispatched = True
    assert service.release(leases[1]) is False
    assert counts(w)[(str(paid), day)] == (PAID - 1, 0)
    service.reserve("openai", False, account=(paid, True))
    assert counts(w)[(str(paid), day)] == (PAID, 0)
    # The runtime login wrote only the usage table (and the provider pool), never an account table.
    assert w.names["app"] not in writers(w)


def test_concurrent_attempts_for_one_account_admit_exactly_its_limit_and_spare_the_others(live):
    w = live
    racer, bystander = register(w, EMAIL), register(w, OTHER)
    day = today(w)
    barrier = threading.Barrier(16)

    def attempt(n):
        service = controls(w)
        barrier.wait(30)
        account = (racer, False) if n < 12 else (bystander, False)
        try:
            return account[0], service.reserve("openai", False, account=account) is not None
        except AccountAttemptLimitReached:
            return account[0], False
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(16)))
    assert sum(ok for user, ok in results if user == racer) == FREE
    assert sum(ok for user, ok in results if user == bystander) == 4
    assert counts(w) == {(str(racer), day): (FREE, 0), (str(bystander), day): (4, 0)}


def test_an_unknown_account_is_refused_and_counted_nowhere(live, capsys):
    import uuid
    w = live
    register(w, EMAIL)
    with pytest.raises(CeilingUnavailable):            # No such account: the foreign key refuses the row.
        controls(w).reserve("openai", False, account=(uuid.uuid4(), False))
    assert "PROVIDER_CEILING_DATABASE_FAILED IntegrityError" in capsys.readouterr().out
    assert counts(w) == {}


def test_the_usage_cleanup_removes_old_attempt_counts_with_their_rows(live):
    from datetime import timedelta
    from kalillac_db.models import AccountUsageDaily
    w = live
    user = register(w, EMAIL)
    day = today(w)
    with w.runtime_factory() as s, s.begin():
        s.add(AccountUsageDaily(user_id=user, usage_date=day - timedelta(days=40), provider_attempts=9))
    controls(w).reserve("openai", False, account=(user, False))
    report = clean_usage(lambda: w.runtime_factory(), keep_days=8, apply=True)
    assert report["deleted_daily_rows"] == 1 and counts(w) == {(str(user), day): (1, 0)}
