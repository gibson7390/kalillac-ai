"""Operator subscription reconciliation (service.reconcile_account).

Recovery uses only the account's own stored evidence (a linked
subscription, or a stored Checkout Session), takes the same account lock as
Checkout and webhooks, applies the same policy as webhooks, and rolls back
on every failure. Every test injects a fake gateway; nothing contacts
Stripe.

SQLite cannot hold a row lock, so the lock tests install AccountLocks: a
per-account lock taken inside lock_account and held to the end of that
transaction, standing in for PostgreSQL's SELECT ... FOR UPDATE on the user
row. Waiting is observed through the lock's own waiter count, never a fixed
sleep; timeouts only stop a hung test. The real PostgreSQL behavior is
checked by tools/verify_billing_postgres.py.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import inspect
import json
import logging
import os
import threading
import uuid

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.exc import OperationalError

import kalillac_billing.service as service
from kalillac_billing.gateway import BillingProviderError
from kalillac_billing.router import build_billing_router
from kalillac_billing.service import (
    PAID_ENTITLEMENT_GRACE,
    ReconciliationConflict,
    ReconciliationRefused,
    SubscriptionStateInvalid,
    process_webhook,
    reconcile_account,
    start_checkout,
)
from kalillac_db.models import (
    AccountBilling,
    AccountEntitlement,
    Base,
    StripeWebhookEvent,
    User,
)
from kalillac_db.repositories.billing import get_or_create_billing
from kalillac_db.repositories.entitlements import (
    create_default_entitlement,
    set_entitlement_tier,
)

from account_test_db import (
    make_session_factory,
    make_sqlite_engine,
    make_sqlite_file_engine,
)
from test_billing import (
    ALLOWED_PRICES,
    CONFIG,
    PAID_EXPIRY,
    PAID_PRICE,
    PERIOD_END,
    SETTINGS,
    FakeGateway,
    _subscription,
)


TIMEOUT = 20
URLS = dict(
    success_url="https://kalillac.ai/billing/success",
    cancel_url="https://kalillac.ai/billing/cancel",
)
OLD_PERIOD_END = PERIOD_END - timedelta(days=30)


# --- fixtures and helpers ------------------------------------------------------------


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)

    yield make_session_factory(engine)

    engine.dispose()


@pytest.fixture
def file_db(tmp_path):
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)

    yield make_session_factory(engine)

    engine.dispose()


@pytest.fixture
def wal_db(tmp_path):
    """File SQLite in WAL mode with deferred transactions: one account's
    write can commit while another account's transaction is still open, so
    only the per-account lock decides who waits."""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'main.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None
        dbapi_connection.execute(
            "ATTACH DATABASE ? AS kalillac", (str(tmp_path / "kalillac.db"),)
        )
        dbapi_connection.execute("PRAGMA kalillac.journal_mode=WAL")
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine, "begin")
    def _on_begin(connection):
        connection.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)

    yield make_session_factory(engine)

    engine.dispose()


@pytest.fixture
def gateway():
    return FakeGateway()


def _add_user(factory, email="person@example.com"):
    user_id = uuid.uuid4()

    with factory() as session, session.begin():
        session.add(User(id=user_id, email=email, password_hash="h"))
        session.flush()
        create_default_entitlement(session, user_id)

    return user_id


def _link(factory, user_id, *, subscription="sub_1", customer="cus_1",
          status="active", price=PAID_PRICE, period_end=OLD_PERIOD_END,
          tier="paid"):
    """Stored state as an earlier verified webhook would have left it."""

    with factory() as session, session.begin():
        billing = get_or_create_billing(session, user_id)
        billing.stripe_customer_id = customer
        billing.stripe_subscription_id = subscription
        billing.stripe_price_id = price
        billing.subscription_status = status
        billing.current_period_end = period_end
        set_entitlement_tier(
            session,
            user_id,
            tier,
            source="stripe",
            expires_at=period_end + PAID_ENTITLEMENT_GRACE if tier == "paid" else None,
        )


def _stored_checkout(factory, gateway, user_id):
    """A real stored Checkout Session, created through start_checkout."""

    result = start_checkout(
        user_id,
        gateway=gateway,
        session_factory=factory,
        price_id=PAID_PRICE,
        **URLS,
    )
    assert result.outcome == "url"
    return gateway.sessions_by_key[next(iter(gateway.sessions_by_key))]


def _reconcile(factory, gateway, user_id):
    return reconcile_account(
        user_id,
        gateway=gateway,
        session_factory=factory,
        allowed_price_ids=ALLOWED_PRICES,
    ).result


def _utc(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _snapshot(factory, user_id):
    with factory() as session:
        billing = session.get(AccountBilling, user_id)
        entitlement = session.get(AccountEntitlement, user_id)

        return {
            "billing": None if billing is None else {
                c.name: getattr(billing, c.name)
                for c in AccountBilling.__table__.columns
            },
            "entitlement": None if entitlement is None else {
                c.name: getattr(entitlement, c.name)
                for c in AccountEntitlement.__table__.columns
            },
        }


def _entitlement(factory, user_id):
    with factory() as session:
        row = session.get(AccountEntitlement, user_id)
        return row.tier, row.source, _utc(row.expires_at)


def _billing(factory, user_id):
    with factory() as session:
        return session.get(AccountBilling, user_id)


def _count(factory, model, *where):
    with factory() as session:
        return session.scalar(select(func.count()).select_from(model).where(*where))


def _assert_checkout_cleared(billing):
    assert billing.stripe_checkout_session_id is None
    assert billing.checkout_session_expires_at is None
    assert billing.checkout_attempt_id is None
    assert billing.checkout_attempt_created_at is None
    assert billing.checkout_customer_id is None
    assert billing.checkout_price_id is None
    assert billing.checkout_success_url is None
    assert billing.checkout_cancel_url is None


class AccountLocks:
    """Per-account locks taken by lock_account and held until that
    transaction ends: PostgreSQL's row lock, on SQLite."""

    def __init__(self, monkeypatch):
        self._guard = threading.Condition()
        self._locks = {}
        self.waiting = Counter()
        self.requested = []
        real = service.lock_account

        def lock_account(session, user_id):
            with self._guard:
                lock = self._locks.setdefault(user_id, threading.Lock())
                self.waiting[user_id] += 1
                self.requested.append(user_id)
                self._guard.notify_all()

            acquired = lock.acquire(timeout=TIMEOUT)

            with self._guard:
                self.waiting[user_id] -= 1
                self._guard.notify_all()

            assert acquired, "account lock wait timed out"
            held = {"open": True}

            def release(_session, transaction):
                if held["open"] and transaction.parent is None:
                    held["open"] = False
                    lock.release()

            event.listen(session, "after_transaction_end", release)
            return real(session, user_id)

        monkeypatch.setattr(service, "lock_account", lock_account)

    def wait_for_waiter(self, user_id):
        """Block until a transaction is waiting for this account's lock."""

        with self._guard:
            assert self._guard.wait_for(
                lambda: self.waiting[user_id] >= 1, timeout=TIMEOUT
            ), "no transaction waited for the account lock"


