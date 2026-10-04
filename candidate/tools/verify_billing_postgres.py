"""Real-PostgreSQL verification of the billing service (staging only).

Drives kalillac_billing.service against an actual PostgreSQL database with
an in-process fake Stripe gateway: no Stripe credentials, no network calls
to Stripe, no app server, no pytest.

Destination is fixed: database kalillac_staging as runtime role
kalillac_staging_app. Arguments and environment must name exactly those,
KALILLAC_DATABASE_URL must be unset, and both are validated before any
password prompt or connection and confirmed again after connecting.

Safety:
- Never creates, alters, or drops schema objects.
- Every record it creates is tagged with a per-run id; cleanup deletes only
  those records (users cascade to their billing/entitlement rows; webhook
  events by id prefix) and then verifies nothing tagged remains.
- Every connection runs with finite lock_timeout and statement_timeout.
- After each check, every hold is released and every worker thread joined
  (bounded) before monkeypatches are restored or the next check starts. If
  a worker cannot be stopped, the run aborts without cleanup (exit 3) and
  prints the run id for manual cleanup.
- Refuses to run with Python optimization (-O), which would strip asserts.

The password is read from KALILLAC_DB_PASSWORD if set, otherwise prompted
without echo. Nothing prints the password or a URL containing it.

Usage (from the candidate/ directory of a checkout at the verified commit):
    KALILLAC_DB_HOST=127.0.0.1 KALILLAC_DB_PORT=5432 \\
    KALILLAC_DB_NAME=kalillac_staging KALILLAC_DB_USER=kalillac_staging_app \\
    python tools/verify_billing_postgres.py \\
        --expect-db kalillac_staging --expect-user kalillac_staging_app

Exit codes: 0 all passed; 1 a check failed (cleanup done); 2 refused to
start; 3 aborted with workers still active (cleanup skipped).
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import URL, create_engine, delete, func, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import kalillac_billing.service as service  # noqa: E402
from kalillac_billing.gateway import (  # noqa: E402
    BillingProviderError,
    CheckoutSessionState,
    InvalidWebhookSignature,
    SubscriptionState,
    WebhookEvent,
)
from kalillac_billing.service import (  # noqa: E402
    ReconciliationConflict,
    checkout_idempotency_key,
    process_webhook,
    start_checkout,
)
from kalillac_db.models import (  # noqa: E402
    AccountBilling,
    AccountEntitlement,
    StripeWebhookEvent,
    User,
)
from kalillac_db.repositories.billing import account_lock_statement  # noqa: E402
from kalillac_db.repositories.entitlements import create_default_entitlement  # noqa: E402


# The only permitted destination. Not configurable.
REQUIRED_DATABASE = "kalillac_staging"
REQUIRED_ROLE = "kalillac_staging_app"

# Server-side bounds on every harness connection. The longest deliberate
# hold is HOLD_SECONDS, so these leave ample margin while guaranteeing that
# no statement or lock wait can hang indefinitely.
LOCK_TIMEOUT = "10s"
STATEMENT_TIMEOUT = "30s"

# Deliberate observation window while a lock or fake call is held.
HOLD_SECONDS = 1.5

# Upper bound on any in-worker wait for a release signal.
WAIT_TIMEOUT = 20.0

# Upper bound on joining all workers after a check.
WORKER_JOIN_TIMEOUT = 60.0

PRICE = "price_verify_paid"
SIGNATURE = "verify-signature"
URLS = dict(
    success_url="https://staging.invalid/billing/success",
    cancel_url="https://staging.invalid/billing/cancel",
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_ABORTED = 3


class WorkerStillActive(RuntimeError):
    """A worker thread could not be stopped; continuing would be unsafe."""


def wait_or_fail(event: threading.Event, what: str) -> None:
    if not event.wait(timeout=WAIT_TIMEOUT):
        raise AssertionError(f"timed out waiting for {what}")


# --- fake Stripe (in-process; never contacts Stripe) ---------------------------------


@dataclass
class FakeStripe:
    """Stripe stand-in. Checkout creation honors idempotency keys (same key
    -> same session, atomically); a hold can pause calls in flight."""

    subscriptions: dict = field(default_factory=dict)
    sessions_by_key: dict = field(default_factory=dict)
    sessions_by_id: dict = field(default_factory=dict)
    checkout_calls: list = field(default_factory=list)
    retrieve_calls: list = field(default_factory=list)
    hold_create: threading.Event | None = None
    retrieve_hook: object = None
    fail_retrieve: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def create_checkout_session(self, **kwargs):
        with self.lock:
            self.checkout_calls.append(kwargs)

        if self.hold_create is not None:
            wait_or_fail(self.hold_create, "checkout hold release")

        key = kwargs["idempotency_key"]

        with self.lock:
            if key not in self.sessions_by_key:
                # Globally unique: the column has a unique constraint and
                # earlier checks' sessions remain stored until cleanup.
                session_id = f"cs_verify_{uuid.uuid4().hex}"
                self.sessions_by_key[key] = session_id
                self.sessions_by_id[session_id] = CheckoutSessionState(
                    session_id=session_id,
                    status="open",
                    url=f"https://checkout.invalid/{session_id}",
                    expires_at=None,
                    subscription_id=None,
                )
            return self.sessions_by_id[self.sessions_by_key[key]]

    def retrieve_checkout_session(self, session_id):
        return self.sessions_by_id[session_id]

    def create_portal_session(self, **kwargs):  # pragma: no cover - unused
        raise AssertionError("portal not exercised")

    def retrieve_subscription(self, subscription_id):
        with self.lock:
            self.retrieve_calls.append(subscription_id)

        if self.fail_retrieve:
            raise BillingProviderError()

        if self.retrieve_hook is not None:
            return self.retrieve_hook(subscription_id)

        return self.subscriptions[subscription_id]

    def parse_webhook(self, payload, signature):
        if signature != SIGNATURE:
            raise InvalidWebhookSignature()

        data = json.loads(payload)
        return WebhookEvent(
            event_id=data["id"],
            event_type=data["type"],
            subscription_id=data.get("subscription"),
            customer_id=data.get("customer"),
            kalillac_user_id=data.get("user"),
        )


# --- harness context ------------------------------------------------------------------


@dataclass
class Context:
    engine: object
    factory: sessionmaker
    run_id: str
    user_ids: list = field(default_factory=list)
    counter: int = 0
    worker_join_timeout: float = WORKER_JOIN_TIMEOUT
    # Per-check resources, settled by the runner after every check.
    workers: list = field(default_factory=list)
    holds: list = field(default_factory=list)
    patches: list = field(default_factory=list)

    def email(self) -> str:
        self.counter += 1
        return f"billing-verify+{self.run_id}-{self.counter}@example.invalid"

    def event_id(self, name: str) -> str:
        return f"evt_verify_{self.run_id}_{name}"

    def sub_id(self, name: str) -> str:
        return f"sub_verify_{self.run_id}_{name}"

    def customer_id(self, user_id: uuid.UUID) -> str:
        # One fake Stripe customer per test account: a customer shared
        # across accounts is (correctly) refused as belonging elsewhere.
        return f"cus_verify_{self.run_id}_{user_id.hex[:12]}"

    def hold(self) -> threading.Event:
        """A release signal the runner always sets after the check."""

        event = threading.Event()
        self.holds.append(event)
        return event

    def start(self, target):
        """Start a tracked worker; the runner joins it after the check."""

        box = {}

        def run():
            try:
                box["value"] = target()
            except BaseException as exc:  # reported via finish()
                box["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        self.workers.append(thread)
        thread.start()
        return thread, box

    def patch(self, owner, name, value) -> None:
        """Monkeypatch restored by the runner only after workers stop."""

        self.patches.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def settle(self) -> list:
        """Release every hold, join every worker (bounded), then restore
        patches. Returns workers still alive; patches are left in place
        (and the run must abort) if any remain."""

        for event in self.holds:
            event.set()

        deadline = time.monotonic() + self.worker_join_timeout

        for thread in self.workers:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

        alive = [thread for thread in self.workers if thread.is_alive()]

        if alive:
            return alive

        for owner, name, original in reversed(self.patches):
            setattr(owner, name, original)

        self.workers.clear()
        self.holds.clear()
        self.patches.clear()
        return []


def finish(thread, box, timeout=WAIT_TIMEOUT):
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise AssertionError("worker did not finish in time")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def add_user(ctx: Context) -> uuid.UUID:
    user_id = uuid.uuid4()

    with ctx.factory() as session, session.begin():
        session.add(User(id=user_id, email=ctx.email(), password_hash="verify-not-a-hash"))
        session.flush()
        create_default_entitlement(session, user_id)

    ctx.user_ids.append(user_id)
    return user_id


def subscription(ctx, user_id, name, status):
    return SubscriptionState(
        subscription_id=ctx.sub_id(name),
        customer_id=ctx.customer_id(user_id),
        status=status,
        price_id=PRICE,
        cancel_at_period_end=False,
        current_period_end=datetime(2026, 12, 1, tzinfo=timezone.utc),
        kalillac_user_id=str(user_id),
    )


def deliver(ctx, stripe, user_id, event_name, sub_name,
            event_type="customer.subscription.updated"):
    payload = json.dumps(
        {
            "id": ctx.event_id(event_name),
            "type": event_type,
            "subscription": ctx.sub_id(sub_name),
            "customer": ctx.customer_id(user_id),
            "user": str(user_id),
        }
    ).encode()

    return process_webhook(
        payload,
        SIGNATURE,
        gateway=stripe,
        session_factory=ctx.factory,
        paid_price_id=PRICE,
    ).result


def checkout(ctx, stripe, user_id):
    return start_checkout(
        user_id,
        gateway=stripe,
        session_factory=ctx.factory,
        price_id=PRICE,
        **URLS,
    )


def account_state(ctx, user_id):
    with ctx.factory() as session:
        billing = session.get(AccountBilling, user_id)
        entitlement = session.get(AccountEntitlement, user_id)

        return {
            "billing": None if billing is None else {
                c.name: getattr(billing, c.name) for c in AccountBilling.__table__.columns
            },
            "entitlement": {
                c.name: getattr(entitlement, c.name)
                for c in AccountEntitlement.__table__.columns
            },
        }


def event_recorded(ctx, name) -> bool:
    with ctx.factory() as session:
        return session.get(StripeWebhookEvent, ctx.event_id(name)) is not None


@contextmanager
def account_row_locked(ctx, user_id):
    """Hold the account's FOR UPDATE lock on a separate connection. The
    lock is always released when the block exits, including on failure."""

    connection = ctx.engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(account_lock_statement(user_id))
        yield
    finally:
        transaction.rollback()
        connection.close()


# --- checks -------------------------------------------------------------------------------


def check_runtime_privileges(ctx):
    """Runtime role: DML on the two billing tables, nothing more."""

    with ctx.engine.connect() as connection:
        def privilege(sql):
            return connection.execute(text(sql)).scalar()

        for table in ("kalillac.account_billing", "kalillac.stripe_webhook_events"):
            for granted in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert privilege(
                    f"SELECT has_table_privilege(current_user, '{table}', '{granted}')"
                ), f"missing {granted} on {table}"
            for denied in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                assert not privilege(
                    f"SELECT has_table_privilege(current_user, '{table}', '{denied}')"
                ), f"unexpected {denied} on {table}"

        for denied in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not privilege(
                "SELECT has_table_privilege(current_user, "
                f"'kalillac.alembic_version', '{denied}')"
            ), f"unexpected {denied} on alembic_version"

        assert not privilege(
            "SELECT has_schema_privilege(current_user, 'kalillac', 'CREATE')"
        ), "runtime role can CREATE in schema"


def check_cascade_rolled_back(ctx):
    """Deleting a user cascades to its billing row (inside a rolled-back
    transaction, so nothing persists)."""

    user_id = uuid.uuid4()

    with ctx.factory() as session:
        transaction = session.begin()
        try:
            session.add(User(id=user_id, email=ctx.email(), password_hash="x"))
            session.flush()
            session.add(AccountBilling(user_id=user_id, stripe_customer_id=None))
            session.flush()
            session.execute(delete(User).where(User.id == user_id))
            remaining = session.scalar(
                select(func.count()).select_from(AccountBilling).where(
                    AccountBilling.user_id == user_id
                )
            )
            assert remaining == 0, "billing row survived user delete"
        finally:
            transaction.rollback()

    with ctx.factory() as session:
        assert session.get(User, user_id) is None


def check_concurrent_checkout_single_session(ctx):
    user_id = add_user(ctx)
    stripe = FakeStripe(hold_create=ctx.hold())

    workers = [ctx.start(lambda: checkout(ctx, stripe, user_id)) for _ in range(8)]
    time.sleep(HOLD_SECONDS)
    stripe.hold_create.set()
    results = [finish(thread, box) for thread, box in workers]

    with ctx.factory() as session:
        attempt = session.get(AccountBilling, user_id).checkout_attempt_id

    keys = {call["idempotency_key"] for call in stripe.checkout_calls}
    assert keys == {checkout_idempotency_key(user_id, attempt)}, keys
    assert len(stripe.sessions_by_key) == 1
    assert {r.outcome for r in results} == {"url"}
    assert len({r.url for r in results}) == 1


def check_checkout_waits_for_account_lock(ctx):
    user_id = add_user(ctx)
    stripe = FakeStripe()

    with account_row_locked(ctx, user_id):
        thread, box = ctx.start(lambda: checkout(ctx, stripe, user_id))
        time.sleep(HOLD_SECONDS)
        assert thread.is_alive(), "checkout did not wait for the row lock"
        assert stripe.checkout_calls == []

    assert finish(thread, box).outcome == "url"


def check_lock_is_per_account(ctx):
    locked_user = add_user(ctx)
    other_user = add_user(ctx)
    stripe = FakeStripe()

    with account_row_locked(ctx, locked_user):
        # Bounded: the other account must complete while A stays locked.
        thread, box = ctx.start(lambda: checkout(ctx, stripe, other_user))
        thread.join(timeout=2 * HOLD_SECONDS)
        assert not thread.is_alive(), "other account blocked by A's lock"

    assert finish(thread, box).outcome == "url"


def check_duplicate_webhook_reconciles_once(ctx):
    user_id = add_user(ctx)
    stripe = FakeStripe()
    stripe.subscriptions[ctx.sub_id("dup")] = subscription(ctx, user_id, "dup", "active")
    first_fetched = threading.Event()
    release = ctx.hold()

    def held_retrieve(subscription_id):
        if not first_fetched.is_set():
            first_fetched.set()
            wait_or_fail(release, "duplicate-test release")
        return stripe.subscriptions[subscription_id]

    stripe.retrieve_hook = held_retrieve

    one = ctx.start(lambda: deliver(ctx, stripe, user_id, "dup", "dup",
                                    "customer.subscription.created"))
    wait_or_fail(first_fetched, "first delivery to fetch")

    # The second delivery passes the pre-lock check (nothing committed yet)
    # and must then wait on the account row lock.
    two = ctx.start(lambda: deliver(ctx, stripe, user_id, "dup", "dup",
                                    "customer.subscription.created"))
    time.sleep(HOLD_SECONDS)
    assert two[0].is_alive(), "duplicate did not wait for the row lock"

    release.set()
    results = sorted([finish(*one), finish(*two)])

    assert results == ["duplicate", "processed"], results
    assert stripe.retrieve_calls == [ctx.sub_id("dup")]

    with ctx.factory() as session:
        count = session.scalar(
            select(func.count()).select_from(StripeWebhookEvent).where(
                StripeWebhookEvent.event_id == ctx.event_id("dup")
            )
        )
    assert count == 1


def check_fetch_after_lock_ordering(ctx):
    user_id = add_user(ctx)
    stripe = FakeStripe()
    truth = {"status": "active"}
    first_fetched = threading.Event()
    release = ctx.hold()

    def retrieve(subscription_id):
        state = subscription(ctx, user_id, "order", truth["status"])
        if not first_fetched.is_set():
            first_fetched.set()
            wait_or_fail(release, "ordering-test release")
        return state

    stripe.retrieve_hook = retrieve

    older = ctx.start(lambda: deliver(ctx, stripe, user_id, "order_old", "order"))
    wait_or_fail(first_fetched, "older event to fetch")

    truth["status"] = "canceled"
    newer = ctx.start(lambda: deliver(ctx, stripe, user_id, "order_new", "order",
                                      "customer.subscription.deleted"))
    time.sleep(HOLD_SECONDS)

    assert newer[0].is_alive(), "second event was not serialized"
    assert len(stripe.retrieve_calls) == 1, "second event fetched before the lock"

    release.set()
    assert finish(*older) == "processed"
    assert finish(*newer) == "processed"

    state = account_state(ctx, user_id)
    assert state["billing"]["subscription_status"] == "canceled"
    assert state["entitlement"]["tier"] == "free"


def check_rollback_on_fetch_failure(ctx):
    user_id = add_user(ctx)
    before = account_state(ctx, user_id)
    stripe = FakeStripe(fail_retrieve=True)
    stripe.subscriptions[ctx.sub_id("fail")] = subscription(ctx, user_id, "fail", "active")

    try:
        deliver(ctx, stripe, user_id, "fail_fetch", "fail")
    except BillingProviderError:
        pass
    else:
        raise AssertionError("fetch failure did not propagate")

    assert not event_recorded(ctx, "fail_fetch")
    assert account_state(ctx, user_id) == before


def check_rollback_after_partial_write(ctx):
    user_id = add_user(ctx)
    before = account_state(ctx, user_id)
    stripe = FakeStripe()
    stripe.subscriptions[ctx.sub_id("partial")] = subscription(
        ctx, user_id, "partial", "active"
    )

    def failing(*args, **kwargs):
        raise RuntimeError("simulated failure after billing upsert")

    # Restored by the runner after all workers have stopped.
    ctx.patch(service, "set_entitlement_tier", failing)

    try:
        deliver(ctx, stripe, user_id, "partial", "partial")
    except RuntimeError:
        pass
    else:
        raise AssertionError("partial failure did not propagate")

    # The billing upsert in the same transaction was rolled back too.
    assert not event_recorded(ctx, "partial")
    assert account_state(ctx, user_id) == before


def check_older_subscription_protection(ctx):
    user_id = add_user(ctx)
    stripe = FakeStripe()

    stripe.subscriptions[ctx.sub_id("A")] = subscription(ctx, user_id, "A", "active")
    assert deliver(ctx, stripe, user_id, "A_on", "A",
                   "customer.subscription.created") == "processed"
    stripe.subscriptions[ctx.sub_id("A")] = subscription(ctx, user_id, "A", "canceled")
    assert deliver(ctx, stripe, user_id, "A_off", "A",
                   "customer.subscription.deleted") == "processed"
    stripe.subscriptions[ctx.sub_id("B")] = subscription(ctx, user_id, "B", "incomplete")
    assert deliver(ctx, stripe, user_id, "B_on", "B",
                   "customer.subscription.created") == "processed"

    before = account_state(ctx, user_id)
    assert before["billing"]["stripe_subscription_id"] == ctx.sub_id("B")

    # Delayed event for A: Stripe's current state for A is canceled.
    assert deliver(ctx, stripe, user_id, "A_late", "A") == "ignored_historical"
    assert account_state(ctx, user_id) == before
    assert event_recorded(ctx, "A_late")

    # A different nonterminal subscription is a conflict: nothing written.
    stripe.subscriptions[ctx.sub_id("C")] = subscription(ctx, user_id, "C", "active")
    try:
        deliver(ctx, stripe, user_id, "C_on", "C", "customer.subscription.created")
    except ReconciliationConflict:
        pass
    else:
        raise AssertionError("conflict was not raised")

    assert account_state(ctx, user_id) == before
    assert not event_recorded(ctx, "C_on")

    # And Checkout stays blocked for the unresolved subscription.
    calls_before = len(stripe.checkout_calls)
    assert checkout(ctx, stripe, user_id).outcome == "subscription_unresolved"
    assert len(stripe.checkout_calls) == calls_before


CHECKS = [
    check_runtime_privileges,
    check_cascade_rolled_back,
    check_concurrent_checkout_single_session,
    check_checkout_waits_for_account_lock,
    check_lock_is_per_account,
    check_duplicate_webhook_reconciles_once,
    check_fetch_after_lock_ordering,
    check_rollback_on_fetch_failure,
    check_rollback_after_partial_write,
    check_older_subscription_protection,
]


# --- runner ------------------------------------------------------------------------------


def run_checks(ctx, checks) -> int:
    """Run checks in order. After each one, settle every worker before
    anything else happens; abort if any cannot be stopped."""

    failures = 0

    for check in checks:
        try:
            check(ctx)
            print(f"PASS {check.__name__}")
        except BaseException:
            failures += 1
            print(f"FAIL {check.__name__}")
            traceback.print_exc(limit=3)
        finally:
            alive = ctx.settle()

        if alive:
            raise WorkerStillActive(
                f"{len(alive)} worker(s) still active after {check.__name__}"
            )

    return failures


def cleanup(ctx) -> None:
    """Delete only this run's records, then prove none remain."""

    if any(thread.is_alive() for thread in ctx.workers):
        raise WorkerStillActive("refusing cleanup while workers are active")

    email_prefix = f"billing-verify+{ctx.run_id}-"
    event_prefix = f"evt_verify_{ctx.run_id}_"

    def email_match():
        return func.substr(User.email, 1, len(email_prefix)) == email_prefix

    def event_match():
        return (
            func.substr(StripeWebhookEvent.event_id, 1, len(event_prefix))
            == event_prefix
        )

    with ctx.factory() as session, session.begin():
        session.execute(delete(StripeWebhookEvent).where(event_match()))
        # Users cascade to entitlement, billing, sessions, and usage rows.
        session.execute(delete(User).where(email_match()))

    with ctx.factory() as session:
        leftover_users = session.scalar(
            select(func.count()).select_from(User).where(email_match())
        )
        leftover_events = session.scalar(
            select(func.count()).select_from(StripeWebhookEvent).where(event_match())
        )
        leftover_billing = session.scalar(
            select(func.count()).select_from(AccountBilling).where(
                AccountBilling.user_id.in_(ctx.user_ids)
            )
        ) if ctx.user_ids else 0

    print(
        "cleanup: remaining tagged users=%d events=%d billing=%d"
        % (leftover_users, leftover_events, leftover_billing)
    )

    if leftover_users or leftover_events or leftover_billing:
        raise SystemExit("CLEANUP INCOMPLETE")


