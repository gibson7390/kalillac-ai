"""Checkout orchestration and verified webhook reconciliation.

Every billing transaction for an account first takes one per-account lock:
SELECT ... FOR UPDATE on that account's user row (lock_account). Checkout
attempts and webhook reconciliations for the same account therefore
serialize; different accounts never block each other.

Checkout (start_checkout):
1. Under the lock:
   - a paying subscription (active, trialing, past_due) -> already_subscribed;
   - a stored Checkout Session -> re-read it from Stripe (below);
   - a known subscription not explicitly terminal (incomplete, unpaid,
     paused, or a missing/unknown status) -> subscription_unresolved; only
     canceled or incomplete_expired may be replaced by a new purchase;
   - a pending attempt -> retried only within CHECKOUT_RETRY_WINDOW of its
     persisted creation time, otherwise held as billing_processing;
   - otherwise persist a new server-generated checkout_attempt_id with its
     UTC creation time and every attempt-specific Checkout parameter
     (customer, Price, success and cancel URLs). Commit.
2. Create the Stripe Checkout Session with idempotency key
   "kalillac-checkout-<user_id>-<checkout_attempt_id>" and the attempt's
   pinned parameters. Concurrent requests sharing the attempt, and retries
   after a crash between creation and step 3, send the identical key and
   parameters and get the same Stripe session back.
3. Under the lock: store the session id/expiry on that same attempt. A
   retried create's response is Stripe's saved original, so its session is
   re-read before any URL is returned.
A stored session is re-read from Stripe: open -> reuse its URL; complete ->
billing_processing unless already reconciled into an explicitly terminal
subscription; expired -> replaced through the guarded path; anything else
-> held. No provider error ever discards an attempt: a failure does not
prove an earlier request under the key produced no session, so the next
request retries the same key with the same parameters.

Webhook (process_webhook):
1. Verify the Stripe signature (failure -> 400 upstream); refuse an event
   whose livemode is not the configured Stripe mode (WebhookModeMismatch,
   nothing read or written, never recorded); ignore irrelevant types; skip
   ids already recorded.
2. Locate the Kalillac account from the signed event and local links.
3. One transaction: lock the account row; re-check the event id; fetch the
   CURRENT subscription from Stripe while holding the lock; update billing
   and entitlement from that object; record the event id last; commit.
   Event arrival order is never trusted, and two events for one account
   cannot commit snapshots out of order. Any failure rolls everything back
   and leaves the event unrecorded so Stripe retries.
4. Linkage: the same subscription updates normally, and a new subscription
   replaces the linked one only once the linked one is explicitly terminal.
   A different, terminal subscription arriving while a nonterminal one is
   linked is a delayed historical event: recorded, nothing changed. Two
   different nonterminal subscriptions are a conflict: rolled back, left
   unrecorded, and surfaced as non-2xx. Entitlement never decides linkage.

Operator recovery (reconcile_account) repairs one account from the ids it
has stored locally (a linked subscription, or a stored Checkout Session),
under the same account lock and through the same policy as webhooks
(_reconcile_subscription). It records no webhook event, and only links a
subscription that Stripe's own metadata assigns to that account. Its
transaction alone carries fixed lock and statement timeouts (SET LOCAL);
a timeout rolls back and raises ReconciliationBusy.

Access policy: paid while Stripe reports active, trialing, or past_due
(dunning) for a Price in the configured allowed set (all of which grant the
same paid entitlement); free for every other status, including unknown
future ones, and for any Price outside the set. cancel_at_period_end stays
paid while the status is still paying; the eventual cancellation makes it
free.

A paid entitlement always expires: at the subscription's current period end
plus PAID_ENTITLEMENT_GRACE. Each reconciliation (renewal included) rewrites
it from Stripe's current state, so a lost or delayed cancellation webhook
cannot leave access open beyond that. A paid-looking subscription without a
valid period end is refused (SubscriptionStateInvalid): the transaction rolls
back, the event stays unrecorded so Stripe retries, and the stored state is
left as it was.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
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

# Checkout replacement is a separate question from entitlement. A known
# subscription may be replaced by a new Checkout only once Stripe reports it
# explicitly terminal. Every other non-paying state (incomplete, unpaid,
# paused) and any missing or unknown status can still become, or already
# is, a live subscription, so it blocks a second one. Not being paid is
# never treated as proof that a subscription ended.
REPLACEABLE_SUBSCRIPTION_STATUSES = frozenset({"canceled", "incomplete_expired"})

ENTITLEMENT_SOURCE = "stripe"

# How long paid access outlives the subscription's recorded period end. The
# one place this policy is defined.
PAID_ENTITLEMENT_GRACE = timedelta(days=7)


def tier_for_subscription(
    state: SubscriptionState,
    allowed_price_ids: frozenset[str],
) -> str:
    """Kalillac access for a Stripe subscription state."""

    if isinstance(allowed_price_ids, str):
        # A single string would make "in" a substring test.
        raise TypeError("allowed_price_ids must be a set of Price ids.")

    if state.status in PAID_STATUSES and state.price_id in allowed_price_ids:
        return TIER_PAID

    return TIER_FREE


class SubscriptionStateInvalid(Exception):
    """A paid-looking subscription has no valid current_period_end.

    Raised inside the reconciliation transaction before anything is
    written, so the stored state is kept and the event stays unrecorded
    (Stripe retries). Carries no details.
    """


def paid_entitlement_expiry(state: SubscriptionState) -> datetime:
    """End of paid access for a paying subscription: its period end plus
    PAID_ENTITLEMENT_GRACE. Raises SubscriptionStateInvalid without a
    timezone-aware period end, so paid access is never open-ended."""

    period_end = state.current_period_end

    if not isinstance(period_end, datetime) or period_end.utcoffset() is None:
        raise SubscriptionStateInvalid()

    return period_end.astimezone(timezone.utc) + PAID_ENTITLEMENT_GRACE


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
    """url | already_subscribed | billing_processing | subscription_unresolved"""

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
    """subscribed | existing (reuse session_id) | unresolved | blocked | create"""

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


def _subscription_replaceable(billing: AccountBilling) -> bool:
    """True when no known subscription exists, or Stripe has reported the
    known one explicitly terminal. Missing or unknown status blocks."""

    if billing.stripe_subscription_id is None:
        return True

    return billing.subscription_status in REPLACEABLE_SUBSCRIPTION_STATUSES


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

        # A stored session is always re-read first, so an open session that
        # completes the original purchase keeps being reused.
        if billing.stripe_checkout_session_id:
            return _CheckoutPlan(
                "existing",
                session_id=billing.stripe_checkout_session_id,
            )

        # A known subscription that Stripe has not reported as explicitly
        # terminal blocks any new subscription Checkout.
        if not _subscription_replaceable(billing):
            return _CheckoutPlan("blocked")

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

        if plan.action == "blocked":
            return CheckoutResult("subscription_unresolved")

        if plan.action == "existing":
            # A known session: act on its actual Stripe state.
            current = gateway.retrieve_checkout_session(plan.session_id)

            if current.status == "open" and current.url:
                return CheckoutResult("url", current.url)

            if current.status == "complete":
                def reconciled(billing: AccountBilling) -> bool:
                    # Already reconciled into a subscription that Stripe has
                    # reported explicitly terminal: the old session is spent,
                    # start fresh. Any other state (paying, incomplete,
                    # unpaid, paused, missing, unknown) keeps the hold.
                    done = bool(
                        current.subscription_id
                        and billing.stripe_subscription_id
                        == current.subscription_id
                        and billing.subscription_status
                        in REPLACEABLE_SUBSCRIPTION_STATUSES
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
    """ignored | duplicate | processed | unmatched | ignored_historical"""

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


class ReconciliationConflict(Exception):
    """Two different nonterminal subscriptions claim one account.

    Raised inside the reconciliation transaction so nothing is written and
    the event stays unrecorded: the conflict is surfaced (non-2xx, Stripe
    keeps retrying) rather than silently resolved. Carries no details.
    """


class WebhookModeMismatch(Exception):
    """A verified event's livemode is not the configured Stripe mode.

    Raised before any database access: nothing is read, written, or
    recorded, so the event is never marked processed. Carries no details.
    """


def _is_terminal(status: str | None) -> bool:
    return status in REPLACEABLE_SUBSCRIPTION_STATUSES


def _linkage_decision(existing, state: SubscriptionState) -> str:
    """How a reconciled subscription relates to the account's linked one.

    - "apply": nothing linked yet, the same subscription (ordinary update),
      or the linked one is explicitly terminal (legitimate replacement);
    - "ignore": a different subscription that is itself explicitly terminal
      while the linked one is not: a delayed historical event that must not
      disturb the current subscription;
    - "conflict": a different subscription that is not explicitly terminal
      while the linked one is not either. Neither is chosen.

    Only canceled/incomplete_expired count as terminal; paying, incomplete,
    unpaid, paused, and missing or unknown statuses are all nonterminal.
    Entitlement (free vs paid) plays no part in this decision.
    """

    if existing is None or existing.stripe_subscription_id in (
        None,
        state.subscription_id,
    ):
        return "apply"

    if _is_terminal(existing.subscription_status):
        return "apply"

    if _is_terminal(state.status):
        return "ignore"

    return "conflict"


def _reconcile_subscription(
    session: Session,
    user_id: uuid.UUID,
    state: SubscriptionState,
    *,
    allowed_price_ids: frozenset[str],
    checkout_completed: bool = False,
) -> str:
    """The one reconciliation policy, shared by verified webhooks and
    operator recovery. Runs inside the caller's transaction, after
    lock_account(user_id), with Stripe's current subscription state.

    Returns "belongs_elsewhere" (nothing written), "ignored_historical"
    (nothing written), or "applied" (billing and entitlement updated).
    Raises ReconciliationConflict or SubscriptionStateInvalid before any
    write. checkout_completed: the state came from the account's own
    completed Checkout Session, so its pending attempt is cleared whatever
    the tier; otherwise only a paid subscription clears it.
    """

    if _belongs_elsewhere(session, user_id, state):
        return "belongs_elsewhere"

    decision = _linkage_decision(get_billing(session, user_id), state)

    if decision == "conflict":
        raise ReconciliationConflict()

    if decision == "ignore":
        return "ignored_historical"

    tier = tier_for_subscription(state, allowed_price_ids)

    # Validated before any write: an invalid paid state raises here and the
    # transaction leaves everything as it was.
    expires_at = paid_entitlement_expiry(state) if tier == TIER_PAID else None

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

    if tier == TIER_PAID or checkout_completed:
        # The Checkout attempt has done its job.
        clear_pending_checkout(billing)

    # Paid always carries its bounded expiry; free has none.
    set_entitlement_tier(
        session,
        user_id,
        tier,
        source=ENTITLEMENT_SOURCE,
        expires_at=expires_at,
    )

    return "applied"


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
    allowed_price_ids: frozenset[str],
    livemode: bool,
) -> WebhookOutcome:
    """Verify and reconcile one delivery. Raises InvalidWebhookSignature for
    a bad signature and WebhookModeMismatch for an event of the other
    Stripe mode; any other exception means "not processed, retry"."""

    event = gateway.parse_webhook(payload, signature)

    # Before anything else, irrelevant types included: an event from the
    # other mode is never acknowledged, read against, or recorded.
    if not isinstance(livemode, bool) or event.livemode is not livemode:
        raise WebhookModeMismatch()

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

            # Raises ReconciliationConflict (roll back, event left
            # unrecorded) or SubscriptionStateInvalid before any write.
            result = _reconcile_subscription(
                session,
                user_id,
                state,
                allowed_price_ids=allowed_price_ids,
            )

            if result == "belongs_elsewhere":
                return _unmatched(session, event)

            if result == "ignored_historical":
                # A delayed terminal event for an older subscription: keep
                # the current linkage, entitlement, and pending Checkout.
                record_webhook_event(session, event.event_id, event.event_type)
                return WebhookOutcome("ignored_historical")

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


# --- operator recovery ---------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileOutcome:
    """subscription_reconciled | checkout_recovered | checkout_open |
    checkout_expired | nothing_to_reconcile | account_not_found"""

    result: str


class ReconciliationRefused(Exception):
    """Operator recovery found provider evidence it must not act on.

    Raised inside the reconciliation transaction, so nothing is written.
    code is one stable value and the only detail carried:
    - ownership_mismatch: the subscription or its customer belongs to
      another account, or the account is linked to a different customer;
    - owner_metadata_missing / owner_metadata_mismatch: a subscription
      recovered through Checkout does not name exactly this account;
    - checkout_state_unrecognized / subscription_state_unrecognized: Stripe
      returned a state that is not proven usable (an unknown Checkout
      status, a completed Checkout without a subscription, or an object
      other than the one requested).
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ReconciliationBusy(Exception):
    """Operator reconciliation hit its lock or statement timeout (for
    example, a webhook held the account lock longer than the limit). The
    transaction rolled back; carries no details."""