class Held:
    """Wrap a gateway method: the first call fetches its result, then
    signals `entered` and waits for `release` while its caller still holds
    the account lock."""

    def __init__(self, method):
        self._method = method
        self._first = threading.Lock()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, *args):
        result = self._method(*args)
        self.calls += 1

        if self._first.acquire(blocking=False):
            self.entered.set()
            assert self.release.wait(timeout=TIMEOUT), "hold never released"

        return result


def _worker(target):
    box = {}

    def run():
        try:
            box["value"] = target()
        except Exception as exc:  # reported by _join
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _join(thread, box):
    thread.join(timeout=TIMEOUT)
    assert not thread.is_alive(), "worker did not finish"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _webhook(factory, gateway, event_id, *, subscription="sub_1",
             customer="cus_1", user=None,
             event_type="customer.subscription.updated"):
    payload = json.dumps({
        "id": event_id,
        "type": event_type,
        "subscription": subscription,
        "customer": customer,
        "user": str(user) if user else None,
    }).encode()

    return process_webhook(
        payload,
        "valid",
        gateway=gateway,
        session_factory=factory,
        allowed_price_ids=ALLOWED_PRICES,
        livemode=False,
    ).result


# --- the operation's surface -----------------------------------------------------------


def test_reconcile_accepts_only_a_user_id_never_a_stripe_id():
    parameters = inspect.signature(reconcile_account).parameters

    assert list(parameters) == [
        "user_id", "gateway", "session_factory", "allowed_price_ids",
    ]


@pytest.mark.parametrize("value", ["sub_1", str(uuid.uuid4()), None])
def test_reconcile_refuses_anything_but_a_uuid_before_any_work(db, gateway, value):
    with pytest.raises(TypeError):
        reconcile_account(
            value,
            gateway=gateway,
            session_factory=db,
            allowed_price_ids=ALLOWED_PRICES,
        )

    assert gateway.retrieve_calls == gateway.checkout_retrievals == []


def test_no_http_route_reaches_reconciliation(db, gateway):
    router = build_billing_router(
        config=CONFIG,
        settings=SETTINGS,
        gateway=gateway,
        session_factory=lambda: db,
    )

    paths = {route.path for route in router.routes}

    assert paths == {
        "/api/account/billing/checkout",
        "/api/account/billing/portal",
        "/api/account/billing",
        "/api/billing/stripe/webhook",
    }


# --- linked subscription ------------------------------------------------------------


@pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
def test_linked_paying_subscription_refreshes_billing_and_paid_expiry(db, gateway, status):
    user_id = _add_user(db)
    _link(db, user_id, status="incomplete", tier="free")
    gateway.subscriptions["sub_1"] = _subscription(user_id, status)

    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"

    billing = _billing(db, user_id)
    assert billing.subscription_status == status
    assert _utc(billing.current_period_end) == PERIOD_END
    assert _entitlement(db, user_id) == ("paid", "stripe", PAID_EXPIRY)
    # Exactly the linked subscription, and no synthetic webhook event.
    assert gateway.retrieve_calls == ["sub_1"]
    assert gateway.checkout_retrievals == []
    assert _count(db, StripeWebhookEvent) == 0


def test_linked_renewal_moves_the_bounded_expiry_forward(db, gateway):
    user_id = _add_user(db)
    _link(db, user_id)
    assert _entitlement(db, user_id)[2] == OLD_PERIOD_END + PAID_ENTITLEMENT_GRACE
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"
    assert _entitlement(db, user_id) == ("paid", "stripe", PAID_EXPIRY)


