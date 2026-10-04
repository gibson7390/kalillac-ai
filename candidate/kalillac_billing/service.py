"""Checkout orchestration and verified webhook reconciliation.

Every billing transaction for an account first takes one per-account lock:
SELECT ... FOR UPDATE on that account's user row (lock_account). Checkout
attempts and webhook reconciliations for the same account therefore
serialize; different accounts never block each other.

Checkout (start_checkout):
1. Under the lock: refuse if already paying; otherwise reuse the stored
   Checkout Session, or reuse the pending attempt, or persist a new
   server-generated checkout_attempt_id together with a snapshot of the
   current Stripe customer (checkout_customer_id). Commit.
2. Create the Stripe Checkout Session with idempotency key
   "kalillac-checkout-<user_id>-<checkout_attempt_id>" and the attempt's
   snapshotted customer. Concurrent requests sharing the attempt, and
   retries after a crash between creation and step 3, send the identical
   key and parameters and get the same Stripe session back.
3. Under the lock: store the session id/expiry on that same attempt.
A stored session is re-read from Stripe: open -> reuse its URL; complete
but not yet reconciled -> billing_processing; expired -> new attempt.
Only a provider error proving the request never ran discards an attempt;
an idempotency conflict or unknown outcome keeps it for the next retry.

Webhook (process_webhook):
1. Verify the Stripe signature (failure -> 400 upstream); ignore irrelevant
   types; skip ids already recorded.
2. Locate the Kalillac account from the signed event and local links.
3. One transaction: lock the account row; re-check the event id; fetch the
   CURRENT subscription from Stripe while holding the lock; update billing
   and entitlement from that object; record the event id last; commit.
   Event arrival order is never trusted, and two events for one account
   cannot commit snapshots out of order. Any failure rolls everything back
   and leaves the event unrecorded so Stripe retries.

Access policy: paid while Stripe reports active, trialing, or past_due
(dunning) for the configured paid Price; free for every other status,
including unknown future ones. cancel_at_period_end stays paid while the
status is still paying; the eventual cancellation makes it free.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kalillac_db.models import AccountBilling, User
from kalillac_db.models.account_entitlement import TIER_FREE, TIER_PAID
from kalillac_db.repositories.billing import (
    clear_pending_checkout,
    get_billing,
    get_billing_by_customer,
    get_billing_by_subscription,
    get_or_create_billing,
    lock_account,
    record_webhook_event,
    upsert_billing_state,
    webhook_event_processed,
)
from kalillac_db.repositories.entitlements import set_entitlement_tier

from .gateway import (
    BillingGateway,
    BillingProviderError,
    CheckoutSessionState,
    SubscriptionState,
    WebhookEvent,
)


RELEVANT_EVENT_TYPES = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)

PAID_STATUSES = frozenset({"active", "trialing", "past_due"})

ENTITLEMENT_SOURCE = "stripe"


def tier_for_subscription(state: SubscriptionState, paid_price_id: str) -> str:
    """Kalillac access for a Stripe subscription state."""

    if state.status in PAID_STATUSES and state.price_id == paid_price_id:
        return TIER_PAID

    return TIER_FREE


def is_paying_status(status: str | None) -> bool:
    return status in PAID_STATUSES


def _parse_user_id(value: str | None) -> uuid.UUID | None:
    if not value:
        return None

    try:
        return uuid.UUID(value)
    except ValueError:
        return None


# --- Checkout ----------------------------------------------------------------------


@dataclass(frozen=True)
class CheckoutResult:
    """url | already_subscribed | billing_processing"""

    outcome: str
    url: str | None = None


# Automatic create retries of an attempt are allowed only this long after the
# attempt was persisted: safely inside Stripe's minimum idempotency-key
# retention (at least 24 hours), so a retry always reaches the original
# key's saved result and can never create a second session under a pruned
# key. Afterwards an unresolved attempt is held, not retried or replaced.
CHECKOUT_RETRY_WINDOW = timedelta(hours=23)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes; PostgreSQL returns aware ones.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CheckoutParams:
    """Every attempt-specific value sent to Checkout creation. Pinned when
    the attempt is persisted and sent unchanged on every retry."""

    price_id: str
    customer_id: str | None
    success_url: str
    cancel_url: str


@dataclass(frozen=True)
class _CheckoutPlan:
    """subscribed | existing (reuse session_id) | unresolved | create"""

    action: str
    session_id: str | None = None
    attempt_id: uuid.UUID | None = None
    attempt_created_at: datetime | None = None
    params: CheckoutParams | None = None
    new_attempt: bool = False


def checkout_idempotency_key(user_id: uuid.UUID, attempt_id: uuid.UUID) -> str:
    """Stripe idempotency key: Kalillac user id + persisted attempt id only."""

    return f"kalillac-checkout-{user_id}-{attempt_id}"


def _locked(session_factory: sessionmaker[Session], user_id: uuid.UUID, work):
    with session_factory() as session, session.begin():
        if not lock_account(session, user_id):
            raise LookupError("account no longer exists")

        return work(get_or_create_billing(session, user_id))


def _retry_window_open(created_at: datetime | None, now: datetime) -> bool:
    # A missing timestamp cannot be proven inside the window: hold it.
    return created_at is not None and now - _as_utc(created_at) < CHECKOUT_RETRY_WINDOW


def _plan_checkout(
    price_id: str,
    success_url: str,
    cancel_url: str,
    now: datetime,
):
    def work(billing: AccountBilling) -> _CheckoutPlan:
        if is_paying_status(billing.subscription_status):
            return _CheckoutPlan("subscribed")

        if billing.stripe_checkout_session_id:
            return _CheckoutPlan(
                "existing",
                session_id=billing.stripe_checkout_session_id,
            )

        new_attempt = billing.checkout_attempt_id is None

        if new_attempt:
            # Pin everything the Stripe request depends on, once.
            billing.checkout_attempt_id = uuid.uuid4()
            billing.checkout_attempt_created_at = now
            billing.checkout_customer_id = billing.stripe_customer_id
            billing.checkout_price_id = price_id
            billing.checkout_success_url = success_url
            billing.checkout_cancel_url = cancel_url
        elif not _retry_window_open(billing.checkout_attempt_created_at, now):
            # The outcome of the earlier create is unknown and its key may
            # be pruned: neither retry nor replace until reconciled.
            return _CheckoutPlan("unresolved")

        return _CheckoutPlan(
            "create",
            attempt_id=billing.checkout_attempt_id,
            attempt_created_at=billing.checkout_attempt_created_at,
            params=CheckoutParams(
                price_id=billing.checkout_price_id,
                customer_id=billing.checkout_customer_id,
                success_url=billing.checkout_success_url,
                cancel_url=billing.checkout_cancel_url,
            ),
            new_attempt=new_attempt,
        )

    return work


def _discard_session(session_id: str):
    """Clear a stored session that can no longer be used (only if it is
    still the stored one; a concurrent request may already have moved on)."""

    def work(billing: AccountBilling) -> None:
        if billing.stripe_checkout_session_id == session_id:
            clear_pending_checkout(billing)

    return work


def _store_session(attempt_id: uuid.UUID, state: CheckoutSessionState):
    """Record the session on its attempt. True if this session is now the
    account's stored one."""

    def work(billing: AccountBilling) -> bool:
        if billing.checkout_attempt_id != attempt_id:
            return False

        if billing.stripe_checkout_session_id not in (None, state.session_id):
            return False

        billing.stripe_checkout_session_id = state.session_id
        billing.checkout_session_expires_at = state.expires_at
        return True

    return work


