"""Reconcile one Kalillac account's subscription from its stored evidence.

Operator command only; there is no HTTP route for this. It runs
kalillac_billing.service.reconcile_account for exactly one account, which
re-reads from Stripe only the subscription or Checkout Session that account
already has stored, under the same account lock and policy as webhooks.

Refused before any database or Stripe client is built:
- anything other than exactly one canonical user UUID;
- a missing --apply flag (an accidental invocation cannot write);
- --expect-stripe-mode differing from KALILLAC_STRIPE_MODE;
- billing disabled or its configuration invalid.
There is deliberately no --all.

Configuration comes from the application's own loaders: billing from
load_billing_config, the database from kalillac_db.engine. Output is only
stable outcome codes and a success/failure line: never an email address,
Stripe id, Price, provider response, secret, database URL, or exception
text.

Usage (from the candidate/ directory, with the application's environment):
    python tools/reconcile_billing_account.py <user-uuid> \\
        --expect-stripe-mode test --apply

Exit codes:
  0 a stable outcome (subscription_reconciled, checkout_recovered,
    checkout_open, checkout_expired, nothing_to_reconcile)
  1 unexpected_error (rolled back), or cleanup_failure: disposing database
    resources failed, so the otherwise intended result is not printed
  2 refused (arguments, mode, configuration, or account not found)
  3 Stripe failed (rolled back)
  4 a paying subscription without a valid period end (rolled back)
  5 two different nonterminal subscriptions claim the account (rolled back)
  6 recovery refused: ownership or provider state not proven (rolled back)
  7 reconciliation_busy: the account lock or a statement exceeded the fixed
    operator limits (lock 5s, statement 30s; not configurable; rolled back)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Callable
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalillac_billing.config import (  # noqa: E402
    STRIPE_MODES,
    BillingConfig,
    BillingConfigError,
    billing_enabled,
    load_billing_config,
)
from kalillac_billing.gateway import BillingProviderError  # noqa: E402
from kalillac_billing.service import (  # noqa: E402
    ReconciliationBusy,
    ReconciliationConflict,
    ReconciliationRefused,
    SubscriptionStateInvalid,
    reconcile_account,
)


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_PROVIDER_FAILURE = 3
EXIT_INVALID_SUBSCRIPTION_STATE = 4
EXIT_RECONCILIATION_CONFLICT = 5
EXIT_RECOVERY_REFUSED = 6
EXIT_RECONCILIATION_BUSY = 7

_CANONICAL_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


class Refused(Exception):
    """Refused before or while opening resources. Carries a stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's own messages can echo what was typed.
        raise Refused("invalid_arguments")


@dataclass(frozen=True)
class Database:
    session_factory: object
    dispose: Callable[[], None]


def open_database() -> Database:
    """The application's own engine; no connection is made until used."""

    from kalillac_db.config import DatabaseConfigError
    from kalillac_db.engine import get_engine, get_session_factory

    try:
        engine = get_engine()
    except DatabaseConfigError:
        raise Refused("database_config_invalid") from None

    if engine is None:
        raise Refused("database_disabled")

    return Database(get_session_factory(), engine.dispose)


def make_gateway(config: BillingConfig):
    from kalillac_billing.stripe_gateway import StripeGateway

    return StripeGateway(config)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="reconcile_billing_account.py",
        description="Reconcile one account's subscription.",
    )
    parser.add_argument("user_id", help="the Kalillac user UUID")
    parser.add_argument(
        "--expect-stripe-mode",
        required=True,
        choices=STRIPE_MODES,
        help="must equal KALILLAC_STRIPE_MODE",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="required: reconciliation writes billing and entitlement",
    )
    return parser


def _user_id(value: str) -> uuid.UUID:
    if not _CANONICAL_UUID.fullmatch(value.lower()):
        raise Refused("invalid_user_id")

    return uuid.UUID(value)


def _billing_config(expected_mode: str) -> BillingConfig:
    if not billing_enabled():
        raise Refused("billing_disabled")

    try:
        config = load_billing_config()
    except BillingConfigError:
        raise Refused("billing_config_invalid") from None

    if config.mode != expected_mode:
        raise Refused("stripe_mode_mismatch")

    return config


def _report(kind: str, code: str, exit_code: int) -> int:
    print(f"{kind}: {code}")
    print("RESULT: success" if exit_code == EXIT_OK else "RESULT: failure")
    return exit_code


def _reconcile(opened, user_id, config, open_database, make_gateway):
    """The stable (kind, code, exit code) result, without printing it. Every
    Exception maps to a result; an opened database is left in `opened` for
    the caller to dispose before reporting."""

    try:
        opened.append(open_database())
        outcome = reconcile_account(
            user_id,
            gateway=make_gateway(config),
            session_factory=opened[0].session_factory,
            allowed_price_ids=config.allowed_price_ids,
        )
    except Refused as refused:
        return "REFUSED", refused.code, EXIT_REFUSED
    except BillingProviderError:
        return "FAILED", "provider_failure", EXIT_PROVIDER_FAILURE
    except SubscriptionStateInvalid:
        return "FAILED", "subscription_state_invalid", EXIT_INVALID_SUBSCRIPTION_STATE
    except ReconciliationConflict:
        return "FAILED", "reconciliation_conflict", EXIT_RECONCILIATION_CONFLICT
    except ReconciliationRefused as refused:
        return "REFUSED", refused.code, EXIT_RECOVERY_REFUSED
    except ReconciliationBusy:
        return "FAILED", "reconciliation_busy", EXIT_RECONCILIATION_BUSY
    except Exception:
        # The class and message could carry provider or database detail.
        return "FAILED", "unexpected_error", EXIT_FAILED

    if outcome.result == "account_not_found":
        return "REFUSED", "account_not_found", EXIT_REFUSED

    return "OUTCOME", outcome.result, EXIT_OK


def main(
    argv=None,
    *,
    open_database: Callable[[], Database] = open_database,
    make_gateway: Callable[[BillingConfig], object] = make_gateway,
) -> int:
    # Everything here is validated before any database or Stripe client
    # exists.
    try:
        args = _parser().parse_args(argv)
        user_id = _user_id(args.user_id)

        if not args.apply:
            raise Refused("apply_flag_required")

        config = _billing_config(args.expect_stripe_mode)
    except Refused as refused:
        return _report("REFUSED", refused.code, EXIT_REFUSED)

    opened = []

    try:
        result = _reconcile(opened, user_id, config, open_database, make_gateway)
    except BaseException:
        # An interrupt or exit is not a tool result: attempt cleanup without
        # letting it mask the original, report nothing, and re-raise.
        if opened:
            try:
                opened[0].dispose()
            except Exception:
                pass
        raise

    # Reported only once cleanup is known to have succeeded.
    if opened:
        try:
            opened[0].dispose()
        except Exception:
            return _report("FAILED", "cleanup_failure", EXIT_FAILED)

    return _report(*result)


if __name__ == "__main__":
    sys.exit(main())