def test_linked_subscription_without_metadata_still_refreshes(db, gateway):
    # The stored link is the ownership evidence; as for webhooks.
    user_id = _add_user(db)
    _link(db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(None)

    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"
    assert _entitlement(db, user_id) == ("paid", "stripe", PAID_EXPIRY)


@pytest.mark.parametrize("status", ["canceled", "incomplete_expired"])
def test_linked_ended_subscription_becomes_free(db, gateway, status):
    user_id = _add_user(db)
    _link(db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(user_id, status)

    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"
    assert _billing(db, user_id).subscription_status == status
    assert _entitlement(db, user_id) == ("free", "stripe", None)


@pytest.mark.parametrize("status", ["unpaid", "paused", "incomplete", "a_future_status"])
def test_linked_nonpaying_subscription_becomes_free(db, gateway, status):
    user_id = _add_user(db)
    _link(db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(user_id, status)

    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"
    assert _entitlement(db, user_id) == ("free", "stripe", None)


def test_linked_disallowed_price_becomes_free(db, gateway):
    user_id = _add_user(db)
    _link(db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(user_id, price_id="price_notallowed")

    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"
    assert _billing(db, user_id).stripe_price_id == "price_notallowed"
    assert _entitlement(db, user_id) == ("free", "stripe", None)


def test_linked_cancel_at_period_end_stays_paid_until_the_bounded_expiry(db, gateway):
    user_id = _add_user(db)
    _link(db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(user_id, cancel_at_period_end=True)

    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"
    assert _billing(db, user_id).cancel_at_period_end is True
    assert _entitlement(db, user_id) == ("paid", "stripe", PAID_EXPIRY)


def test_provider_returning_another_subscription_is_refused(db, gateway):
    user_id = _add_user(db)
    _link(db, user_id)
    before = _snapshot(db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(user_id, subscription_id="sub_other")

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert refused.value.code == "subscription_state_unrecognized"
    assert _snapshot(db, user_id) == before


@pytest.mark.parametrize("status", [None, "", 7])
@pytest.mark.parametrize("path", ["linked", "checkout"])
def test_subscription_without_a_status_is_refused(db, gateway, path, status):
    user_id = _add_user(db)
    if path == "linked":
        _link(db, user_id)
    else:
        session = _stored_checkout(db, gateway, user_id)
        gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id, status)
    before = _snapshot(db, user_id)

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert refused.value.code == "subscription_state_unrecognized"
    assert _snapshot(db, user_id) == before


# --- stored Checkout Session -------------------------------------------------------


def test_completed_checkout_repairs_a_missed_webhook_and_links_it(db, gateway):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    assert _reconcile(db, gateway, user_id) == "checkout_recovered"

    billing = _billing(db, user_id)
    assert billing.stripe_subscription_id == "sub_1"
    assert billing.stripe_customer_id == "cus_1"
    assert billing.stripe_price_id == PAID_PRICE
    assert billing.subscription_status == "active"
    _assert_checkout_cleared(billing)
    assert _entitlement(db, user_id) == ("paid", "stripe", PAID_EXPIRY)
    assert gateway.checkout_retrievals == [session]
    assert gateway.retrieve_calls == ["sub_1"]
    assert _count(db, StripeWebhookEvent) == 0


def test_recovered_link_then_reconciles_like_any_other(db, gateway):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    assert _reconcile(db, gateway, user_id) == "checkout_recovered"

    # A rerun refreshes the now-linked subscription only.
    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"
    assert gateway.checkout_retrievals == [session]

    # The late webhook for the same subscription is an ordinary update.
    assert _webhook(db, gateway, "evt_late", user=user_id) == "processed"
    assert _entitlement(db, user_id) == ("paid", "stripe", PAID_EXPIRY)

    # And Checkout now reports the paying subscription.
    result = start_checkout(
        user_id, gateway=gateway, session_factory=db, price_id=PAID_PRICE, **URLS
    )
    assert result.outcome == "already_subscribed"


def test_completed_checkout_with_unpaid_subscription_links_it_as_free(db, gateway):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id, "incomplete")

    assert _reconcile(db, gateway, user_id) == "checkout_recovered"

    billing = _billing(db, user_id)
    assert billing.stripe_subscription_id == "sub_1"
    _assert_checkout_cleared(billing)
    assert _entitlement(db, user_id) == ("free", "stripe", None)
    # The unresolved subscription now blocks a second purchase.
    result = start_checkout(
        user_id, gateway=gateway, session_factory=db, price_id=PAID_PRICE, **URLS
    )
    assert result.outcome == "subscription_unresolved"


def test_completed_checkout_with_disallowed_price_never_grants_paid(db, gateway):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id, price_id="price_notallowed")

    assert _reconcile(db, gateway, user_id) == "checkout_recovered"
    assert _entitlement(db, user_id) == ("free", "stripe", None)


def test_resubscribe_after_an_ended_subscription_recovers_through_checkout(db, gateway):
    user_id = _add_user(db)
    _link(db, user_id, subscription="sub_old", status="canceled", tier="free")
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    assert _reconcile(db, gateway, user_id) == "checkout_recovered"

    billing = _billing(db, user_id)
    assert billing.stripe_subscription_id == "sub_1"
    _assert_checkout_cleared(billing)
    assert _entitlement(db, user_id) == ("paid", "stripe", PAID_EXPIRY)
    # The ended subscription is final at Stripe and is not re-fetched.
    assert gateway.retrieve_calls == ["sub_1"]


@pytest.mark.parametrize("metadata", [None, ""])
def test_completed_checkout_without_owner_metadata_is_refused(db, gateway, metadata):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id, kalillac_user_id=metadata)
    before = _snapshot(db, user_id)

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert refused.value.code == "owner_metadata_missing"
    assert _snapshot(db, user_id) == before
    assert _billing(db, user_id).stripe_checkout_session_id == session


@pytest.mark.parametrize("owner", ["other-user", "not-a-uuid", str(uuid.uuid4()).upper()])
def test_completed_checkout_naming_another_user_is_refused(db, gateway, owner):
    user_id = _add_user(db)
    other_id = _add_user(db, "other@example.com")
    metadata = str(other_id) if owner == "other-user" else owner
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id, kalillac_user_id=metadata)
    before, other_before = _snapshot(db, user_id), _snapshot(db, other_id)

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert refused.value.code == "owner_metadata_mismatch"
    assert _snapshot(db, user_id) == before
    assert _snapshot(db, other_id) == other_before


def test_open_checkout_returns_checkout_open_and_changes_nothing(db, gateway):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    before = _snapshot(db, user_id)

    assert _reconcile(db, gateway, user_id) == "checkout_open"
    assert _snapshot(db, user_id) == before
    assert gateway.checkout_retrievals == [session]
    assert gateway.retrieve_calls == []


def test_expired_checkout_clears_only_that_pending_attempt(db, gateway):
    user_id = _add_user(db)
    with db() as session, session.begin():
        get_or_create_billing(session, user_id).stripe_customer_id = "cus_1"
    session_id = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session_id, "expired")
    before = _snapshot(db, user_id)
    first_key = next(iter(gateway.sessions_by_key))

    assert _reconcile(db, gateway, user_id) == "checkout_expired"

    after = _snapshot(db, user_id)
    _assert_checkout_cleared(_billing(db, user_id))
    checkout_columns = {
        "stripe_checkout_session_id", "checkout_session_expires_at",
        "checkout_attempt_id", "checkout_attempt_created_at",
        "checkout_customer_id", "checkout_price_id",
        "checkout_success_url", "checkout_cancel_url",
    }
    changed = {
        name for name, value in after["billing"].items()
        if value != before["billing"][name]
    }
    assert changed <= checkout_columns | {"updated_at"}
    assert after["billing"]["stripe_customer_id"] == "cus_1"
    assert after["entitlement"] == before["entitlement"]
    assert gateway.retrieve_calls == []

    # A later Checkout begins safely, with a new attempt and key.
    result = start_checkout(
        user_id, gateway=gateway, session_factory=db, price_id=PAID_PRICE, **URLS
    )
    assert result.outcome == "url"
    assert len(gateway.sessions_by_key) == 2
    assert next(reversed(gateway.sessions_by_key)) != first_key


