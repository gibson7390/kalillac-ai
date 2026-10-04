"""Billing concurrency and idempotency.

Checkout: one persisted attempt per account, sent to Stripe as the
idempotency key, so concurrent requests and crash retries resolve to one
Checkout Session. Webhooks: every reconciliation locks the account row
before fetching Stripe's current subscription, so two events for one
account cannot commit snapshots out of order.

Concurrency tests use a file-backed SQLite database with real separate
connections; its BEGIN IMMEDIATE write lock serializes the transactions the
way PostgreSQL's per-account SELECT ... FOR UPDATE does. The PostgreSQL
lock statement itself is asserted from its compiled SQL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import threading
import uuid

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

import kalillac_billing.service as service
from kalillac_billing.gateway import BillingProviderError
from kalillac_billing.service import (
    CHECKOUT_RETRY_WINDOW,
    checkout_idempotency_key,
    process_webhook,
    start_checkout,
)
from kalillac_db.models import (
    AccountBilling,
    AccountEntitlement,
    Base,
    StripeWebhookEvent,
    User,
)
from kalillac_db.repositories.billing import (
    account_lock_statement,
    get_or_create_billing,
)
from kalillac_db.repositories.entitlements import create_default_entitlement

from account_test_db import (
    make_session_factory,
    make_sqlite_engine,
    make_sqlite_file_engine,
)
from test_billing import PAID_PRICE, FakeGateway, _subscription


URLS = dict(
    success_url="https://kalillac.com/billing/success",
    cancel_url="https://kalillac.com/billing/cancel",
)


# --- fixtures ----------------------------------------------------------------------


def _add_user(factory, email):
    user_id = uuid.uuid4()

    with factory() as session, session.begin():
        session.add(User(id=user_id, email=email, password_hash="h"))
        session.flush()
        create_default_entitlement(session, user_id)

    return user_id


@pytest.fixture
def file_db(tmp_path):
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)

    yield make_session_factory(engine)

    engine.dispose()


@pytest.fixture
def memory_db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)

    yield make_session_factory(engine)

    engine.dispose()


@pytest.fixture
def gateway():
    return FakeGateway()


def _checkout(user_id, gateway, factory):
    return start_checkout(
        user_id,
        gateway=gateway,
        session_factory=factory,
        price_id=PAID_PRICE,
        **URLS,
    )


def _billing(factory, user_id):
    with factory() as session:
        return session.get(AccountBilling, user_id)


def _count(factory, model):
    with factory() as session:
        return session.scalar(select(func.count()).select_from(model))


def _in_threads(*targets):
    results = [None] * len(targets)
    errors = []

    def run(index, target):
        try:
            results[index] = target()
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(index, target))
        for index, target in enumerate(targets)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    return results


# --- Checkout idempotency -------------------------------------------------------------


def test_concurrent_checkouts_share_one_attempt_and_session(file_db, gateway):
    user_id = _add_user(file_db, "a@example.com")
    # Both requests must be inside Stripe creation at the same moment.
    gateway.create_barrier = threading.Barrier(2)

    first, second = _in_threads(
        lambda: _checkout(user_id, gateway, file_db),
        lambda: _checkout(user_id, gateway, file_db),
    )

    keys = {call["idempotency_key"] for call in gateway.checkout_calls}
    billing = _billing(file_db, user_id)

    assert len(gateway.checkout_calls) == 2
    # Same persisted attempt -> same idempotency key -> one Stripe session.
    assert keys == {checkout_idempotency_key(user_id, billing.checkout_attempt_id)}
    assert len(gateway.sessions_by_key) == 1
    assert first.outcome == second.outcome == "url"
    assert first.url == second.url
    assert billing.stripe_checkout_session_id == "cs_test_1"


def test_many_concurrent_checkouts_never_create_two_sessions(file_db, gateway):
    user_id = _add_user(file_db, "a@example.com")

    results = _in_threads(*[lambda: _checkout(user_id, gateway, file_db)] * 8)

    assert len(gateway.sessions_by_key) == 1
    assert {result.url for result in results} == {
        "https://checkout.stripe.com/c/pay/cs_test_1"
    }


def test_retry_after_crash_reuses_the_same_stripe_session(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    gateway.crash_after_create = True

    with pytest.raises(RuntimeError):
        _checkout(user_id, gateway, memory_db)

    # Stripe created a session, but Kalillac never stored its id.
    assert len(gateway.sessions_by_key) == 1
    pending = _billing(memory_db, user_id)
    assert pending.checkout_attempt_id is not None
    assert pending.stripe_checkout_session_id is None

    result = _checkout(user_id, gateway, memory_db)

    first_key, retry_key = (c["idempotency_key"] for c in gateway.checkout_calls)
    assert first_key == retry_key
    assert len(gateway.sessions_by_key) == 1
    assert result.url == "https://checkout.stripe.com/c/pay/cs_test_1"
    assert _billing(memory_db, user_id).stripe_checkout_session_id == "cs_test_1"


def test_unknown_outcome_failure_keeps_the_attempt(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    gateway.fail_provider = True

    with pytest.raises(BillingProviderError):
        _checkout(user_id, gateway, memory_db)

    attempt = _billing(memory_db, user_id).checkout_attempt_id
    gateway.fail_provider = False
    _checkout(user_id, gateway, memory_db)

    keys = [call["idempotency_key"] for call in gateway.checkout_calls]
    assert keys == [checkout_idempotency_key(user_id, attempt)] * 2


def test_provider_rejection_keeps_the_attempt(memory_db, gateway):
    # Even an outright rejection does not prove that no earlier request
    # under this key succeeded, so the attempt and its key survive.
    user_id = _add_user(memory_db, "a@example.com")
    gateway.fail_provider = True

    with pytest.raises(BillingProviderError):
        _checkout(user_id, gateway, memory_db)

    attempt = _billing(memory_db, user_id).checkout_attempt_id
    assert attempt is not None

    gateway.fail_provider = False
    _checkout(user_id, gateway, memory_db)

    first_key, second_key = (c["idempotency_key"] for c in gateway.checkout_calls)
    assert first_key == second_key == checkout_idempotency_key(user_id, attempt)


def test_open_pending_checkout_is_reused(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")

    first = _checkout(user_id, gateway, memory_db)
    second = _checkout(user_id, gateway, memory_db)

    assert first.url == second.url
    assert len(gateway.checkout_calls) == 1
    assert gateway.checkout_retrievals == ["cs_test_1"]


def test_completed_pending_checkout_returns_billing_processing(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    _checkout(user_id, gateway, memory_db)
    gateway.set_checkout_status("cs_test_1", "complete", subscription_id="sub_1")

    result = _checkout(user_id, gateway, memory_db)

    assert result.outcome == "billing_processing"
    assert result.url is None
    assert len(gateway.checkout_calls) == 1
    assert len(gateway.sessions_by_key) == 1


def test_expired_pending_checkout_permits_a_new_attempt(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    _checkout(user_id, gateway, memory_db)
    old_attempt = _billing(memory_db, user_id).checkout_attempt_id
    gateway.set_checkout_status("cs_test_1", "expired")

    result = _checkout(user_id, gateway, memory_db)

    billing = _billing(memory_db, user_id)
    assert result.url == "https://checkout.stripe.com/c/pay/cs_test_2"
    assert billing.checkout_attempt_id != old_attempt
    assert billing.stripe_checkout_session_id == "cs_test_2"
    assert gateway.checkout_calls[-1]["idempotency_key"] == checkout_idempotency_key(
        user_id, billing.checkout_attempt_id
    )


def test_crash_recovered_session_that_expired_is_replaced(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    gateway.crash_after_create = True

    with pytest.raises(RuntimeError):
        _checkout(user_id, gateway, memory_db)

    # The idempotent replay returns the original session, now expired.
    gateway.set_checkout_status("cs_test_1", "expired")

    result = _checkout(user_id, gateway, memory_db)

    assert result.url == "https://checkout.stripe.com/c/pay/cs_test_2"
    assert len(gateway.sessions_by_key) == 2


def test_completed_session_of_an_ended_subscription_allows_resubscribing(
    memory_db, gateway
):
    user_id = _add_user(memory_db, "a@example.com")
    _checkout(user_id, gateway, memory_db)
    gateway.set_checkout_status("cs_test_1", "complete", subscription_id="sub_1")

    with memory_db() as session, session.begin():
        billing = session.get(AccountBilling, user_id)
        billing.stripe_subscription_id = "sub_1"
        billing.subscription_status = "canceled"

    result = _checkout(user_id, gateway, memory_db)

    assert result.outcome == "url"
    assert result.url == "https://checkout.stripe.com/c/pay/cs_test_2"


def test_different_accounts_have_independent_attempts(file_db, gateway):
    user_a = _add_user(file_db, "a@example.com")
    user_b = _add_user(file_db, "b@example.com")

    result_a, result_b = _in_threads(
        lambda: _checkout(user_a, gateway, file_db),
        lambda: _checkout(user_b, gateway, file_db),
    )

    assert result_a.url != result_b.url
    assert len(gateway.sessions_by_key) == 2
    assert (
        _billing(file_db, user_a).checkout_attempt_id
        != _billing(file_db, user_b).checkout_attempt_id
    )


def test_paid_reconciliation_clears_the_pending_attempt(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    _checkout(user_id, gateway, memory_db)
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    _deliver(memory_db, gateway, "evt_1", "checkout.session.completed", user=user_id)

    billing = _billing(memory_db, user_id)
    assert billing.subscription_status == "active"
    assert billing.checkout_attempt_id is None
    assert billing.stripe_checkout_session_id is None
    assert billing.checkout_session_expires_at is None

    assert _checkout(user_id, gateway, memory_db).outcome == "already_subscribed"


def test_checkout_never_grants_paid(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    _checkout(user_id, gateway, memory_db)
    gateway.set_checkout_status("cs_test_1", "complete", subscription_id="sub_1")
    _checkout(user_id, gateway, memory_db)

    with memory_db() as session:
        assert session.get(AccountEntitlement, user_id).tier == "free"


# --- webhook serialization -------------------------------------------------------------


def _deliver(factory, gateway, event_id, event_type, *, user=None,
             subscription="sub_1", customer="cus_1"):
    payload = json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "subscription": subscription,
            "customer": customer,
            "user": str(user) if user else None,
        }
    ).encode()

    return process_webhook(
        payload,
        "valid",
        gateway=gateway,
        session_factory=factory,
        paid_price_id=PAID_PRICE,
    ).result


def test_account_lock_is_select_for_update_on_the_user_row():
    sql = " ".join(
        str(
            account_lock_statement(uuid.uuid4()).compile(
                dialect=postgresql.dialect()
            )
        ).split()
    )

    assert sql.startswith("SELECT kalillac.users.id FROM kalillac.users")
    assert "WHERE kalillac.users.id =" in sql
    assert sql.endswith("FOR UPDATE")


def test_reconciliation_locks_before_fetching_stripe_truth(
    memory_db, gateway, monkeypatch
):
    user_id = _add_user(memory_db, "a@example.com")
    order = []
    original_lock = service.lock_account
    original_retrieve = gateway.retrieve_subscription

    def tracking_lock(session, locked_user_id):
        order.append(("lock", locked_user_id))
        return original_lock(session, locked_user_id)

    def tracking_retrieve(subscription_id):
        order.append(("retrieve", subscription_id))
        return original_retrieve(subscription_id)

    monkeypatch.setattr(service, "lock_account", tracking_lock)
    monkeypatch.setattr(gateway, "retrieve_subscription", tracking_retrieve)
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    assert _deliver(
        memory_db, gateway, "evt_1", "customer.subscription.created", user=user_id
    ) == "processed"
    assert order == [("lock", user_id), ("retrieve", "sub_1")]


def test_concurrent_events_cannot_commit_stale_stripe_state(file_db, gateway):
    user_id = _add_user(file_db, "a@example.com")
    truth = {"status": "active"}
    first_fetched = threading.Event()
    release_first = threading.Event()
    original_retrieve = gateway.retrieve_subscription

    def retrieve(subscription_id):
        state = _subscription(user_id, status=truth["status"])
        gateway.retrieve_calls.append(subscription_id)

        if not first_fetched.is_set():
            first_fetched.set()
            # Hold the older snapshot (and the account lock) while Stripe's
            # truth changes and a second event arrives.
            assert release_first.wait(timeout=20)

        return state

    gateway.retrieve_subscription = retrieve
    results = {}

    def first():
        results["first"] = _deliver(
            file_db, gateway, "evt_old", "customer.subscription.updated", user=user_id
        )

    def second():
        results["second"] = _deliver(
            file_db, gateway, "evt_new", "customer.subscription.deleted", user=user_id
        )

    thread_one = threading.Thread(target=first)
    thread_one.start()
    assert first_fetched.wait(timeout=20)

    truth["status"] = "canceled"
    thread_two = threading.Thread(target=second)
    thread_two.start()

    # The second event cannot fetch Stripe while the first holds the lock.
    thread_two.join(timeout=1.0)
    assert thread_two.is_alive()
    assert gateway.retrieve_calls == ["sub_1"]

    release_first.set()
    thread_one.join(timeout=30)
    thread_two.join(timeout=30)

    gateway.retrieve_subscription = original_retrieve

    assert results == {"first": "processed", "second": "processed"}
    # The later reconciliation fetched the newer truth and committed last.
    assert gateway.retrieve_calls == ["sub_1", "sub_1"]
    assert _billing(file_db, user_id).subscription_status == "canceled"

    with file_db() as session:
        assert session.get(AccountEntitlement, user_id).tier == "free"


def test_simultaneous_duplicate_deliveries_reconcile_once(file_db, gateway):
    user_id = _add_user(file_db, "a@example.com")
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    results = _in_threads(
        lambda: _deliver(
            file_db, gateway, "evt_dup", "customer.subscription.created", user=user_id
        ),
        lambda: _deliver(
            file_db, gateway, "evt_dup", "customer.subscription.created", user=user_id
        ),
    )

    assert sorted(results) == ["duplicate", "processed"]
    assert gateway.retrieve_calls == ["sub_1"]
    assert _count(file_db, StripeWebhookEvent) == 1


def test_duplicate_seen_only_under_the_lock_is_clean(memory_db, gateway, monkeypatch):
    # Both deliveries saw the event as absent before locking; the second
    # must resolve as a duplicate under the lock, not as a uniqueness error.
    user_id = _add_user(memory_db, "a@example.com")
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    assert _deliver(
        memory_db, gateway, "evt_dup", "customer.subscription.created", user=user_id
    ) == "processed"

    checks = []
    original = service.webhook_event_processed

    def stale_first_check(session, event_id):
        checks.append(event_id)
        # The pre-lock check reads a stale "absent"; later checks are real.
        return False if len(checks) == 1 else original(session, event_id)

    monkeypatch.setattr(service, "webhook_event_processed", stale_first_check)

    assert _deliver(
        memory_db, gateway, "evt_dup", "customer.subscription.created", user=user_id
    ) == "duplicate"
    assert len(checks) == 2
    assert gateway.retrieve_calls == ["sub_1"]
    assert _count(memory_db, StripeWebhookEvent) == 1


def test_failed_authoritative_fetch_changes_nothing(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    gateway.fail_provider = True

    with pytest.raises(BillingProviderError):
        _deliver(memory_db, gateway, "evt_1", "customer.subscription.created",
                 user=user_id)

    assert _count(memory_db, StripeWebhookEvent) == 0
    assert _billing(memory_db, user_id) is None

    with memory_db() as session:
        assert session.get(AccountEntitlement, user_id).tier == "free"


def test_different_accounts_reconcile_independently(file_db, gateway):
    user_a = _add_user(file_db, "a@example.com")
    user_b = _add_user(file_db, "b@example.com")
    gateway.subscriptions["sub_a"] = _subscription(
        user_a, subscription_id="sub_a", customer_id="cus_a"
    )
    gateway.subscriptions["sub_b"] = _subscription(
        user_b, subscription_id="sub_b", customer_id="cus_b", status="trialing"
    )

    results = _in_threads(
        lambda: _deliver(file_db, gateway, "evt_a", "customer.subscription.created",
                         user=user_a, subscription="sub_a", customer="cus_a"),
        lambda: _deliver(file_db, gateway, "evt_b", "customer.subscription.created",
                         user=user_b, subscription="sub_b", customer="cus_b"),
    )

    assert results == ["processed", "processed"]
    assert _billing(file_db, user_a).subscription_status == "active"
    assert _billing(file_db, user_b).subscription_status == "trialing"


# --- same-key conflicts keep the attempt ------------------------------------------------


def _set_account_customer(factory, user_id, customer_id):
    with factory() as session, session.begin():
        get_or_create_billing(session, user_id).stripe_customer_id = customer_id


def _keys(gateway):
    return [call["idempotency_key"] for call in gateway.checkout_calls]


def _wait_until(predicate, timeout=20):
    import time

    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_in_progress_same_key_conflict_keeps_the_attempt(file_db, gateway):
    user_id = _add_user(file_db, "a@example.com")
    gateway.emulate_in_progress_conflicts = True
    gateway.hold_create = threading.Event()
    first_result = {}

    def first():
        first_result["value"] = _checkout(user_id, gateway, file_db)

    first_thread = threading.Thread(target=first)
    first_thread.start()
    _wait_until(lambda: gateway.in_progress)

    attempt = _billing(file_db, user_id).checkout_attempt_id

    # Stripe answers 409 while the first same-key request is in progress.
    for _ in range(2):
        with pytest.raises(BillingProviderError) as raised:
            _checkout(user_id, gateway, file_db)

        # The attempt survives; no new key is minted.
        assert _billing(file_db, user_id).checkout_attempt_id == attempt

    assert [kind for kind, _ in gateway.conflicts] == ["in_progress"] * 2

    gateway.hold_create.set()
    first_thread.join(timeout=30)

    retry = _checkout(user_id, gateway, file_db)

    assert set(_keys(gateway)) == {checkout_idempotency_key(user_id, attempt)}
    assert len(gateway.sessions_by_key) == 1
    assert retry.url == first_result["value"].url


def test_conflict_then_crash_retry_resolves_to_the_same_session(file_db, gateway):
    user_id = _add_user(file_db, "a@example.com")
    gateway.emulate_in_progress_conflicts = True
    gateway.hold_create = threading.Event()
    gateway.crash_after_create = True
    crashed = []

    def first():
        try:
            _checkout(user_id, gateway, file_db)
        except RuntimeError:
            crashed.append(True)

    first_thread = threading.Thread(target=first)
    first_thread.start()
    _wait_until(lambda: gateway.in_progress)

    with pytest.raises(BillingProviderError):
        _checkout(user_id, gateway, file_db)

    # The first request's Stripe call completes, then Kalillac crashes
    # before storing the session id.
    gateway.hold_create.set()
    first_thread.join(timeout=30)
    assert crashed == [True]
    assert _billing(file_db, user_id).stripe_checkout_session_id is None

    result = _checkout(user_id, gateway, file_db)

    assert len(set(_keys(gateway))) == 1
    assert len(gateway.sessions_by_key) == 1
    assert result.url == "https://checkout.stripe.com/c/pay/cs_test_1"
    assert _billing(file_db, user_id).stripe_checkout_session_id == "cs_test_1"


def test_no_error_ever_clears_an_unresolved_attempt(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    _set_account_customer(memory_db, user_id, "cus_A")
    gateway.fail_provider = True

    for _ in range(5):
        with pytest.raises(BillingProviderError):
            _checkout(user_id, gateway, memory_db)

    billing = _billing(memory_db, user_id)
    # One key throughout, and every pinned value is still in place.
    assert len(set(_keys(gateway))) == 1
    assert billing.checkout_attempt_id is not None
    assert billing.checkout_customer_id == "cus_A"
    assert billing.checkout_attempt_created_at is not None
    assert billing.checkout_price_id == PAID_PRICE


# --- attempt-pinned customer ----------------------------------------------------------


def test_new_attempt_snapshots_the_current_customer(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    _set_account_customer(memory_db, user_id, "cus_A")

    _checkout(user_id, gateway, memory_db)

    assert _billing(memory_db, user_id).checkout_customer_id == "cus_A"
    assert gateway.checkout_calls[0]["customer_id"] == "cus_A"


def test_attempt_without_customer_snapshots_none(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")

    _checkout(user_id, gateway, memory_db)

    billing = _billing(memory_db, user_id)
    assert billing.checkout_attempt_id is not None
    assert billing.checkout_customer_id is None
    assert gateway.checkout_calls[0]["customer_id"] is None


@pytest.mark.parametrize("original_customer", ["cus_A", None])
def test_crash_recovery_with_changed_customer_returns_the_original_session(
    memory_db, gateway, original_customer
):
    user_id = _add_user(memory_db, "a@example.com")
    if original_customer:
        _set_account_customer(memory_db, user_id, original_customer)
    gateway.crash_after_create = True

    with pytest.raises(RuntimeError):
        _checkout(user_id, gateway, memory_db)

    # Meanwhile another operation changes the account's Stripe customer.
    _set_account_customer(memory_db, user_id, "cus_B")

    result = _checkout(user_id, gateway, memory_db)

    first_call, retry_call = gateway.checkout_calls
    # Identical key AND identical parameters: Stripe replays the original.
    assert first_call == retry_call
    assert retry_call["customer_id"] == original_customer
    assert gateway.conflicts == []
    assert len(gateway.sessions_by_key) == 1
    assert result.url == "https://checkout.stripe.com/c/pay/cs_test_1"

    billing = _billing(memory_db, user_id)
    assert billing.checkout_customer_id == original_customer
    assert billing.stripe_customer_id == "cus_B"


def test_changed_customer_does_not_alter_a_pending_attempt(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    _set_account_customer(memory_db, user_id, "cus_A")
    gateway.fail_provider = True

    with pytest.raises(BillingProviderError):
        _checkout(user_id, gateway, memory_db)

    _set_account_customer(memory_db, user_id, "cus_B")
    gateway.fail_provider = False
    _checkout(user_id, gateway, memory_db)

    assert [c["customer_id"] for c in gateway.checkout_calls] == ["cus_A", "cus_A"]
    assert len(set(_keys(gateway))) == 1


def test_parameter_mismatch_would_be_caught(memory_db, gateway):
    # Control: the fake really rejects a reused key with other parameters,
    # so the pinning tests above are meaningful.
    gateway.create_checkout_session(
        price_id=PAID_PRICE, customer_id="cus_A", kalillac_user_id="u",
        idempotency_key="k", **URLS,
    )

    with pytest.raises(BillingProviderError) as raised:
        gateway.create_checkout_session(
            price_id=PAID_PRICE, customer_id="cus_B", kalillac_user_id="u",
            idempotency_key="k", **URLS,
        )

    assert gateway.conflicts == [("parameter_mismatch", "k")]


def test_expired_attempt_is_replaced_with_a_fresh_snapshot(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    _set_account_customer(memory_db, user_id, "cus_A")
    _checkout(user_id, gateway, memory_db)
    old_attempt = _billing(memory_db, user_id).checkout_attempt_id

    _set_account_customer(memory_db, user_id, "cus_B")
    gateway.set_checkout_status("cs_test_1", "expired")
    _checkout(user_id, gateway, memory_db)

    billing = _billing(memory_db, user_id)
    assert billing.checkout_attempt_id != old_attempt
    assert billing.checkout_customer_id == "cus_B"
    assert gateway.checkout_calls[-1]["customer_id"] == "cus_B"
    assert gateway.conflicts == []


def test_reconciled_attempt_clears_the_snapshot(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    _set_account_customer(memory_db, user_id, "cus_1")
    _checkout(user_id, gateway, memory_db)
    assert _billing(memory_db, user_id).checkout_customer_id == "cus_1"
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    _deliver(memory_db, gateway, "evt_1", "checkout.session.completed", user=user_id)

    billing = _billing(memory_db, user_id)
    assert billing.checkout_attempt_id is None
    assert billing.checkout_customer_id is None
    assert billing.stripe_checkout_session_id is None


def test_accounts_snapshot_their_own_customers(file_db, gateway):
    user_a = _add_user(file_db, "a@example.com")
    user_b = _add_user(file_db, "b@example.com")
    _set_account_customer(file_db, user_a, "cus_A")

    _in_threads(
        lambda: _checkout(user_a, gateway, file_db),
        lambda: _checkout(user_b, gateway, file_db),
    )

    assert _billing(file_db, user_a).checkout_customer_id == "cus_A"
    assert _billing(file_db, user_b).checkout_customer_id is None
    calls = {c["kalillac_user_id"]: c["customer_id"] for c in gateway.checkout_calls}
    assert calls == {str(user_a): "cus_A", str(user_b): None}


# --- recovery window and pinned configuration ----------------------------------------


T0 = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)

ORIGINAL_CONFIG = dict(
    price_id=PAID_PRICE,
    success_url="https://kalillac.com/billing/success",
    cancel_url="https://kalillac.com/billing/cancel",
)

CHANGED_CONFIG = dict(
    price_id="price_newprice",
    success_url="https://kalillac.com/v2/billing/success",
    cancel_url="https://kalillac.com/v2/billing/cancel",
)


def _checkout_at(user_id, gateway, factory, at, config=ORIGINAL_CONFIG):
    return start_checkout(
        user_id,
        gateway=gateway,
        session_factory=factory,
        clock=lambda: at,
        **config,
    )


def _unknown_outcome(user_id, gateway, factory, at=T0):
    """Stripe created the session; Kalillac never learned its id."""

    gateway.crash_after_create = True

    with pytest.raises(RuntimeError):
        _checkout_at(user_id, gateway, factory, at)

    billing = _billing(factory, user_id)
    assert billing.stripe_checkout_session_id is None
    return billing.checkout_attempt_id


def test_attempt_creation_time_is_persisted_once(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    gateway.fail_provider = True

    for offset in (0, 1, 5):
        with pytest.raises(BillingProviderError):
            _checkout_at(user_id, gateway, memory_db, T0 + timedelta(hours=offset))

    created_at = _billing(memory_db, user_id).checkout_attempt_created_at
    assert created_at.replace(tzinfo=timezone.utc) == T0


@pytest.mark.parametrize(
    "elapsed",
    [CHECKOUT_RETRY_WINDOW, CHECKOUT_RETRY_WINDOW + timedelta(seconds=1),
     timedelta(hours=48), timedelta(days=30)],
)
def test_unknown_outcome_after_window_holds_without_any_create(
    memory_db, gateway, elapsed
):
    user_id = _add_user(memory_db, "a@example.com")
    attempt = _unknown_outcome(user_id, gateway, memory_db)
    calls_before = len(gateway.checkout_calls)

    result = _checkout_at(user_id, gateway, memory_db, T0 + elapsed)

    assert result.outcome == "billing_processing"
    assert result.url is None
    # No create call, no new key, and the attempt is untouched.
    assert len(gateway.checkout_calls) == calls_before
    assert _billing(memory_db, user_id).checkout_attempt_id == attempt
    assert len(gateway.sessions_by_key) == 1


def test_failed_provider_call_after_window_also_holds(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    gateway.fail_provider = True

    with pytest.raises(BillingProviderError):
        _checkout_at(user_id, gateway, memory_db, T0)

    gateway.fail_provider = False
    result = _checkout_at(user_id, gateway, memory_db, T0 + timedelta(hours=30))

    assert result.outcome == "billing_processing"
    assert len(gateway.checkout_calls) == 1


def test_retry_within_window_sends_the_original_key_and_parameters(
    memory_db, gateway
):
    user_id = _add_user(memory_db, "a@example.com")
    _set_account_customer(memory_db, user_id, "cus_A")
    attempt = _unknown_outcome(user_id, gateway, memory_db)

    result = _checkout_at(
        user_id,
        gateway,
        memory_db,
        T0 + CHECKOUT_RETRY_WINDOW - timedelta(seconds=1),
    )

    original, retry = gateway.checkout_calls
    assert retry == original
    assert retry["idempotency_key"] == checkout_idempotency_key(user_id, attempt)
    assert gateway.conflicts == []
    assert result.url == "https://checkout.stripe.com/c/pay/cs_test_1"


def test_configuration_change_does_not_alter_a_recovering_attempt(
    memory_db, gateway
):
    user_id = _add_user(memory_db, "a@example.com")
    _unknown_outcome(user_id, gateway, memory_db)

    # Price and redirect URLs change before the crash-recovery retry.
    result = _checkout_at(
        user_id,
        gateway,
        memory_db,
        T0 + timedelta(hours=1),
        config=CHANGED_CONFIG,
    )

    original, retry = gateway.checkout_calls
    assert retry == original
    assert retry["price_id"] == ORIGINAL_CONFIG["price_id"]
    assert retry["success_url"] == ORIGINAL_CONFIG["success_url"]
    assert retry["cancel_url"] == ORIGINAL_CONFIG["cancel_url"]
    assert gateway.conflicts == []
    assert len(gateway.sessions_by_key) == 1
    assert result.url == "https://checkout.stripe.com/c/pay/cs_test_1"


def test_new_attempt_after_confirmed_expiry_uses_current_configuration(
    memory_db, gateway
):
    user_id = _add_user(memory_db, "a@example.com")
    _checkout_at(user_id, gateway, memory_db, T0)
    gateway.set_checkout_status("cs_test_1", "expired")

    result = _checkout_at(
        user_id,
        gateway,
        memory_db,
        T0 + timedelta(hours=30),
        config=CHANGED_CONFIG,
    )

    billing = _billing(memory_db, user_id)
    assert result.url == "https://checkout.stripe.com/c/pay/cs_test_2"
    assert gateway.checkout_calls[-1]["price_id"] == CHANGED_CONFIG["price_id"]
    assert billing.checkout_price_id == CHANGED_CONFIG["price_id"]
    assert billing.checkout_attempt_created_at.replace(
        tzinfo=timezone.utc
    ) == T0 + timedelta(hours=30)


def test_known_completed_session_with_delayed_webhook_never_rechecks_out(
    memory_db, gateway
):
    user_id = _add_user(memory_db, "a@example.com")
    _checkout_at(user_id, gateway, memory_db, T0)
    gateway.set_checkout_status("cs_test_1", "complete", subscription_id="sub_1")

    for elapsed in (timedelta(minutes=5), timedelta(hours=30), timedelta(days=7)):
        result = _checkout_at(user_id, gateway, memory_db, T0 + elapsed)
        assert result.outcome == "billing_processing"

    # Every check re-read Stripe; no second Checkout was ever created.
    assert len(gateway.checkout_calls) == 1
    assert gateway.checkout_retrievals == ["cs_test_1"] * 3


def test_confirmed_expired_session_is_replaced_safely(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    _checkout_at(user_id, gateway, memory_db, T0)
    old_attempt = _billing(memory_db, user_id).checkout_attempt_id
    gateway.set_checkout_status("cs_test_1", "expired")

    result = _checkout_at(user_id, gateway, memory_db, T0 + timedelta(hours=2))

    billing = _billing(memory_db, user_id)
    assert result.url == "https://checkout.stripe.com/c/pay/cs_test_2"
    assert billing.checkout_attempt_id != old_attempt
    assert billing.stripe_checkout_session_id == "cs_test_2"


def test_unrecognized_session_state_is_held_not_replaced(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    _checkout_at(user_id, gateway, memory_db, T0)
    gateway.set_checkout_status("cs_test_1", None)

    result = _checkout_at(user_id, gateway, memory_db, T0 + timedelta(hours=1))

    assert result.outcome == "billing_processing"
    assert len(gateway.checkout_calls) == 1
    assert _billing(memory_db, user_id).stripe_checkout_session_id == "cs_test_1"


def test_replayed_stale_open_response_is_rechecked_before_returning(
    memory_db, gateway
):
    user_id = _add_user(memory_db, "a@example.com")
    _unknown_outcome(user_id, gateway, memory_db)
    # Stripe's saved response says "open", but the session has expired.
    gateway.set_checkout_status("cs_test_1", "expired")

    result = _checkout_at(user_id, gateway, memory_db, T0 + timedelta(hours=1))

    # The stale replayed URL was not returned; the confirmed-expired
    # session was replaced.
    assert result.url == "https://checkout.stripe.com/c/pay/cs_test_2"
    assert "cs_test_1" in gateway.checkout_retrievals


def test_errors_cannot_clear_an_unresolved_earlier_outcome(memory_db, gateway):
    user_id = _add_user(memory_db, "a@example.com")
    attempt = _unknown_outcome(user_id, gateway, memory_db)

    # Every later request fails, within and beyond the window.
    gateway.fail_provider = True
    for hours in (1, 5, 22):
        with pytest.raises(BillingProviderError):
            _checkout_at(user_id, gateway, memory_db, T0 + timedelta(hours=hours))

    assert _checkout_at(
        user_id, gateway, memory_db, T0 + timedelta(hours=40)
    ).outcome == "billing_processing"

    billing = _billing(memory_db, user_id)
    assert billing.checkout_attempt_id == attempt
    assert billing.checkout_price_id == PAID_PRICE
    assert {c["idempotency_key"] for c in gateway.checkout_calls} == {
        checkout_idempotency_key(user_id, attempt)
    }