def execute(ctx, checks) -> int:
    """Run checks, then clean up only if every worker has stopped."""

    try:
        failures = run_checks(ctx, checks)
    except WorkerStillActive as exc:
        print(f"ABORT: {exc}")
        print(
            "cleanup SKIPPED while workers may still hold connections; "
            f"run the manual cleanup for run id {ctx.run_id}"
        )
        return EXIT_ABORTED

    cleanup(ctx)
    print(f"RESULT: {len(checks) - failures}/{len(checks)} checks passed")
    return EXIT_FAILED if failures else EXIT_OK


# --- startup validation, connection, main ------------------------------------------------


def refuse(message: str):
    print(f"REFUSING: {message}")
    raise SystemExit(EXIT_REFUSED)


def require_assertions() -> None:
    if sys.flags.optimize or not __debug__:
        refuse("Python optimization (-O) is enabled; assertions would be skipped.")


def validate_destination(args, environ) -> None:
    """Staging only. Checked before any password prompt or connection."""

    if args.expect_db != REQUIRED_DATABASE or args.expect_user != REQUIRED_ROLE:
        refuse(
            f"this harness only runs against {REQUIRED_DATABASE} as "
            f"{REQUIRED_ROLE}; --expect-db/--expect-user must name exactly those."
        )

    if environ.get("KALILLAC_DATABASE_URL"):
        refuse("KALILLAC_DATABASE_URL is set; unset it and use KALILLAC_DB_* settings.")

    if environ.get("KALILLAC_DB_NAME") != REQUIRED_DATABASE:
        refuse(f"KALILLAC_DB_NAME must be {REQUIRED_DATABASE}.")

    if environ.get("KALILLAC_DB_USER") != REQUIRED_ROLE:
        refuse(f"KALILLAC_DB_USER must be {REQUIRED_ROLE}.")