@pytest.mark.parametrize("status", [None, "processing", "unknown_status"])
def test_unrecognized_checkout_status_is_refused(db, gateway, status):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, status)
    before = _snapshot(db, user_id)

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert refused.value.code == "checkout_state_unrecognized"
    assert _snapshot(db, user_id) == before
    assert gateway.retrieve_calls == []


def test_completed_checkout_without_a_subscription_is_refused(db, gateway):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", None)
    before = _snapshot(db, user_id)

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert refused.value.code == "checkout_state_unrecognized"
    assert _snapshot(db, user_id) == before
    assert gateway.retrieve_calls == []


def test_provider_returning_another_checkout_session_is_refused(db, gateway):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    original = gateway.sessions_by_id[session]
    gateway.sessions_by_id[session] = type(original)(
        session_id="cs_other",
        status="complete",
        url=None,
        expires_at=original.expires_at,
        subscription_id="sub_1",
    )
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    before = _snapshot(db, user_id)

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert refused.value.code == "checkout_state_unrecognized"
    assert _snapshot(db, user_id) == before


def test_checkout_subscription_returned_under_another_id_is_refused(db, gateway):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id, subscription_id="sub_other")
    before = _snapshot(db, user_id)

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert refused.value.code == "subscription_state_unrecognized"
    assert _snapshot(db, user_id) == before


# --- nothing stored ---------------------------------------------------------------------


def test_no_billing_row_is_nothing_to_reconcile_without_stripe(db, gateway):
    user_id = _add_user(db)

    assert _reconcile(db, gateway, user_id) == "nothing_to_reconcile"
    assert _billing(db, user_id) is None
    assert gateway.retrieve_calls == gateway.checkout_retrievals == []
    assert gateway.checkout_calls == gateway.portal_calls == []


def test_billing_row_without_subscription_or_session_is_nothing_to_reconcile(db, gateway):
    user_id = _add_user(db)
    with db() as session, session.begin():
        billing = get_or_create_billing(session, user_id)
        billing.stripe_customer_id = "cus_1"
        # A pending attempt whose create never returned a session is not
        # Stripe evidence; it is left for Checkout's own retry rules.
        billing.checkout_attempt_id = uuid.uuid4()
    before = _snapshot(db, user_id)

    assert _reconcile(db, gateway, user_id) == "nothing_to_reconcile"
    assert _snapshot(db, user_id) == before
    assert gateway.retrieve_calls == gateway.checkout_retrievals == []


def test_missing_account_is_not_found_and_creates_nothing(db, gateway):
    user_id = uuid.uuid4()

    assert _reconcile(db, gateway, user_id) == "account_not_found"
    assert _count(db, AccountBilling) == 0
    assert _count(db, AccountEntitlement) == 0
    assert gateway.retrieve_calls == gateway.checkout_retrievals == []


# --- rollback -------------------------------------------------------------------------------


def _fail_subscription_retrieval(gateway):
    def failing(subscription_id):
        gateway.retrieve_calls.append(subscription_id)
        raise BillingProviderError()

    gateway.retrieve_subscription = failing


