"""The single-account operator command, tools/reconcile_billing_account.py.

Every refusal happens before a database or Stripe client is built; every
line it prints is a stable code; database resources are disposed on every
exit path. The database and gateway are injected, so nothing here contacts
Stripe or PostgreSQL.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

import kalillac_billing.service as service
from kalillac_billing.service import ReconciliationConflict
from kalillac_db.models import AccountBilling, Base, StripeWebhookEvent

from account_test_db import make_session_factory, make_sqlite_engine
from test_billing import BILLING_ENV, PAID_EXPIRY, FakeGateway, _subscription
from test_billing_reconciliation import (
    _database_timeout,
    _add_user,
    _entitlement,
    _link,
    _snapshot,
    _stored_checkout,
)


CANDIDATE_DIR = Path(__file__).resolve().parents[1]
TOOL_PATH = CANDIDATE_DIR / "tools" / "reconcile_billing_account.py"

_spec = importlib.util.spec_from_file_location("reconcile_billing_account", TOOL_PATH)
tool = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = tool
_spec.loader.exec_module(tool)

# The only lines the command may ever print.
ALLOWED_LINE = re.compile(r"(OUTCOME|REFUSED|FAILED): [a-z_]+|RESULT: (success|failure)")

SECRET_KEY = BILLING_ENV["KALILLAC_STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = BILLING_ENV["KALILLAC_STRIPE_WEBHOOK_SECRET"]
DATABASE_URL = BILLING_ENV["KALILLAC_DATABASE_URL"]


@pytest.fixture(autouse=True)
def billing_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("KALILLAC_") or key == "CREDENTIALS_DIRECTORY":
            monkeypatch.delenv(key)

    for key, value in BILLING_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)

    yield make_session_factory(engine)

    engine.dispose()


@pytest.fixture
def gateway():
    return FakeGateway()


class Resources:
    """Injected database and gateway that record their own lifecycle."""

    def __init__(self, factory, gateway):
        self.factory = factory
        self.gateway = gateway
        self.opened = 0
        self.disposed = 0
        self.dispose_error = None
        self.gateways = []

    def open_database(self):
        self.opened += 1
        return tool.Database(self.factory, self._dispose)

    def _dispose(self):
        self.disposed += 1
        if self.dispose_error is not None:
            raise self.dispose_error

    def make_gateway(self, config):
        self.gateways.append(config.mode)
        return self.gateway

    def run(self, *argv):
        return tool.main(
            list(argv),
            open_database=self.open_database,
            make_gateway=self.make_gateway,
        )


@pytest.fixture
def resources(db, gateway):
    return Resources(db, gateway)


def _lines(capsys):
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    for line in lines:
        assert ALLOWED_LINE.fullmatch(line), line
    return lines


# --- refused before any database or Stripe client -----------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "not-a-uuid",
        "person@example.com",
        "",
        "12345",
        "{%s}" % uuid.uuid4(),
        "urn:uuid:%s" % uuid.uuid4(),
        uuid.uuid4().hex,
        str(uuid.uuid4()) + " ",
        "sub_1",
    ],
    ids=["word", "email", "empty", "number", "braces", "urn", "hex", "space", "stripe-id"],
)
def test_malformed_uuid_is_refused_before_database_or_stripe(resources, capsys, value):
    exit_code = resources.run(value, "--expect-stripe-mode", "test", "--apply")

    assert exit_code == tool.EXIT_REFUSED
    assert _lines(capsys) == ["REFUSED: invalid_user_id", "RESULT: failure"]
    assert resources.opened == 0
    assert resources.gateways == []


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--expect-stripe-mode", "test", "--apply"],
        [str(uuid.uuid4()), str(uuid.uuid4()), "--expect-stripe-mode", "test", "--apply"],
        ["--all", "--expect-stripe-mode", "test", "--apply"],
        [str(uuid.uuid4()), "--all", "--expect-stripe-mode", "test", "--apply"],
        [str(uuid.uuid4()), "--apply"],
        [str(uuid.uuid4()), "--expect-stripe-mode", "production", "--apply"],
        [str(uuid.uuid4()), "--expect-stripe-mode", "test", "--apply",
         "--subscription", "sub_1"],
    ],
    ids=["nothing", "no-user", "two-users", "all", "user-and-all", "no-mode",
         "bad-mode", "subscription-id"],
)
def test_invalid_arguments_are_refused_without_echoing_them(resources, capsys, argv):
    exit_code = resources.run(*argv)

    assert exit_code == tool.EXIT_REFUSED
    assert _lines(capsys) == ["REFUSED: invalid_arguments", "RESULT: failure"]
    assert resources.opened == 0
    assert resources.gateways == []


def test_the_apply_flag_is_required(resources, db, gateway, capsys):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    before = _snapshot(db, user_id)
    gateway.checkout_retrievals.clear()

    exit_code = resources.run(str(user_id), "--expect-stripe-mode", "test")

    assert exit_code == tool.EXIT_REFUSED
    assert _lines(capsys) == ["REFUSED: apply_flag_required", "RESULT: failure"]
    assert resources.opened == 0
    assert resources.gateways == []
    assert gateway.checkout_retrievals == gateway.retrieve_calls == []
    assert _snapshot(db, user_id) == before


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("test", "live"), ("live", "test")],
)
def test_a_stripe_mode_mismatch_is_refused(resources, capsys, monkeypatch,
                                           configured, expected):
    monkeypatch.setenv("KALILLAC_STRIPE_MODE", configured)
    monkeypatch.setenv("KALILLAC_STRIPE_SECRET_KEY", f"sk_{configured}_secretvalue123")

    exit_code = resources.run(
        str(uuid.uuid4()), "--expect-stripe-mode", expected, "--apply"
    )

    assert exit_code == tool.EXIT_REFUSED
    assert _lines(capsys) == ["REFUSED: stripe_mode_mismatch", "RESULT: failure"]
    assert resources.opened == 0
    assert resources.gateways == []


def test_billing_disabled_is_refused(resources, capsys, monkeypatch):
    monkeypatch.delenv("KALILLAC_BILLING_ENABLED")

    exit_code = resources.run(str(uuid.uuid4()), "--expect-stripe-mode", "test", "--apply")

    assert exit_code == tool.EXIT_REFUSED
    assert _lines(capsys) == ["REFUSED: billing_disabled", "RESULT: failure"]
    assert resources.opened == 0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KALILLAC_STRIPE_ALLOWED_PRICE_IDS", ""),
        ("KALILLAC_STRIPE_MODE", ""),
        ("KALILLAC_STRIPE_SECRET_KEY", "sk_live_secretvalue123"),
    ],
)
def test_invalid_billing_configuration_is_refused(resources, capsys, monkeypatch,
                                                  name, value):
    monkeypatch.setenv(name, value)

    exit_code = resources.run(str(uuid.uuid4()), "--expect-stripe-mode", "test", "--apply")

    assert exit_code == tool.EXIT_REFUSED
    assert _lines(capsys) == ["REFUSED: billing_config_invalid", "RESULT: failure"]
    assert resources.opened == 0
    assert resources.gateways == []


def test_the_real_database_loader_refuses_when_the_database_is_disabled(gateway, capsys,
                                                                       monkeypatch):
    monkeypatch.delenv("KALILLAC_DB_ENABLED")
    built = []

    exit_code = tool.main(
        [str(uuid.uuid4()), "--expect-stripe-mode", "test", "--apply"],
        make_gateway=lambda config: built.append(config) or gateway,
    )

    assert exit_code == tool.EXIT_REFUSED
    assert _lines(capsys) == ["REFUSED: database_disabled", "RESULT: failure"]
    assert built == []


def test_the_script_refuses_a_bad_uuid_and_all_from_the_command_line():
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("KALILLAC_")}
    env.update(BILLING_ENV)

    for argv in (
        ["person@example.com", "--expect-stripe-mode", "test", "--apply"],
        ["--all", "--expect-stripe-mode", "test", "--apply"],
    ):
        result = subprocess.run(
            [sys.executable, str(TOOL_PATH), *argv],
            cwd=CANDIDATE_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode == tool.EXIT_REFUSED
        assert result.stderr == ""
        lines = result.stdout.splitlines()
        assert lines[-1] == "RESULT: failure"
        assert all(ALLOWED_LINE.fullmatch(line) for line in lines), lines


# --- applied outcomes --------------------------------------------------------------------


def test_recovers_a_missed_webhook_for_exactly_one_account(resources, db, gateway, capsys):
    user_id = _add_user(db)
    other_id = _add_user(db, "other@example.com")
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id)
    _link(db, other_id, subscription="sub_other", customer="cus_other")
    other_before = _snapshot(db, other_id)
    gateway.checkout_retrievals.clear()

    exit_code = resources.run(str(user_id), "--expect-stripe-mode", "test", "--apply")

    assert exit_code == tool.EXIT_OK
    assert _lines(capsys) == ["OUTCOME: checkout_recovered", "RESULT: success"]
    assert _entitlement(db, user_id) == ("paid", "stripe", PAID_EXPIRY)
    assert _snapshot(db, other_id) == other_before
    assert gateway.checkout_retrievals == [session]
    assert gateway.retrieve_calls == ["sub_1"]
    assert resources.gateways == ["test"]
    assert (resources.opened, resources.disposed) == (1, 1)

    with db() as check:
        assert check.query(StripeWebhookEvent).count() == 0


def test_accepts_an_uppercase_canonical_uuid(resources, db, gateway, capsys):
    user_id = _add_user(db)

    exit_code = resources.run(
        str(user_id).upper(), "--expect-stripe-mode", "test", "--apply"
    )

    assert exit_code == tool.EXIT_OK
    assert _lines(capsys) == ["OUTCOME: nothing_to_reconcile", "RESULT: success"]


@pytest.mark.parametrize(
    ("prepare", "outcome"),
    [
        ("linked", "subscription_reconciled"),
        ("open", "checkout_open"),
        ("expired", "checkout_expired"),
        ("nothing", "nothing_to_reconcile"),
    ],
)
def test_stable_outcomes_exit_zero(resources, db, gateway, capsys, prepare, outcome):
    user_id = _add_user(db)

    if prepare == "linked":
        _link(db, user_id)
        gateway.subscriptions["sub_1"] = _subscription(user_id)
    elif prepare in {"open", "expired"}:
        session = _stored_checkout(db, gateway, user_id)
        if prepare == "expired":
            gateway.set_checkout_status(session, "expired")

    exit_code = resources.run(str(user_id), "--expect-stripe-mode", "test", "--apply")

    assert exit_code == tool.EXIT_OK
    assert _lines(capsys) == [f"OUTCOME: {outcome}", "RESULT: success"]
    assert resources.disposed == 1


def test_missing_account_is_refused(resources, capsys):
    exit_code = resources.run(str(uuid.uuid4()), "--expect-stripe-mode", "test", "--apply")

    assert exit_code == tool.EXIT_REFUSED
    assert _lines(capsys) == ["REFUSED: account_not_found", "RESULT: failure"]
    assert resources.disposed == 1


# --- failures: distinct exit codes, rolled back, disposed --------------------------------


def _completed_checkout(db, gateway, **subscription):
    user_id = _add_user(db)
    session = _stored_checkout(db, gateway, user_id)
    gateway.set_checkout_status(session, "complete", "sub_1")
    gateway.subscriptions["sub_1"] = _subscription(user_id, **subscription)
    return user_id


def test_provider_failure_has_its_own_exit_code(resources, db, gateway, capsys):
    user_id = _completed_checkout(db, gateway)
    before = _snapshot(db, user_id)
    gateway.fail_provider = True

    exit_code = resources.run(str(user_id), "--expect-stripe-mode", "test", "--apply")

    assert exit_code == tool.EXIT_PROVIDER_FAILURE == 3
    assert _lines(capsys) == ["FAILED: provider_failure", "RESULT: failure"]
    assert _snapshot(db, user_id) == before
    assert resources.disposed == 1


def test_invalid_subscription_state_has_its_own_exit_code(resources, db, gateway, capsys):
    user_id = _completed_checkout(db, gateway, current_period_end=None)
    before = _snapshot(db, user_id)

    exit_code = resources.run(str(user_id), "--expect-stripe-mode", "test", "--apply")

    assert exit_code == tool.EXIT_INVALID_SUBSCRIPTION_STATE == 4
    assert _lines(capsys) == ["FAILED: subscription_state_invalid", "RESULT: failure"]
    assert _snapshot(db, user_id) == before
    assert resources.disposed == 1


def test_reconciliation_conflict_has_its_own_exit_code(resources, db, capsys, monkeypatch):
    # reconcile_account links only when nothing live is linked, so the
    # shared policy's conflict is injected here to pin the mapping.
    def conflict(*args, **kwargs):
        raise ReconciliationConflict()

    monkeypatch.setattr(tool, "reconcile_account", conflict)

    exit_code = resources.run(str(uuid.uuid4()), "--expect-stripe-mode", "test", "--apply")

    assert exit_code == tool.EXIT_RECONCILIATION_CONFLICT == 5
    assert _lines(capsys) == ["FAILED: reconciliation_conflict", "RESULT: failure"]
    assert resources.disposed == 1


@pytest.mark.parametrize(
    ("subscription", "code"),
    [
        ({"kalillac_user_id": None}, "owner_metadata_missing"),
        ({"kalillac_user_id": str(uuid.uuid4())}, "owner_metadata_mismatch"),
    ],
)
def test_recovery_refusal_has_its_own_exit_code(resources, db, gateway, capsys,
                                                subscription, code):
    user_id = _completed_checkout(db, gateway, **subscription)
    before = _snapshot(db, user_id)

    exit_code = resources.run(str(user_id), "--expect-stripe-mode", "test", "--apply")

    assert exit_code == tool.EXIT_RECOVERY_REFUSED == 6
    assert _lines(capsys) == [f"REFUSED: {code}", "RESULT: failure"]
    assert _snapshot(db, user_id) == before
    assert resources.disposed == 1


def test_exit_codes_are_distinct():
    codes = [
        tool.EXIT_OK,
        tool.EXIT_FAILED,
        tool.EXIT_REFUSED,
        tool.EXIT_PROVIDER_FAILURE,
        tool.EXIT_INVALID_SUBSCRIPTION_STATE,
        tool.EXIT_RECONCILIATION_CONFLICT,
        tool.EXIT_RECOVERY_REFUSED,
        tool.EXIT_RECONCILIATION_BUSY,
    ]

    assert len(set(codes)) == len(codes)
    assert codes == [0, 1, 2, 3, 4, 5, 6, 7]


def test_unexpected_error_prints_no_exception_text(resources, db, capsys):
    user_id = _add_user(db)
    secret_message = (
        f"boom person@example.com sub_1 cus_1 cs_test_1 price_kalillacpaid "
        f"{SECRET_KEY} {WEBHOOK_SECRET} {DATABASE_URL}"
    )

    def failing_gateway(config):
        raise RuntimeError(secret_message)

    exit_code = tool.main(
        [str(user_id), "--expect-stripe-mode", "test", "--apply"],
        open_database=resources.open_database,
        make_gateway=failing_gateway,
    )

    output = capsys.readouterr()
    assert exit_code == tool.EXIT_FAILED
    assert output.out.splitlines() == ["FAILED: unexpected_error", "RESULT: failure"]
    assert output.err == ""
    assert resources.disposed == 1


def test_interrupts_are_not_swallowed_and_still_dispose(resources, db, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(tool, "reconcile_account", interrupted)

    with pytest.raises(KeyboardInterrupt):
        resources.run(str(uuid.uuid4()), "--expect-stripe-mode", "test", "--apply")

    assert resources.disposed == 1


def test_a_database_refusal_while_opening_is_reported(resources, capsys):
    def refusing():
        raise tool.Refused("database_config_invalid")

    exit_code = tool.main(
        [str(uuid.uuid4()), "--expect-stripe-mode", "test", "--apply"],
        open_database=refusing,
        make_gateway=resources.make_gateway,
    )

    assert exit_code == tool.EXIT_REFUSED
    assert _lines(capsys) == ["REFUSED: database_config_invalid", "RESULT: failure"]
    assert resources.gateways == []


# --- privacy ----------------------------------------------------------------------------


def test_no_run_prints_an_identifier_secret_or_url(resources, db, gateway, capsys):
    email = "person@example.com"
    user_id = _completed_checkout(db, gateway)
    forbidden = [
        email, str(user_id), "sub_1", "cus_1", "cs_test_1", "price_kalillacpaid",
        SECRET_KEY, WEBHOOK_SECRET, DATABASE_URL, "Traceback",
    ]
    outputs = []

    for argv in (
        [email, "--expect-stripe-mode", "test", "--apply"],
        [str(user_id), "--expect-stripe-mode", "live", "--apply"],
        [str(user_id), "--expect-stripe-mode", "test"],
        [str(user_id), "--expect-stripe-mode", "test", "--apply"],
        [str(user_id), "--expect-stripe-mode", "test", "--apply"],
    ):
        resources.run(*argv)
        outputs.append(capsys.readouterr())

    gateway.fail_provider = True
    resources.run(str(user_id), "--expect-stripe-mode", "test", "--apply")
    outputs.append(capsys.readouterr())

    with db() as check:
        assert check.get(AccountBilling, user_id).stripe_subscription_id == "sub_1"

    for output in outputs:
        assert output.err == ""
        for line in output.out.splitlines():
            assert ALLOWED_LINE.fullmatch(line), line
        for value in forbidden:
            assert value not in output.out


# --- bounded waiting -------------------------------------------------------------------


@pytest.mark.parametrize("sqlstate", ["55P03", "57014"],
                         ids=["lock-timeout", "statement-timeout"])
@pytest.mark.parametrize("where", ["lock", "entitlement-write"])
def test_a_database_timeout_is_reconciliation_busy(resources, db, gateway, capsys,
                                                    monkeypatch, sqlstate, where):
    user_id = _completed_checkout(db, gateway)
    before = _snapshot(db, user_id)
    gateway.checkout_retrievals.clear()

    def timed_out(*args, **kwargs):
        raise _database_timeout(sqlstate)

    monkeypatch.setattr(
        service, "lock_account" if where == "lock" else "set_entitlement_tier", timed_out
    )

    exit_code = resources.run(str(user_id), "--expect-stripe-mode", "test", "--apply")

    assert exit_code == tool.EXIT_RECONCILIATION_BUSY == 7
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.splitlines() == ["FAILED: reconciliation_busy", "RESULT: failure"]
    for value in ("SELECT", "FOR UPDATE", "timeout", str(user_id), "person@example.com",
                  DATABASE_URL, "sub_1", "cus_1", "cs_test", "price_"):
        assert value not in output.out
    # Rolled back: billing, pending Checkout and entitlement as they were.
    assert _snapshot(db, user_id) == before
    assert resources.disposed == 1
    if where == "lock":
        assert gateway.checkout_retrievals == gateway.retrieve_calls == []


def test_timeouts_are_not_operator_arguments(resources, capsys):
    for flag in ("--lock-timeout", "--statement-timeout"):
        exit_code = resources.run(
            str(uuid.uuid4()), "--expect-stripe-mode", "test", "--apply", flag, "0"
        )

        assert exit_code == tool.EXIT_REFUSED
        assert _lines(capsys) == ["REFUSED: invalid_arguments", "RESULT: failure"]

    assert resources.opened == 0


# --- cleanup before reporting -----------------------------------------------------------


CLEANUP_FAILURE = ["FAILED: cleanup_failure", "RESULT: failure"]


def _revealing_error(user_id):
    return RuntimeError(
        f"could not close connection to {DATABASE_URL} for person@example.com "
        f"{user_id} after SELECT kalillac.users.id FROM kalillac.users FOR UPDATE; "
        f"sub_1 cus_1 cs_test_1 price_kalillacpaid {SECRET_KEY} {WEBHOOK_SECRET}"
    )


def _prepare(kind, db, gateway, monkeypatch):
    """An account and gateway set up for one intended result."""

    if kind == "account_not_found":
        return uuid.uuid4()

    user_id = _completed_checkout(
        db, gateway, **({"kalillac_user_id": None} if kind == "recovery_refused" else {})
    )
    if kind == "provider_failure":
        gateway.fail_provider = True
    elif kind == "invalid_state":
        gateway.subscriptions["sub_1"] = _subscription(user_id, current_period_end=None)
    elif kind == "busy":
        def timed_out(*args, **kwargs):
            raise _database_timeout("55P03")
        monkeypatch.setattr(service, "lock_account", timed_out)
    elif kind == "unexpected":
        def broken(*args, **kwargs):
            raise RuntimeError("unexpected")
        monkeypatch.setattr(service, "set_entitlement_tier", broken)
    return user_id


INTENDED = [
    ("success", tool.EXIT_OK, ["OUTCOME: checkout_recovered", "RESULT: success"]),
    ("provider_failure", tool.EXIT_PROVIDER_FAILURE,
     ["FAILED: provider_failure", "RESULT: failure"]),
    ("invalid_state", tool.EXIT_INVALID_SUBSCRIPTION_STATE,
     ["FAILED: subscription_state_invalid", "RESULT: failure"]),
    ("recovery_refused", tool.EXIT_RECOVERY_REFUSED,
     ["REFUSED: owner_metadata_missing", "RESULT: failure"]),
    ("busy", tool.EXIT_RECONCILIATION_BUSY,
     ["FAILED: reconciliation_busy", "RESULT: failure"]),
    ("unexpected", tool.EXIT_FAILED, ["FAILED: unexpected_error", "RESULT: failure"]),
    ("account_not_found", tool.EXIT_REFUSED,
     ["REFUSED: account_not_found", "RESULT: failure"]),
]


@pytest.mark.parametrize(("kind", "exit_code", "lines"), INTENDED, ids=[i[0] for i in INTENDED])
def test_every_opened_path_disposes_once_and_keeps_its_output(
    resources, db, gateway, capsys, monkeypatch, kind, exit_code, lines
):
    user_id = _prepare(kind, db, gateway, monkeypatch)

    assert resources.run(str(user_id), "--expect-stripe-mode", "test", "--apply") == exit_code
    assert _lines(capsys) == lines
    assert (resources.opened, resources.disposed) == (1, 1)


@pytest.mark.parametrize(("kind", "exit_code", "lines"), INTENDED, ids=[i[0] for i in INTENDED])
def test_a_disposal_failure_replaces_the_intended_result_with_cleanup_failure(
    resources, db, gateway, capsys, monkeypatch, kind, exit_code, lines
):
    user_id = _prepare(kind, db, gateway, monkeypatch)
    resources.dispose_error = _revealing_error(user_id)

    result = resources.run(str(user_id), "--expect-stripe-mode", "test", "--apply")

    assert result == tool.EXIT_FAILED == 1
    # Only the cleanup failure: nothing of the earlier intended result.
    assert _lines(capsys) == CLEANUP_FAILURE
    assert resources.disposed == 1


def test_a_disposal_failure_leaks_nothing_it_carries(resources, db, gateway, capsys):
    user_id = _completed_checkout(db, gateway)
    resources.dispose_error = _revealing_error(user_id)

    assert resources.run(str(user_id), "--expect-stripe-mode", "test", "--apply") == 1

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.splitlines() == CLEANUP_FAILURE
    for value in ("RuntimeError", "could not close", "Traceback", DATABASE_URL,
                  "postgresql", "person@example.com", str(user_id), "SELECT",
                  "FOR UPDATE", "sub_1", "cus_1", "cs_test_1", "price_kalillacpaid",
                  SECRET_KEY, WEBHOOK_SECRET):
        assert value not in output.out


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit],
                         ids=["keyboard-interrupt", "system-exit"])
@pytest.mark.parametrize("dispose_fails", [False, True], ids=["dispose-ok", "dispose-fails"])
def test_an_interrupt_is_preserved_and_disposal_still_attempted_once(
    resources, db, capsys, monkeypatch, interrupt, dispose_fails
):
    original = interrupt()

    def interrupted(*args, **kwargs):
        raise original

    monkeypatch.setattr(tool, "reconcile_account", interrupted)
    if dispose_fails:
        resources.dispose_error = _revealing_error("u")

    with pytest.raises(interrupt) as raised:
        resources.run(str(uuid.uuid4()), "--expect-stripe-mode", "test", "--apply")

    # The very same interrupt, never replaced by the disposal failure.
    assert raised.value is original
    assert resources.disposed == 1
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_an_interrupt_while_opening_has_nothing_to_dispose(resources, capsys):
    def interrupted():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        tool.main(
            [str(uuid.uuid4()), "--expect-stripe-mode", "test", "--apply"],
            open_database=interrupted,
            make_gateway=resources.make_gateway,
        )

    assert resources.disposed == 0
    assert capsys.readouterr().out == ""