def start_checkout(
    user_id: uuid.UUID,
    *,
    gateway: BillingGateway,
    session_factory: sessionmaker[Session],
    price_id: str,
    success_url: str,
    cancel_url: str,
    clock: Callable[[], datetime] = _utc_now,
) -> CheckoutResult:
    """Return one usable Checkout URL for the account, never a second
    subscription path. Raises BillingProviderError if Stripe fails; the
    attempt is always kept, so the next request retries the same key."""

    # Several passes cover a concurrent request replacing the attempt, and a
    # retried create whose session must then be re-read for its real state.
    for _ in range(4):
        plan = _locked(
            session_factory,
            user_id,
            _plan_checkout(price_id, success_url, cancel_url, clock()),
        )

        if plan.action == "subscribed":
            return CheckoutResult("already_subscribed")

        if plan.action == "unresolved":
            return CheckoutResult("billing_processing")

        if plan.action == "existing":
            # A known session: act on its actual Stripe state.
            current = gateway.retrieve_checkout_session(plan.session_id)

            if current.status == "open" and current.url:
                return CheckoutResult("url", current.url)

            if current.status == "complete":
                def reconciled(billing: AccountBilling) -> bool:
                    # Already reconciled into a subscription that has since
                    # ended: the old session is spent, start fresh.
                    done = bool(
                        current.subscription_id
                        and billing.stripe_subscription_id
                        == current.subscription_id
                        and not is_paying_status(billing.subscription_status)
                    )
                    if done:
                        _discard_session(plan.session_id)(billing)
                    return done

                if _locked(session_factory, user_id, reconciled):
                    continue

                # Paid, but the webhook has not reconciled yet.
                return CheckoutResult("billing_processing")

            if current.status == "expired":
                # Confirmed expired: replace through the guarded path.
                _locked(session_factory, user_id, _discard_session(plan.session_id))
                continue

            # Any other state is not proven usable or dead: hold.
            return CheckoutResult("billing_processing")

        # Checked immediately before the provider call: never send a retry
        # whose key may have been pruned.
        if not plan.new_attempt and not _retry_window_open(
            plan.attempt_created_at, clock()
        ):
            return CheckoutResult("billing_processing")

        # No provider error clears the attempt: an earlier request under this
        # key may have succeeded, so the next request retries the same key
        # with the same pinned parameters.
        created = gateway.create_checkout_session(
            price_id=plan.params.price_id,
            customer_id=plan.params.customer_id,
            kalillac_user_id=str(user_id),
            success_url=plan.params.success_url,
            cancel_url=plan.params.cancel_url,
            idempotency_key=checkout_idempotency_key(user_id, plan.attempt_id),
        )

        stored = _locked(session_factory, user_id, _store_session(plan.attempt_id, created))

        if not stored:
            # A concurrent request replaced this attempt; re-plan.
            continue

        if plan.new_attempt and created.status == "open" and created.url:
            return CheckoutResult("url", created.url)

        # A retried key returns Stripe's saved original response, whose
        # status may be stale: the next pass re-reads the stored session's
        # actual state before any URL is returned.

    raise BillingProviderError()


