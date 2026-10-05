"""The real Stripe adapter, using the stripe library itself (no network).

Webhook signatures are produced exactly as Stripe signs them (HMAC-SHA256
over "timestamp.payload"), and Stripe objects are built from dicts with the
library's own constructors. API calls are intercepted before any request.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import time

import pytest

stripe = pytest.importorskip("stripe")

from kalillac_billing.config import BillingConfig
from kalillac_billing.gateway import BillingProviderError, InvalidWebhookSignature
from kalillac_billing.stripe_gateway import (
    StripeGateway,
    subscription_state_from_stripe,
    webhook_event_from_stripe,
)


WEBHOOK_SECRET = "whsec_testsigningsecret"

CONFIG = BillingConfig(
    secret_key="sk_test_notreal",
    webhook_secret=WEBHOOK_SECRET,
    price_id="price_kalillacpaid",
    success_url="https://kalillac.com/billing/success",
    cancel_url="https://kalillac.com/billing/cancel",
    portal_return_url="https://kalillac.com/account",
)

PERIOD_END = 1793880000  # 2026-11-05T12:00:00Z


def _sign(payload: bytes, secret: str = WEBHOOK_SECRET, timestamp=None) -> str:
    timestamp = int(time.time()) if timestamp is None else timestamp
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _event(event_type, data_object, event_id="evt_test_1"):
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "type": event_type,
            "api_version": stripe.api_version,
            "data": {"object": data_object},
        }
    ).encode()


SUBSCRIPTION = {
    "id": "sub_123",
    "object": "subscription",
    "customer": "cus_123",
    "status": "active",
    "cancel_at_period_end": True,
    "metadata": {"kalillac_user_id": "11111111-1111-1111-1111-111111111111"},
    "items": {
        "object": "list",
        "data": [
            {
                "id": "si_1",
                "object": "subscription_item",
                "price": {"id": "price_kalillacpaid", "object": "price"},
                "current_period_end": PERIOD_END,
            }
        ],
    },
}


@pytest.fixture
def gateway():
    return StripeGateway(CONFIG)


# --- webhook signature verification ----------------------------------------------


def test_valid_signature_is_accepted(gateway):
    payload = _event("customer.subscription.updated", SUBSCRIPTION)

    event = gateway.parse_webhook(payload, _sign(payload))

    assert event.event_id == "evt_test_1"
    assert event.event_type == "customer.subscription.updated"
    assert event.subscription_id == "sub_123"
    assert event.customer_id == "cus_123"
    assert event.kalillac_user_id == "11111111-1111-1111-1111-111111111111"


def test_tampered_payload_is_rejected(gateway):
    payload = _event("customer.subscription.updated", SUBSCRIPTION)
    signature = _sign(payload)
    tampered = payload.replace(b'"active"', b'"trialing"')

    with pytest.raises(InvalidWebhookSignature):
        gateway.parse_webhook(tampered, signature)


def test_wrong_secret_is_rejected(gateway):
    payload = _event("customer.subscription.updated", SUBSCRIPTION)

    with pytest.raises(InvalidWebhookSignature):
        gateway.parse_webhook(payload, _sign(payload, secret="whsec_attacker"))


def test_stale_timestamp_is_rejected(gateway):
    payload = _event("customer.subscription.updated", SUBSCRIPTION)
    old = int(time.time()) - 3600

    with pytest.raises(InvalidWebhookSignature):
        gateway.parse_webhook(payload, _sign(payload, timestamp=old))


@pytest.mark.parametrize("header", ["", "garbage", "t=1,v1=00"])
def test_malformed_signature_header_is_rejected(gateway, header):
    payload = _event("customer.subscription.updated", SUBSCRIPTION)

    with pytest.raises(InvalidWebhookSignature):
        gateway.parse_webhook(payload, header)


def test_signed_non_json_payload_is_rejected(gateway):
    payload = b"not json"

    with pytest.raises(InvalidWebhookSignature):
        gateway.parse_webhook(payload, _sign(payload))


# --- object reduction ---------------------------------------------------------------


def test_checkout_completed_event_fields():
    event = stripe.Event.construct_from(
        json.loads(
            _event(
                "checkout.session.completed",
                {
                    "id": "cs_1",
                    "object": "checkout.session",
                    "mode": "subscription",
                    "subscription": "sub_123",
                    "customer": "cus_123",
                    "client_reference_id": "22222222-2222-2222-2222-222222222222",
                    "customer_details": {"email": "payer@example.com"},
                },
            )
        ),
        "sk_test_notreal",
    )

    reduced = webhook_event_from_stripe(event)

    assert (
        reduced.subscription_id,
        reduced.customer_id,
        reduced.kalillac_user_id,
    ) == ("sub_123", "cus_123", "22222222-2222-2222-2222-222222222222")
    # Only these five fields survive; customer details are dropped.
    assert "payer@example.com" not in repr(reduced)


def test_irrelevant_event_has_no_subscription():
    event = stripe.Event.construct_from(
        json.loads(_event("invoice.paid", {"id": "in_1", "object": "invoice"})),
        "sk_test_notreal",
    )

    assert webhook_event_from_stripe(event).subscription_id is None


def test_subscription_state_reads_period_and_price_from_items():
    subscription = stripe.Subscription.construct_from(SUBSCRIPTION, "sk_test_notreal")

    state = subscription_state_from_stripe(subscription)

    assert state.subscription_id == "sub_123"
    assert state.customer_id == "cus_123"
    assert state.status == "active"
    assert state.price_id == "price_kalillacpaid"
    assert state.cancel_at_period_end is True
    assert state.current_period_end == datetime.fromtimestamp(
        PERIOD_END, tz=timezone.utc
    )
    assert state.kalillac_user_id == "11111111-1111-1111-1111-111111111111"


def test_expanded_customer_object_is_reduced_to_its_id():
    expanded = dict(SUBSCRIPTION, customer={"id": "cus_123", "object": "customer",
                                            "email": "payer@example.com"})
    state = subscription_state_from_stripe(
        stripe.Subscription.construct_from(expanded, "sk_test_notreal")
    )

    assert state.customer_id == "cus_123"
    assert "payer@example.com" not in repr(state)


# --- cancellation at period end (newer API shape) ---------------------------------

# Observed in the Stripe sandbox after a Customer Portal "cancel at period end".
PORTAL_PERIOD_END = datetime(2026, 11, 4, 20, 14, 45, tzinfo=timezone.utc)
PORTAL_CANCELED_AT = datetime(2026, 10, 4, 23, 15, 38, tzinfo=timezone.utc)


def _epoch(value):
    return int(value.timestamp())


def _portal_subscription(**overrides):
    """A subscription shaped like Stripe's response on this API version:
    no top-level current_period_end; the period end lives on the item."""

    subscription = {
        "id": "sub_portal",
        "object": "subscription",
        "livemode": False,
        "customer": "cus_portal",
        "status": "active",
        "cancel_at_period_end": False,
        "cancel_at": _epoch(PORTAL_PERIOD_END),
        "canceled_at": _epoch(PORTAL_CANCELED_AT),
        "ended_at": None,
        "metadata": {"kalillac_user_id": "33333333-3333-3333-3333-333333333333"},
        "items": {
            "object": "list",
            "data": [
                {
                    "id": "si_portal",
                    "object": "subscription_item",
                    "price": {"id": "price_kalillacpaid", "object": "price"},
                    "current_period_end": _epoch(PORTAL_PERIOD_END),
                }
            ],
        },
    }
    subscription.update(overrides)
    return stripe.Subscription.construct_from(subscription, "sk_test_notreal")


def test_portal_shape_has_no_top_level_period_end():
    subscription = _portal_subscription()

    assert "current_period_end" not in subscription
    assert subscription["cancel_at_period_end"] is False


def test_literal_cancel_at_period_end_true_remains_true():
    state = subscription_state_from_stripe(
        _portal_subscription(cancel_at_period_end=True, cancel_at=None)
    )

    assert state.cancel_at_period_end is True


def test_cancel_at_equal_to_item_period_end_means_cancel_at_period_end():
    # Exactly the sandbox observation: literal false, cancel_at == period end.
    state = subscription_state_from_stripe(_portal_subscription())

    assert state.cancel_at_period_end is True
    assert state.status == "active"


def test_no_cancel_at_remains_false():
    state = subscription_state_from_stripe(
        _portal_subscription(cancel_at=None, canceled_at=None)
    )

    assert state.cancel_at_period_end is False


@pytest.mark.parametrize(
    "cancel_at",
    [
        PORTAL_PERIOD_END.replace(day=20),               # later, explicit date
        PORTAL_PERIOD_END.replace(month=10, day=20),     # earlier, mid-period
        PORTAL_PERIOD_END.replace(second=44),            # one second off
    ],
)
def test_cancel_at_different_from_period_end_remains_false(cancel_at):
    state = subscription_state_from_stripe(
        _portal_subscription(cancel_at=_epoch(cancel_at))
    )

    assert state.cancel_at_period_end is False


def test_cancel_at_without_item_period_end_remains_false():
    subscription = _portal_subscription()
    subscription["items"]["data"][0]["current_period_end"] = None

    state = subscription_state_from_stripe(subscription)

    assert state.cancel_at_period_end is False
    assert state.current_period_end is None


def test_item_period_end_is_converted_and_preserved():
    state = subscription_state_from_stripe(_portal_subscription())

    assert state.current_period_end == PORTAL_PERIOD_END
    assert state.current_period_end.tzinfo is not None
    assert state.current_period_end.isoformat() == "2026-11-04T20:14:45+00:00"


def test_canceled_at_alone_does_not_mean_cancel_at_period_end():
    state = subscription_state_from_stripe(
        _portal_subscription(cancel_at=None, canceled_at=_epoch(PORTAL_CANCELED_AT))
    )

    assert state.cancel_at_period_end is False


def test_canceled_at_equal_to_period_end_still_does_not_count():
    # Even a canceled_at that happens to match the period end is ignored;
    # only cancel_at expresses the scheduled end.
    state = subscription_state_from_stripe(
        _portal_subscription(cancel_at=None, canceled_at=_epoch(PORTAL_PERIOD_END))
    )

    assert state.cancel_at_period_end is False


# --- API calls (intercepted; no network) ------------------------------------------


OPEN_SESSION = {
    "id": "cs_1",
    "object": "checkout.session",
    "status": "open",
    "url": "https://checkout.stripe.com/c/pay/cs_1",
    "expires_at": PERIOD_END,
    "subscription": None,
}


def test_checkout_session_parameters(gateway, monkeypatch):
    captured = {}
    captured_options = {}

    def create(params=None, options=None):
        captured.update(params)
        captured_options.update(options)
        return stripe.checkout.Session.construct_from(
            OPEN_SESSION,
            "sk_test_notreal",
        )

    monkeypatch.setattr(gateway._client.v1.checkout.sessions, "create", create)

    state = gateway.create_checkout_session(
        price_id="price_kalillacpaid",
        customer_id="cus_123",
        kalillac_user_id="11111111-1111-1111-1111-111111111111",
        success_url=CONFIG.success_url,
        cancel_url=CONFIG.cancel_url,
        idempotency_key="kalillac-checkout-u-attempt",
    )

    assert state.url == "https://checkout.stripe.com/c/pay/cs_1"
    assert state.session_id == "cs_1"
    assert state.status == "open"
    assert state.expires_at == datetime.fromtimestamp(PERIOD_END, tz=timezone.utc)
    # The server-generated key is sent as Stripe's idempotency key.
    assert captured_options == {"idempotency_key": "kalillac-checkout-u-attempt"}
    assert captured == {
        "mode": "subscription",
        "line_items": [{"price": "price_kalillacpaid", "quantity": 1}],
        "success_url": CONFIG.success_url,
        "cancel_url": CONFIG.cancel_url,
        "client_reference_id": "11111111-1111-1111-1111-111111111111",
        "metadata": {"kalillac_user_id": "11111111-1111-1111-1111-111111111111"},
        "subscription_data": {
            "metadata": {
                "kalillac_user_id": "11111111-1111-1111-1111-111111111111"
            },
        },
        "customer": "cus_123",
    }


def test_new_customer_is_created_through_checkout(gateway, monkeypatch):
    captured = {}

    def create(params=None, options=None):
        captured.update(params)
        return stripe.checkout.Session.construct_from(
            OPEN_SESSION,
            "sk_test_notreal",
        )

    monkeypatch.setattr(gateway._client.v1.checkout.sessions, "create", create)

    gateway.create_checkout_session(
        price_id="price_kalillacpaid",
        customer_id=None,
        kalillac_user_id="u",
        success_url=CONFIG.success_url,
        cancel_url=CONFIG.cancel_url,
        idempotency_key="kalillac-checkout-u-attempt",
    )

    assert "customer" not in captured


def test_retrieve_checkout_session_states(gateway, monkeypatch):
    sessions = {
        "cs_open": OPEN_SESSION,
        "cs_done": dict(
            OPEN_SESSION, id="cs_done", status="complete", url=None,
            subscription={"id": "sub_123", "object": "subscription"},
        ),
        "cs_old": dict(OPEN_SESSION, id="cs_old", status="expired", url=None),
    }

    def retrieve(session_id, params=None, options=None):
        return stripe.checkout.Session.construct_from(
            sessions[session_id], "sk_test_notreal"
        )

    monkeypatch.setattr(gateway._client.v1.checkout.sessions, "retrieve", retrieve)

    assert gateway.retrieve_checkout_session("cs_open").status == "open"

    done = gateway.retrieve_checkout_session("cs_done")
    assert (done.status, done.subscription_id, done.url) == (
        "complete",
        "sub_123",
        None,
    )

    assert gateway.retrieve_checkout_session("cs_old").status == "expired"


def _checkout_call(gateway):
    return gateway.create_checkout_session(
        price_id="p", customer_id=None, kalillac_user_id="u",
        success_url="s", cancel_url="c", idempotency_key="k",
    )


def test_stripe_errors_become_opaque_provider_errors(gateway, monkeypatch):
    def failing(*args, **kwargs):
        raise stripe.APIConnectionError("connection details sk_test_notreal")

    monkeypatch.setattr(gateway._client.v1.checkout.sessions, "create", failing)
    monkeypatch.setattr(gateway._client.v1.checkout.sessions, "retrieve", failing)
    monkeypatch.setattr(gateway._client.v1.billing_portal.sessions, "create", failing)
    monkeypatch.setattr(gateway._client.v1.subscriptions, "retrieve", failing)

    for call in (
        lambda: _checkout_call(gateway),
        lambda: gateway.retrieve_checkout_session("cs_1"),
        lambda: gateway.create_portal_session(customer_id="cus", return_url="r"),
        lambda: gateway.retrieve_subscription("sub_123"),
    ):
        with pytest.raises(BillingProviderError) as raised:
            call()

        assert str(raised.value) == ""


@pytest.mark.parametrize(
    "error",
    [
        stripe.InvalidRequestError("bad", param="price", http_status=400),
        stripe.IdempotencyError("in-progress request", http_status=409),
        stripe.IdempotencyError("keys reused with other params", http_status=400),
        stripe.IdempotencyError("unspecified"),
        stripe.InvalidRequestError("lock timeout", param=None, http_status=409),
        stripe.InvalidRequestError("missing", param=None, http_status=404),
        stripe.AuthenticationError("bad key"),
        stripe.PermissionError("no access"),
        stripe.APIConnectionError("network"),
        stripe.RateLimitError("slow down"),
        stripe.APIError("stripe 500", http_status=500),
    ],
)
def test_every_stripe_error_is_opaque_and_carries_no_discard_signal(
    gateway, monkeypatch, error
):
    # No error class or status is treated as proof that an earlier request
    # under the key produced no session; none tells the caller to discard.
    def failing(*args, **kwargs):
        raise error

    monkeypatch.setattr(gateway._client.v1.checkout.sessions, "create", failing)

    with pytest.raises(BillingProviderError) as raised:
        _checkout_call(gateway)

    assert str(raised.value) == ""
    assert vars(raised.value) == {}


def test_portal_and_retrieve(gateway, monkeypatch):
    portal_params = {}

    def portal_create(params=None, options=None):
        portal_params.update(params)
        return stripe.billing_portal.Session.construct_from(
            {"id": "bps_1", "url": "https://billing.stripe.com/p/session/1"},
            "sk_test_notreal",
        )

    def retrieve(subscription_id, params=None, options=None):
        assert subscription_id == "sub_123"
        return stripe.Subscription.construct_from(SUBSCRIPTION, "sk_test_notreal")

    monkeypatch.setattr(
        gateway._client.v1.billing_portal.sessions, "create", portal_create
    )
    monkeypatch.setattr(gateway._client.v1.subscriptions, "retrieve", retrieve)

    assert gateway.create_portal_session(
        customer_id="cus_123", return_url=CONFIG.portal_return_url
    ) == "https://billing.stripe.com/p/session/1"
    assert portal_params == {
        "customer": "cus_123",
        "return_url": CONFIG.portal_return_url,
    }
    assert gateway.retrieve_subscription("sub_123").status == "active"
