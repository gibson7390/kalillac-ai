"""Refunds and disputes: verified webhooks record billing cases with facts fetched from Stripe, for an
operator to review. No refund is issued and no entitlement changes because of a case.

Stripe is faked (no network); the database is a real SQLite schema from the models.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from kalillac_billing.gateway import BillingProviderError, ChargeState, DisputeState, WebhookEvent
from kalillac_billing.service import CASE_EVENT_TYPES, RELEVANT_EVENT_TYPES, CaseStateInvalid, process_webhook
from kalillac_db.models import AccountBilling, AccountEntitlement, BillingCase, Base, StripeWebhookEvent, User
from kalillac_db.models.account_entitlement import TIER_PAID
from account_test_db import make_session_factory, make_sqlite_engine
from test_billing import ALLOWED_PRICES, FakeGateway

import tools.billing_cases as tool  # noqa: E402  (candidate/ is the working directory under pytest)


class Gateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.charges, self.disputes, self.case_calls = {}, {}, []

    def retrieve_charge(self, charge_id):
        self.case_calls.append(("charge", charge_id))
        if self.fail_provider:
            raise BillingProviderError()
        return self.charges[charge_id]

    def retrieve_dispute(self, dispute_id):
        self.case_calls.append(("dispute", dispute_id))
        if self.fail_provider:
            raise BillingProviderError()
        return self.disputes[dispute_id]

    def parse_webhook(self, payload, signature):
        event = super().parse_webhook(payload, signature)
        return WebhookEvent(**{**event.__dict__, "object_id": json.loads(payload).get("object")})


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def gateway():
    return Gateway()


@pytest.fixture
def paid_account(db):
    """A paying account linked to Stripe customer cus_1."""
    with db() as session, session.begin():
        user = User(email="payer@example.com", password_hash="h")
        session.add(user)
        session.flush()
        session.add(AccountBilling(user_id=user.id, stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
                                   subscription_status="active", cancel_at_period_end=False))
        session.add(AccountEntitlement(user_id=user.id, tier=TIER_PAID,
                                       expires_at=datetime.now(timezone.utc) + timedelta(days=30), source="stripe"))
        return user.id


def deliver(db, gateway, event_id, event_type, obj, customer=None):
    payload = json.dumps({"id": event_id, "type": event_type, "object": obj, "customer": customer,
                          "livemode": False}).encode()
    return process_webhook(payload, "valid", gateway=gateway, session_factory=db,
                           allowed_price_ids=ALLOWED_PRICES, livemode=False)


def cases(db):
    with db() as session:
        return {case.provider_object_id: case for case in session.query(BillingCase).all()}


def charge(amount_refunded, refunded, customer="cus_1", amount=4900):
    return ChargeState(charge_id="ch_1", customer_id=customer, amount=amount, amount_refunded=amount_refunded,
                       currency="usd", refunded=refunded)


def dispute(status, reason="fraudulent", customer="cus_1"):
    return DisputeState(dispute_id="dp_1", charge_id="ch_1", customer_id=customer, amount=4900, currency="usd",
                        status=status, reason=reason)


def tier(db, user_id):
    with db() as session:
        return session.get(AccountEntitlement, user_id).tier


def test_refund_and_dispute_events_are_relevant_and_others_ignored(db, gateway):
    assert CASE_EVENT_TYPES <= RELEVANT_EVENT_TYPES
    assert {"charge.refunded", "charge.dispute.created", "charge.dispute.closed"} <= CASE_EVENT_TYPES
    assert deliver(db, gateway, "evt_0", "charge.succeeded", "ch_1").result == "ignored"
    assert gateway.case_calls == [] and cases(db) == {}


def test_a_refund_is_recorded_for_review_and_access_is_unchanged(db, gateway, paid_account, capsys):
    gateway.charges["ch_1"] = charge(4900, True)
    assert deliver(db, gateway, "evt_1", "charge.refunded", "ch_1", "cus_1").result == "case_recorded"
    case = cases(db)["ch_1"]
    assert (case.kind, case.user_id, case.charge_id) == ("refund", paid_account, "ch_1")
    assert not {"amount", "amount_refunded", "currency"} & set(BillingCase.__table__.columns.keys())
    assert (case.provider_status, case.review_status, case.last_event_id) == ("refunded", "open", "evt_1")
    assert tier(db, paid_account) == TIER_PAID                              # No automatic consequence.
    assert gateway.checkout_calls == [] and gateway.portal_calls == []      # And no refund issued.
    assert gateway.case_calls == [("charge", "ch_1"), ("charge", "ch_1")]   # Located, then re-read under the lock.
    log = capsys.readouterr().out
    assert "STRIPE_BILLING_CASE refund refunded" in log
    assert "4900" not in log and "ch_1" not in log and "cus_1" not in log and "payer@" not in log


def test_a_later_full_refund_updates_the_case_and_reopens_it(db, gateway, paid_account):
    gateway.charges["ch_1"] = charge(1000, False)
    deliver(db, gateway, "evt_1", "charge.refunded", "ch_1", "cus_1")
    assert cases(db)["ch_1"].provider_status == "partially_refunded"
    with db() as session, session.begin():
        session.query(BillingCase).one().review_status = "acknowledged"
    gateway.charges["ch_1"] = charge(4900, True)
    deliver(db, gateway, "evt_2", "charge.refunded", "ch_1", "cus_1")
    case = cases(db)["ch_1"]
    assert (case.provider_status, case.review_status) == ("refunded", "open")
    assert len(cases(db)) == 1


def test_a_dispute_is_tracked_through_to_its_outcome(db, gateway, paid_account):
    gateway.disputes["dp_1"] = dispute("needs_response")
    deliver(db, gateway, "evt_1", "charge.dispute.created", "dp_1")    # A dispute event names no customer.
    case = cases(db)["dp_1"]
    assert (case.kind, case.user_id, case.provider_status, case.reason) == (
        "dispute", paid_account, "needs_response", "fraudulent")
    with db() as session, session.begin():
        session.query(BillingCase).one().review_status = "closed"
    gateway.disputes["dp_1"] = dispute("lost")
    deliver(db, gateway, "evt_2", "charge.dispute.closed", "dp_1")
    case = cases(db)["dp_1"]
    assert (case.provider_status, case.review_status) == ("lost", "open")   # A new outcome needs a new look.
    assert tier(db, paid_account) == TIER_PAID


def test_unchanged_facts_do_not_reopen_a_reviewed_case(db, gateway, paid_account):
    gateway.disputes["dp_1"] = dispute("under_review")
    deliver(db, gateway, "evt_1", "charge.dispute.created", "dp_1")
    with db() as session, session.begin():
        session.query(BillingCase).one().review_status = "acknowledged"
    deliver(db, gateway, "evt_2", "charge.dispute.updated", "dp_1")
    assert cases(db)["dp_1"].review_status == "acknowledged"


def test_a_redelivered_event_changes_nothing(db, gateway, paid_account):
    gateway.charges["ch_1"] = charge(4900, True)
    assert deliver(db, gateway, "evt_1", "charge.refunded", "ch_1", "cus_1").result == "case_recorded"
    gateway.charges["ch_1"] = charge(100, False)                           # Would change it, if processed.
    assert deliver(db, gateway, "evt_1", "charge.refunded", "ch_1", "cus_1").result == "duplicate"
    assert cases(db)["ch_1"].provider_status == "refunded"


def test_a_case_for_an_unknown_customer_is_kept_unlinked(db, gateway, capsys):
    gateway.charges["ch_1"] = charge(4900, True, customer="cus_unknown")
    assert deliver(db, gateway, "evt_1", "charge.refunded", "ch_1", "cus_unknown").result == "case_recorded"
    assert cases(db)["ch_1"].user_id is None
    assert "unmatched" in capsys.readouterr().out


@pytest.mark.parametrize("state", [
    ChargeState("ch_other", "cus_1", 4900, 4900, "usd", True),               # Not the charge asked for.
    ChargeState("ch_1", "cus_1", -1, 0, "usd", False),                       # Amount missing.
    ChargeState("ch_1", "cus_1", 4900, 5000, "usd", True),                   # Refunded more than charged.
    ChargeState("ch_1", "cus_1", 4900, 4900, "US Dollars", True),            # Not a currency code.
])
def test_unusable_provider_facts_write_nothing_and_leave_the_event_for_a_retry(db, gateway, paid_account, state):
    gateway.charges["ch_1"] = state
    with pytest.raises(CaseStateInvalid):
        deliver(db, gateway, "evt_1", "charge.refunded", "ch_1", "cus_1")
    assert cases(db) == {}
    with db() as session:
        assert session.get(StripeWebhookEvent, "evt_1") is None


def test_a_provider_failure_writes_nothing(db, gateway, paid_account):
    gateway.fail_provider = True
    with pytest.raises(BillingProviderError):
        deliver(db, gateway, "evt_1", "charge.refunded", "ch_1", "cus_1")
    assert cases(db) == {}


def test_an_event_without_its_object_is_recorded_as_unmatched(db, gateway):
    assert deliver(db, gateway, "evt_1", "charge.dispute.created", None).result == "unmatched"
    assert cases(db) == {} and gateway.case_calls == []


# --- the Stripe adapter --------------------------------------------------------------------------


def test_the_stripe_adapter_reads_the_object_and_its_facts():
    stripe_gateway = pytest.importorskip("kalillac_billing.stripe_gateway")
    for event_type, obj in (("charge.refunded", {"id": "ch_9", "customer": "cus_9"}),
                            ("charge.dispute.created", {"id": "dp_9", "charge": "ch_9"})):
        event = stripe_gateway.webhook_event_from_stripe(
            {"id": "evt_9", "type": event_type, "livemode": False, "data": {"object": obj}})
        assert event.object_id == obj["id"] and event.subscription_id is None
    plain = stripe_gateway.webhook_event_from_stripe(
        {"id": "evt_9", "type": "customer.subscription.updated", "livemode": False,
         "data": {"object": {"id": "sub_9", "customer": "cus_9"}}})
    assert plain.object_id is None
    state = stripe_gateway.charge_state_from_stripe(
        {"id": "ch_9", "customer": {"id": "cus_9"}, "amount": 4900, "amount_refunded": 900, "currency": "eur",
         "refunded": False})
    assert state == ChargeState("ch_9", "cus_9", 4900, 900, "eur", False)
    disputed = stripe_gateway.dispute_state_from_stripe(
        {"id": "dp_9", "charge": {"id": "ch_9", "customer": "cus_9"}, "amount": 4900, "currency": "eur",
         "status": "won", "reason": "product_not_received"})
    assert disputed == DisputeState("dp_9", "ch_9", "cus_9", 4900, "eur", "won", "product_not_received")
    odd = stripe_gateway.charge_state_from_stripe({"id": "ch_9", "amount": "4900", "amount_refunded": True})
    assert (odd.amount, odd.amount_refunded) == (-1, -1)              # Refused later as unusable.


# --- the operator tool ---------------------------------------------------------------------------


def test_the_operator_lists_acknowledges_and_closes_cases(db, gateway, paid_account, capsys):
    gateway.charges["ch_1"] = charge(4900, True)
    deliver(db, gateway, "evt_1", "charge.refunded", "ch_1", "cus_1")
    case_id = str(next(iter(cases(db).values())).id)
    capsys.readouterr()
    assert tool.run(["list"], db) == 0
    listing = capsys.readouterr().out
    assert case_id in listing and "refunded" in listing and "linked" in listing and "1 case" in listing
    assert "payer@example.com" not in listing and "49" not in listing.replace(case_id, "")
    assert tool.run(["close", case_id], db) == 2                        # Nothing changes without --apply.
    assert tool.run(["acknowledge", case_id, "--apply"], db) == 0
    assert tool.run(["acknowledge", case_id, "--apply"], db) == 2       # Already acknowledged.
    assert tool.run(["close", case_id, "--apply"], db) == 0
    capsys.readouterr()
    tool.run(["list"], db)
    assert "0 cases" in capsys.readouterr().out
    tool.run(["list", "--all"], db)
    assert "closed" in capsys.readouterr().out
    assert tool.run(["close", "not-a-uuid", "--apply"], db) == 2
    assert tool.run(["close", "00000000-0000-0000-0000-000000000000", "--apply"], db) == 2
    assert tool.run(["delete", case_id], db) == 2