# Fixed bounds for the operator transaction only; not configurable, so an
# operator cannot disable or widen them. SET LOCAL ends with the
# transaction, so the pooled connection, webhooks, Checkout and account
# requests keep their own settings.
OPERATOR_LOCK_TIMEOUT = "5s"
OPERATOR_STATEMENT_TIMEOUT = "30s"
OPERATOR_TIMEOUT_STATEMENTS = (
    f"SET LOCAL lock_timeout = '{OPERATOR_LOCK_TIMEOUT}'",
    f"SET LOCAL statement_timeout = '{OPERATOR_STATEMENT_TIMEOUT}'",
)

# PostgreSQL SQLSTATEs: lock_not_available (lock_timeout) and
# query_canceled (statement_timeout).
_OPERATOR_TIMEOUT_SQLSTATES = frozenset({"55P03", "57014"})


def _apply_operator_limits(session: Session) -> None:
    dialect = session.get_bind().dialect.name

    if dialect == "postgresql":
        for statement in OPERATOR_TIMEOUT_STATEMENTS:
            session.execute(text(statement))
        return

    if dialect == "sqlite":
        # Test databases only: SQLite has no such settings, and its own
        # busy timeout bounds waiting.
        return

    # Never run unbounded against an unrecognized database.
    raise RuntimeError("Operator reconciliation requires PostgreSQL.")