def build_engine(environ):
    password = environ.get("KALILLAC_DB_PASSWORD") or getpass.getpass(
        f"{REQUIRED_ROLE} password: "
    )

    url = URL.create(
        drivername="postgresql+psycopg",
        username=REQUIRED_ROLE,
        password=password,
        host=environ.get("KALILLAC_DB_HOST", "127.0.0.1"),
        port=int(environ.get("KALILLAC_DB_PORT", "5432")),
        database=REQUIRED_DATABASE,
    )

    return create_engine(
        url,
        pool_size=12,
        max_overflow=4,
        pool_timeout=10,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 5,
            "application_name": "kalillac-billing-verify",
            # Applied to every harness connection at session start.
            "options": (
                f"-c lock_timeout={LOCK_TIMEOUT} "
                f"-c statement_timeout={STATEMENT_TIMEOUT}"
            ),
        },
    )


def guard(engine) -> None:
    """Confirm the live connection is the required destination."""

    with engine.connect() as connection:
        database, user = connection.execute(
            text("SELECT current_database(), current_user")
        ).one()
        lock_timeout = connection.execute(text("SHOW lock_timeout")).scalar()
        statement_timeout = connection.execute(text("SHOW statement_timeout")).scalar()
        tables = connection.execute(
            text(
                "SELECT to_regclass('kalillac.account_billing') IS NOT NULL, "
                "to_regclass('kalillac.stripe_webhook_events') IS NOT NULL"
            )
        ).one()

    if database != REQUIRED_DATABASE or user != REQUIRED_ROLE:
        refuse(
            f"connected to database {database!r} as {user!r}; "
            f"required {REQUIRED_DATABASE!r} as {REQUIRED_ROLE!r}."
        )

    if lock_timeout != LOCK_TIMEOUT or statement_timeout != STATEMENT_TIMEOUT:
        refuse(
            f"session timeouts not applied (lock_timeout={lock_timeout}, "
            f"statement_timeout={statement_timeout})."
        )

    if not all(tables):
        refuse("migration 0004 tables are not present.")

    print(
        f"guard ok: database={database} user={user} "
        f"lock_timeout={lock_timeout} statement_timeout={statement_timeout}"
    )


def main(argv=None) -> int:
    require_assertions()

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--expect-db", required=True)
    parser.add_argument("--expect-user", required=True)
    args = parser.parse_args(argv)

    validate_destination(args, os.environ)

    engine = build_engine(os.environ)

    try:
        guard(engine)

        ctx = Context(
            engine=engine,
            factory=sessionmaker(bind=engine, expire_on_commit=False),
            run_id=uuid.uuid4().hex[:12],
        )
        print(f"run id: {ctx.run_id}")

        return execute(ctx, CHECKS)
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