def test_provider_failure_on_the_linked_subscription_rolls_back(db, gateway):
    user_id = _add_user(db)
    _link(db, user_id)
    before = _snapshot(db, user_id)
    gateway.fail_provider = True

    with pytest.raises(BillingProviderError):
        _reconcile(db, gateway, user_id)

    assert _snapshot(db, user_id) == before


def test_provider_failure_on_the_checkout_session_rolls_back(db, gateway):
    user_id = _add_user(db)
    _stored_checkout(db, gateway, user_id)
    before = _snapshot(db, user_id)
    gateway.fail_provider = True

    with pytest.raises(BillingProviderError):
        _reconcile(db, gateway, user_id)

    assert _snapshot(db, user_id) == before


def test_provider_failure_after_a_completed_checkout_rolls_back(db, gateway):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    _fail_subscription_retrieval(gateway)
    before = _snapshot(db, user_id)

    with pytest.raises(BillingProviderError):
        _reconcile(db, gateway, user_id)

    assert gateway.retrieve_calls == ["sub_1"]
    assert _snapshot(db, user_id) == before
    assert _billing(db, user_id).stripe_checkout_session_id == session


def test_entitlement_write_failure_rolls_back_the_billing_link(db, gateway, monkeypatch):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    before = _snapshot(db, user_id)

    def failing(*args, **kwargs):
        raise RuntimeError("entitlement write failed")

    monkeypatch.setattr(service, "set_entitlement_tier", failing)

    with pytest.raises(RuntimeError):
        _reconcile(db, gateway, user_id)

    # The billing upsert and the checkout clear were in the same transaction.
    assert _snapshot(db, user_id) == before
    assert _billing(db, user_id).stripe_subscription_id is None


@pytest.mark.parametrize(
    "period_end",
    [None, datetime(2026, 12, 1), "2026-12-01T00:00:00Z"],
    ids=["missing", "naive", "not-a-datetime"],
)
def test_invalid_paid_period_end_rolls_back_checkout_recovery(db, gateway, period_end):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id, current_period_end=period_end)
    before = _snapshot(db, user_id)

    with pytest.raises(SubscriptionStateInvalid):
        _reconcile(db, gateway, user_id)

    assert _snapshot(db, user_id) == before