# --- webhook reconciliation -----------------------------------------------------


@dataclass(frozen=True)
class WebhookOutcome:
    """ignored | duplicate | processed | unmatched"""

    result: str


def _locate_user(session: Session, event: WebhookEvent) -> uuid.UUID | None:
    """The account a signed event refers to, from the event itself and
    existing billing links. No Stripe call is needed to decide what to lock."""

    candidates = [_parse_user_id(event.kalillac_user_id)]

    for stripe_id, lookup in (
        (event.subscription_id, get_billing_by_subscription),
        (event.customer_id, get_billing_by_customer),
    ):
        if stripe_id:
            billing = lookup(session, stripe_id)
            candidates.append(billing.user_id if billing else None)

    for user_id in candidates:
        if user_id is not None and session.get(User, user_id) is not None:
            return user_id

    return None


def _belongs_elsewhere(
    session: Session,
    user_id: uuid.UUID,
    state: SubscriptionState,
) -> bool:
    metadata_user = _parse_user_id(state.kalillac_user_id)

    if metadata_user is not None and metadata_user != user_id:
        return True

    for stripe_id, lookup in (
        (state.customer_id, get_billing_by_customer),
        (state.subscription_id, get_billing_by_subscription),
    ):
        if stripe_id:
            billing = lookup(session, stripe_id)

            if billing is not None and billing.user_id != user_id:
                return True

    existing = get_billing(session, user_id)

    return bool(
        existing is not None
        and existing.stripe_customer_id
        and state.customer_id
        and existing.stripe_customer_id != state.customer_id
    )