def _is_operator_timeout(exc: DBAPIError) -> bool:
    original = exc.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return sqlstate in _OPERATOR_TIMEOUT_SQLSTATES


def _require_requested_subscription(
    state: SubscriptionState,
    subscription_id: str,
) -> None:
    """Stripe must return the subscription asked for, with a status. An
    unknown status string still follows the shared policy (free,
    nonterminal); a missing one is malformed and is not applied."""

    if state.subscription_id != subscription_id or not (
        isinstance(state.status, str) and state.status
    ):
        raise ReconciliationRefused("subscription_state_unrecognized")


def _refresh_linked_subscription(
    session: Session,
    user_id: uuid.UUID,
    billing: AccountBilling,
    gateway: BillingGateway,
    allowed_price_ids: frozenset[str],
) -> ReconcileOutcome:
    subscription_id = billing.stripe_subscription_id
    state = gateway.retrieve_subscription(subscription_id)
    _require_requested_subscription(state, subscription_id)

    result = _reconcile_subscription(
        session,
        user_id,
        state,
        allowed_price_ids=allowed_price_ids,
    )

    if result == "belongs_elsewhere":
        raise ReconciliationRefused("ownership_mismatch")

    if result != "applied":
        # The linked subscription itself is never historical; anything else
        # is not proven and is not reported as success.
        raise ReconciliationRefused("subscription_state_unrecognized")

    return ReconcileOutcome("subscription_reconciled")