def test_invalid_paid_period_end_keeps_the_linked_paid_state(db, gateway):
    user_id = _add_user(db)
    _link(db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(user_id, current_period_end=None)
    before = _snapshot(db, user_id)

    with pytest.raises(SubscriptionStateInvalid):
        _reconcile(db, gateway, user_id)

    assert _snapshot(db, user_id) == before
    # Retryable: once Stripe reports a period end, it applies.
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"
    assert _entitlement(db, user_id) == ("paid", "stripe", PAID_EXPIRY)


def test_free_subscription_needs_no_period_end(db, gateway):
    user_id = _add_user(db)
    _link(db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(
        user_id, "canceled", current_period_end=None
    )

    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"
    assert _entitlement(db, user_id) == ("free", "stripe", None)


# --- ownership and linkage -----------------------------------------------------------


def _two_accounts_and_completed_checkout(db, gateway):
    user_id = _add_user(db)
    other_id = _add_user(db, "other@example.com")
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    return user_id, other_id


def test_customer_linked_to_another_account_rolls_back(db, gateway):
    user_id, other_id = _two_accounts_and_completed_checkout(db, gateway)
    _link(db, other_id, subscription="sub_other", customer="cus_1")
    before, other_before = _snapshot(db, user_id), _snapshot(db, other_id)

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert refused.value.code == "ownership_mismatch"
    assert _snapshot(db, user_id) == before
    assert _snapshot(db, other_id) == other_before


def test_subscription_linked_to_another_account_rolls_back(db, gateway):
    user_id, other_id = _two_accounts_and_completed_checkout(db, gateway)
    _link(db, other_id, subscription="sub_1", customer="cus_other")
    before, other_before = _snapshot(db, user_id), _snapshot(db, other_id)

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert refused.value.code == "ownership_mismatch"
    assert _snapshot(db, user_id) == before
    assert _snapshot(db, other_id) == other_before


def test_account_linked_to_a_different_customer_rolls_back(db, gateway):
    user_id = _add_user(db)
    with db() as session, session.begin():
        get_or_create_billing(session, user_id).stripe_customer_id = "cus_mine"
    session_id = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session_id, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id, customer_id="cus_1")
    before = _snapshot(db, user_id)

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert refused.value.code == "ownership_mismatch"
    assert _snapshot(db, user_id) == before


def test_linked_subscription_now_claimed_by_another_account_rolls_back(db, gateway):
    user_id = _add_user(db)
    other_id = _add_user(db, "other@example.com")
    _link(db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(other_id)
    before = _snapshot(db, user_id)

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert refused.value.code == "ownership_mismatch"
    assert _snapshot(db, user_id) == before


def test_different_nonterminal_subscriptions_remain_a_conflict(db):
    # The one policy both paths use refuses to choose between two live
    # subscriptions, and writes nothing.
    user_id = _add_user(db)
    _link(db, user_id, subscription="sub_1", status="active")
    before = _snapshot(db, user_id)

    with pytest.raises(ReconciliationConflict):
        with db() as session, session.begin():
            assert service.lock_account(session, user_id)
            service._reconcile_subscription(
                session,
                user_id,
                _subscription(user_id, subscription_id="sub_2", customer_id="cus_1"),
                allowed_price_ids=ALLOWED_PRICES,
            )

    assert _snapshot(db, user_id) == before


def test_webhook_conflict_still_rolls_back_through_the_shared_policy(db, gateway):
    user_id = _add_user(db)
    _link(db, user_id, subscription="sub_1", status="active")
    gateway.subscriptions["sub_2"] = _subscription(user_id, subscription_id="sub_2")
    before = _snapshot(db, user_id)

    with pytest.raises(ReconciliationConflict):
        _webhook(db, gateway, "evt_conflict", subscription="sub_2", user=user_id,
                 event_type="customer.subscription.created")

    assert _snapshot(db, user_id) == before
    assert _count(db, StripeWebhookEvent) == 0


def test_stored_checkout_never_replaces_a_live_linked_subscription(db, gateway):
    # A live linked subscription and a stored session for another one: the
    # operator refreshes the linked subscription and never links the other.
    user_id = _add_user(db)
    _link(db, user_id, subscription="sub_1", status="unpaid", tier="free")
    with db() as session, session.begin():
        billing = get_or_create_billing(session, user_id)
        billing.stripe_checkout_session_id = "cs_test_stray"
    gateway.subscriptions["sub_1"] = _subscription(user_id, "unpaid")
    gateway.subscriptions["sub_2"] = _subscription(user_id, subscription_id="sub_2")

    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"

    billing = _billing(db, user_id)
    assert billing.stripe_subscription_id == "sub_1"
    assert gateway.retrieve_calls == ["sub_1"]
    assert gateway.checkout_retrievals == []
    assert _entitlement(db, user_id) == ("free", "stripe", None)


# --- locking and concurrency ---------------------------------------------------------


def test_reconcile_locks_the_account_before_reading_or_calling_stripe(db, gateway, monkeypatch):
    user_id = _add_user(db)
    _link(db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    order = []
    real_lock, real_get = service.lock_account, service.get_billing
    real_retrieve = gateway.retrieve_subscription

    monkeypatch.setattr(service, "lock_account",
                        lambda s, u: order.append(("lock", u)) or real_lock(s, u))
    monkeypatch.setattr(service, "get_billing",
                        lambda s, u: order.append(("read", u)) or real_get(s, u))
    gateway.retrieve_subscription = (
        lambda sid: order.append(("stripe", sid)) or real_retrieve(sid)
    )

    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"
    assert order[0] == ("lock", user_id)
    assert order[1] == ("read", user_id)
    assert ("stripe", "sub_1") in order
    # Only this account's lock is ever requested.
    assert {u for kind, u in order if kind == "lock"} == {user_id}


def test_webhook_holding_the_lock_makes_reconciliation_wait(wal_db, gateway, monkeypatch):
    locks = AccountLocks(monkeypatch)
    user_id = _add_user(wal_db)
    _link(wal_db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    held = Held(gateway.retrieve_subscription)
    gateway.retrieve_subscription = held

    webhook = _worker(lambda: _webhook(wal_db, gateway, "evt_1", user=user_id))
    assert held.entered.wait(TIMEOUT)

    operator = _worker(lambda: _reconcile(wal_db, gateway, user_id))
    locks.wait_for_waiter(user_id)
    # The operator is parked on the lock: it has not called Stripe.
    assert held.calls == 1

    # Stripe's truth moves on before the webhook commits its older fetch.
    gateway.subscriptions["sub_1"] = _subscription(user_id, "canceled")
    held.release.set()

    assert _join(*webhook) == "processed"
    assert _join(*operator) == "subscription_reconciled"
    assert held.calls == 2
    # The operator read after the webhook committed, so the newer state wins.
    assert _billing(wal_db, user_id).subscription_status == "canceled"
    assert _entitlement(wal_db, user_id) == ("free", "stripe", None)
    assert _count(wal_db, StripeWebhookEvent) == 1


def test_reconciliation_holding_the_lock_makes_a_webhook_wait(wal_db, gateway, monkeypatch):
    locks = AccountLocks(monkeypatch)
    user_id = _add_user(wal_db)
    _link(wal_db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    held = Held(gateway.retrieve_subscription)
    gateway.retrieve_subscription = held

    operator = _worker(lambda: _reconcile(wal_db, gateway, user_id))
    assert held.entered.wait(TIMEOUT)

    webhook = _worker(lambda: _webhook(wal_db, gateway, "evt_1", user=user_id))
    locks.wait_for_waiter(user_id)
    assert held.calls == 1
    assert _count(wal_db, StripeWebhookEvent) == 0

    gateway.subscriptions["sub_1"] = _subscription(user_id, "past_due")
    held.release.set()

    assert _join(*operator) == "subscription_reconciled"
    assert _join(*webhook) == "processed"
    assert held.calls == 2
    assert _billing(wal_db, user_id).subscription_status == "past_due"
    assert _entitlement(wal_db, user_id) == ("paid", "stripe", PAID_EXPIRY)


def test_concurrent_reconciliations_of_one_checkout_link_it_once(wal_db, gateway, monkeypatch):
    locks = AccountLocks(monkeypatch)
    user_id = _add_user(wal_db)
    session = _stored_checkout(wal_db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    held = Held(gateway.retrieve_checkout_session)
    gateway.retrieve_checkout_session = held

    first = _worker(lambda: _reconcile(wal_db, gateway, user_id))
    assert held.entered.wait(TIMEOUT)
    second = _worker(lambda: _reconcile(wal_db, gateway, user_id))
    locks.wait_for_waiter(user_id)
    held.release.set()

    # The second saw the first's committed link and only refreshed it.
    assert _join(*first) == "checkout_recovered"
    assert _join(*second) == "subscription_reconciled"
    assert held.calls == 1
    assert _billing(wal_db, user_id).stripe_subscription_id == "sub_1"
    assert _entitlement(wal_db, user_id) == ("paid", "stripe", PAID_EXPIRY)


def test_different_accounts_do_not_block_each_other(wal_db, gateway, monkeypatch):
    locks = AccountLocks(monkeypatch)
    held_user = _add_user(wal_db, "held@example.com")
    free_user = _add_user(wal_db, "free@example.com")
    _stored_checkout(wal_db, gateway, held_user)
    _link(wal_db, free_user, subscription="sub_free", customer="cus_free")
    gateway.subscriptions["sub_free"] = _subscription(
        free_user, subscription_id="sub_free", customer_id="cus_free"
    )
    held = Held(gateway.retrieve_checkout_session)
    gateway.retrieve_checkout_session = held

    blocked = _worker(lambda: _reconcile(wal_db, gateway, held_user))
    assert held.entered.wait(TIMEOUT)

    # The other account completes while the first still holds its lock.
    other = _worker(lambda: _reconcile(wal_db, gateway, free_user))
    assert _join(*other) == "subscription_reconciled"
    assert blocked[0].is_alive()
    assert locks.waiting[free_user] == 0
    assert _entitlement(wal_db, free_user) == ("paid", "stripe", PAID_EXPIRY)

    held.release.set()
    assert _join(*blocked) == "checkout_open"
    assert set(locks.requested) == {held_user, free_user}


def test_account_deleted_before_reconciliation_leaves_no_orphans(file_db, gateway):
    user_id = _add_user(file_db)
    _link(file_db, user_id)
    gateway.subscriptions["sub_1"] = _subscription(user_id)

    with file_db() as session, session.begin():
        session.execute(delete(User).where(User.id == user_id))

    assert _reconcile(file_db, gateway, user_id) == "account_not_found"
    assert gateway.retrieve_calls == []
    assert _count(file_db, AccountBilling, AccountBilling.user_id == user_id) == 0
    assert _count(file_db, AccountEntitlement, AccountEntitlement.user_id == user_id) == 0


def test_account_deleted_during_reconciliation_leaves_no_orphans(file_db, gateway):
    user_id = _add_user(file_db)
    session_id = _stored_checkout(file_db, gateway, user_id)
    gateway.set_checkout_status(session_id, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    held = Held(gateway.retrieve_subscription)
    gateway.retrieve_subscription = held

    operator = _worker(lambda: _reconcile(file_db, gateway, user_id))
    assert held.entered.wait(TIMEOUT)

    deleting = threading.Event()

    def delete_account():
        deleting.set()
        with file_db() as session, session.begin():
            session.execute(delete(User).where(User.id == user_id))

    deleter = _worker(delete_account)
    assert deleting.wait(TIMEOUT)
    held.release.set()

    # Whichever commits second, the account's rows go with the user row.
    assert _join(*operator) == "checkout_recovered"
    _join(*deleter)
    assert _count(file_db, User, User.id == user_id) == 0
    assert _count(file_db, AccountBilling, AccountBilling.user_id == user_id) == 0
    assert _count(file_db, AccountEntitlement, AccountEntitlement.user_id == user_id) == 0


# --- privacy ------------------------------------------------------------------------------


IDENTIFIERS = ("person@example.com", "other@example.com", "sub_", "cus_",
               "cs_test", "price_", "sk_test", "whsec_", "failed")


def test_reconciliation_writes_nothing_to_output_or_logs(db, gateway, capsys, caplog,
                                                         monkeypatch):
    caplog.set_level(logging.DEBUG)
    user_id = _add_user(db)
    other_id = _add_user(db, "other@example.com")
    session = _stored_checkout(db, gateway, user_id)
    capsys.readouterr()

    def attempt(**subscription):
        gateway.set_checkout_status(session, "complete", "sub_1")
        gateway.subscriptions["sub_1"] = _subscription(user_id, **subscription)
        try:
            return _reconcile(db, gateway, user_id)
        except (ReconciliationRefused, SubscriptionStateInvalid,
                BillingProviderError) as exc:
            return exc

    attempt(kalillac_user_id=None)
    attempt(kalillac_user_id=str(other_id))
    attempt(current_period_end=None)
    gateway.fail_provider = True
    attempt()
    gateway.fail_provider = False

    def failing(*args, **kwargs):
        raise RuntimeError("entitlement write failed for person@example.com")

    monkeypatch.setattr(service, "set_entitlement_tier", failing)
    with pytest.raises(RuntimeError):
        attempt()
    monkeypatch.undo()

    assert attempt() == "checkout_recovered"
    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"
    assert _reconcile(db, gateway, other_id) == "nothing_to_reconcile"

    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for value in IDENTIFIERS + (str(user_id), str(other_id)):
        assert value not in logged


def test_refusals_carry_only_a_stable_code(db, gateway):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id, kalillac_user_id=None)

    with pytest.raises(ReconciliationRefused) as refused:
        _reconcile(db, gateway, user_id)

    assert str(refused.value) == refused.value.code == "owner_metadata_missing"
    assert refused.value.args == ("owner_metadata_missing",)


# --- bounded waiting -------------------------------------------------------------------


class _DriverTimeout(Exception):
    """A driver error shaped like psycopg's, with a revealing message."""

    def __init__(self, sqlstate):
        super().__init__(
            "canceling statement due to lock timeout for person@example.com "
            "sub_1 cus_1 cs_test_1"
        )
        self.sqlstate = sqlstate


def _database_timeout(sqlstate):
    return OperationalError(
        "SELECT kalillac.users.id FROM kalillac.users "
        "WHERE kalillac.users.id = %(id)s FOR UPDATE",
        {"id": "person@example.com"},
        _DriverTimeout(sqlstate),
    )


TIMEOUT_SQLSTATES = pytest.mark.parametrize(
    "sqlstate", ["55P03", "57014"], ids=["lock-timeout", "statement-timeout"]
)


class _RecordingSession:
    def __init__(self, dialect):
        self.executed = []
        self._bind = type("Bind", (), {"dialect": type("D", (), {"name": dialect})()})()

    def get_bind(self):
        return self._bind

    def execute(self, statement):
        self.executed.append(str(statement))


def test_operator_limits_are_fixed_and_transaction_local():
    assert service.OPERATOR_TIMEOUT_STATEMENTS == (
        "SET LOCAL lock_timeout = '5s'",
        "SET LOCAL statement_timeout = '30s'",
    )


def test_postgresql_transactions_always_receive_the_limits():
    session = _RecordingSession("postgresql")

    service._apply_operator_limits(session)

    assert session.executed == list(service.OPERATOR_TIMEOUT_STATEMENTS)


def test_an_unrecognized_database_is_refused_rather_than_unbounded():
    session = _RecordingSession("mysql")

    with pytest.raises(RuntimeError):
        service._apply_operator_limits(session)

    assert session.executed == []


def test_limits_precede_the_lock_and_the_lock_precedes_reads_stripe_and_writes(
    db, gateway, monkeypatch
):
    user_id = _add_user(db)
    session_id = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session_id, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    gateway.checkout_retrievals.clear()
    order = []

    def recorded(name, real):
        def wrapper(*args, **kwargs):
            order.append(name)
            return real(*args, **kwargs)
        return wrapper

    for name, kind in (
        ("_apply_operator_limits", "limits"),
        ("lock_account", "lock"),
        ("get_billing", "read"),
        ("upsert_billing_state", "write"),
        ("set_entitlement_tier", "write"),
    ):
        monkeypatch.setattr(service, name, recorded(kind, getattr(service, name)))
    gateway.retrieve_checkout_session = recorded("stripe", gateway.retrieve_checkout_session)
    gateway.retrieve_subscription = recorded("stripe", gateway.retrieve_subscription)

    assert _reconcile(db, gateway, user_id) == "checkout_recovered"

    assert order[:3] == ["limits", "lock", "read"]
    assert order.count("limits") == order.count("lock") == 1
    assert {"stripe", "write"} <= set(order[2:])


@TIMEOUT_SQLSTATES
def test_a_database_timeout_on_the_lock_is_busy_before_any_read_or_stripe_call(
    db, gateway, monkeypatch, sqlstate
):
    user_id = _add_user(db)
    session_id = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session_id, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    gateway.checkout_retrievals.clear()
    before = _snapshot(db, user_id)
    reads = []
    real_get = service.get_billing

    def timed_out(session, locked_user):
        raise _database_timeout(sqlstate)

    monkeypatch.setattr(service, "lock_account", timed_out)
    monkeypatch.setattr(service, "get_billing",
                        lambda s, u: reads.append(u) or real_get(s, u))

    with pytest.raises(service.ReconciliationBusy) as busy:
        _reconcile(db, gateway, user_id)

    # No driver message, SQL, or identifier travels with it.
    assert str(busy.value) == "" and busy.value.args == ()
    assert busy.value.__cause__ is None and busy.value.__suppress_context__
    assert reads == []
    assert gateway.checkout_retrievals == gateway.retrieve_calls == []
    assert _snapshot(db, user_id) == before


@TIMEOUT_SQLSTATES
def test_a_timeout_after_writes_rolls_back_billing_and_entitlement(
    db, gateway, monkeypatch, sqlstate
):
    user_id = _add_user(db)
    session_id = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session_id, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    before = _snapshot(db, user_id)

    def timed_out(*args, **kwargs):
        raise _database_timeout(sqlstate)

    # The billing upsert and the checkout clear have run when this fires.
    monkeypatch.setattr(service, "set_entitlement_tier", timed_out)

    with pytest.raises(service.ReconciliationBusy):
        _reconcile(db, gateway, user_id)

    assert _snapshot(db, user_id) == before
    assert _billing(db, user_id).stripe_subscription_id is None
    assert _billing(db, user_id).stripe_checkout_session_id == session_id


def test_other_database_errors_are_not_reported_as_busy(db, gateway, monkeypatch):
    user_id = _add_user(db)

    def failed(session, locked_user):
        raise _database_timeout("40001")

    monkeypatch.setattr(service, "lock_account", failed)

    with pytest.raises(OperationalError):
        _reconcile(db, gateway, user_id)


def test_webhooks_and_checkout_never_apply_the_operator_limits(db, gateway, monkeypatch):
    applied = []
    real = service._apply_operator_limits
    monkeypatch.setattr(service, "_apply_operator_limits",
                        lambda session: applied.append(1) or real(session))
    user_id = _add_user(db)

    assert start_checkout(
        user_id, gateway=gateway, session_factory=db, price_id=PAID_PRICE, **URLS
    ).outcome == "url"
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    assert _webhook(db, gateway, "evt_1", user=user_id,
                    event_type="customer.subscription.created") == "processed"
    assert applied == []

    assert _reconcile(db, gateway, user_id) == "subscription_reconciled"
    assert applied == [1]