def _supersedes_local(existing, state: SubscriptionState) -> bool:
    """One paid subscription per account: a different, non-paying
    subscription never overwrites a currently paying one."""

    if existing is None or existing.stripe_subscription_id in (
        None,
        state.subscription_id,
    ):
        return True

    return is_paying_status(state.status) or not is_paying_status(
        existing.subscription_status
    )


def _unmatched(session: Session, event: WebhookEvent) -> WebhookOutcome:
    record_webhook_event(session, event.event_id, event.event_type)
    print(f"WARN: STRIPE_WEBHOOK_UNMATCHED {event.event_type}")
    return WebhookOutcome("unmatched")


def process_webhook(
    payload: bytes,
    signature: str,
    *,
    gateway: BillingGateway,
    session_factory: sessionmaker[Session],
    paid_price_id: str,
) -> WebhookOutcome:
    """Verify and reconcile one delivery. Raises InvalidWebhookSignature for
    a bad signature; any other exception means "not processed, retry"."""

    event = gateway.parse_webhook(payload, signature)

    if event.event_type not in RELEVANT_EVENT_TYPES:
        return WebhookOutcome("ignored")

    with session_factory() as session:
        if webhook_event_processed(session, event.event_id):
            return WebhookOutcome("duplicate")

        user_id = _locate_user(session, event)

    try:
        with session_factory() as session, session.begin():
            if user_id is None or not lock_account(session, user_id):
                if webhook_event_processed(session, event.event_id):
                    return WebhookOutcome("duplicate")
                return _unmatched(session, event)

            # Under the account lock: a concurrent delivery of this same
            # event that committed first is seen here.
            if webhook_event_processed(session, event.event_id):
                return WebhookOutcome("duplicate")

            if event.subscription_id is None:
                # e.g. a non-subscription Checkout: nothing to reconcile.
                record_webhook_event(session, event.event_id, event.event_type)
                return WebhookOutcome("processed")

            # Authoritative state, fetched while holding the account lock.
            state = gateway.retrieve_subscription(event.subscription_id)

            if _belongs_elsewhere(session, user_id, state):
                return _unmatched(session, event)

            existing = get_billing(session, user_id)

            if _supersedes_local(existing, state):
                billing = upsert_billing_state(
                    session,
                    user_id,
                    stripe_customer_id=state.customer_id,
                    stripe_subscription_id=state.subscription_id,
                    stripe_price_id=state.price_id,
                    subscription_status=state.status,
                    cancel_at_period_end=state.cancel_at_period_end,
                    current_period_end=state.current_period_end,
                )

                tier = tier_for_subscription(state, paid_price_id)

                if tier == TIER_PAID:
                    # The Checkout attempt has done its job.
                    clear_pending_checkout(billing)

                set_entitlement_tier(
                    session,
                    user_id,
                    tier,
                    source=ENTITLEMENT_SOURCE,
                    expires_at=None,
                )

            # Recorded only after reconciliation, in the same transaction.
            record_webhook_event(session, event.event_id, event.event_type)
            return WebhookOutcome("processed")
    except IntegrityError:
        # Two deliveries of an event with no lockable account both inserted;
        # the unique event id rejected the second.
        with session_factory() as session:
            if webhook_event_processed(session, event.event_id):
                return WebhookOutcome("duplicate")

        raise