def _recover_from_checkout(
    session: Session,
    user_id: uuid.UUID,
    billing: AccountBilling,
    gateway: BillingGateway,
    allowed_price_ids: frozenset[str],
) -> ReconcileOutcome:
    session_id = billing.stripe_checkout_session_id
    checkout = gateway.retrieve_checkout_session(session_id)

    if checkout.session_id != session_id:
        raise ReconciliationRefused("checkout_state_unrecognized")

    if checkout.status == "open":
        # Still payable: nothing to repair and nothing written.
        return ReconcileOutcome("checkout_open")

    if checkout.status == "expired":
        # Confirmed dead: clear only this stored attempt, so a later
        # Checkout can begin through the guarded path.
        _discard_session(session_id)(billing)
        return ReconcileOutcome("checkout_expired")

    if checkout.status != "complete" or not checkout.subscription_id:
        raise ReconciliationRefused("checkout_state_unrecognized")

    state = gateway.retrieve_subscription(checkout.subscription_id)
    _require_requested_subscription(state, checkout.subscription_id)

    # A subscription not yet linked here is linked only when Stripe's own
    # metadata names exactly this account.
    if not state.kalillac_user_id:
        raise ReconciliationRefused("owner_metadata_missing")

    if _parse_user_id(state.kalillac_user_id) != user_id:
        raise ReconciliationRefused("owner_metadata_mismatch")

    result = _reconcile_subscription(
        session,
        user_id,
        state,
        allowed_price_ids=allowed_price_ids,
        checkout_completed=True,
    )

    if result == "belongs_elsewhere":
        raise ReconciliationRefused("ownership_mismatch")

    if result != "applied":
        raise ReconciliationRefused("subscription_state_unrecognized")

    return ReconcileOutcome("checkout_recovered")


def reconcile_account(
    user_id: uuid.UUID,
    *,
    gateway: BillingGateway,
    session_factory: sessionmaker[Session],
    allowed_price_ids: frozenset[str],
) -> ReconcileOutcome:
    """Repair one account's billing and entitlement from its own stored
    Stripe evidence, for an operator (never an HTTP route).

    One transaction, under the same account lock as Checkout and webhooks:
    1. lock the account row, then read its billing row;
    2. a linked subscription -> re-fetch exactly that subscription and apply
       it through the shared webhook policy (_reconcile_subscription);
    3. otherwise a stored Checkout Session -> re-fetch exactly that session:
       open -> checkout_open, nothing written; expired -> clear that pending
       attempt, checkout_expired; complete -> fetch its subscription, which
       must name this account in its metadata, apply it, and clear the
       attempt, checkout_recovered;
    4. nothing stored -> nothing_to_reconcile, without any Stripe call.
    A linked subscription Stripe has ended (canceled, incomplete_expired)
    alongside a stored Checkout Session is the re-subscribe case: step 3
    runs, and the shared linkage rule replaces the ended subscription.

    Only the account's locally stored ids are used; no caller can supply a
    subscription or session id. No webhook event is recorded.

    The transaction first sets the fixed operator limits
    (OPERATOR_TIMEOUT_STATEMENTS), so waiting for the account lock or any
    statement is bounded; a timeout raises ReconciliationBusy. Raises
    BillingProviderError, SubscriptionStateInvalid, ReconciliationConflict,
    ReconciliationRefused, or ReconciliationBusy, each after rolling
    everything back.
    """

    if not isinstance(user_id, uuid.UUID):
        raise TypeError("user_id must be a UUID.")

    try:
        with session_factory() as session, session.begin():
            # Before the lock: nothing has been read, fetched, or written.
            _apply_operator_limits(session)

            if not lock_account(session, user_id):
                return ReconcileOutcome("account_not_found")

            # A fresh session, read after the lock: nothing cached predates it.
            billing = get_billing(session, user_id)

            if billing is None:
                return ReconcileOutcome("nothing_to_reconcile")

            linked = billing.stripe_subscription_id

            if billing.stripe_checkout_session_id and (
                linked is None or _is_terminal(billing.subscription_status)
            ):
                return _recover_from_checkout(
                    session, user_id, billing, gateway, allowed_price_ids
                )

            if linked:
                return _refresh_linked_subscription(
                    session, user_id, billing, gateway, allowed_price_ids
                )

            return ReconcileOutcome("nothing_to_reconcile")
    except DBAPIError as exc:
        # The transaction has already rolled back. Neither the driver's
        # message nor its SQL is carried on.
        if _is_operator_timeout(exc):
            raise ReconciliationBusy() from None
        raise
