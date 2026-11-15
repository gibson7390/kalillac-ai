"""Review refunds and disputes Stripe reported (billing_cases).

Operator command only; there is no HTTP route for this. Kalillac never issues a refund and never
changes an entitlement because of a case: the operator decides in Stripe (cancel a subscription,
respond to a dispute) and records here that the case was seen or settled.

Usage (from the candidate/ directory, with the application's database environment):
    python tools/billing_cases.py list [--all]
    python tools/billing_cases.py acknowledge <case-uuid> --apply
    python tools/billing_cases.py close <case-uuid> --apply

Output is one line per case: its id, kind, Stripe's status, the review status, whether it is linked
to an account, the Stripe object to look up in the dashboard (where the amounts are), and when it
opened. Never an email address, an amount, a card detail, a secret or a database URL.

Exit codes: 0 done; 1 unexpected error (rolled back); 2 refused (arguments, missing --apply, unknown
case, or database support disabled).
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TRANSITIONS = {"acknowledge": ("open",), "close": ("open", "acknowledged")}
TARGET = {"acknowledge": "acknowledged", "close": "closed"}


def describe(case):
    return (f"{case.id}  {case.kind:7}  {case.provider_status:22}  {case.review_status:12}  "
            f"{'linked' if case.user_id else 'unlinked'}  {case.provider_object_id}  "
            f"{case.opened_at:%Y-%m-%d}")


def run(argv, session_factory):
    parser = argparse.ArgumentParser(prog="billing_cases.py", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--all", action="store_true")
    for name in TRANSITIONS:
        command = commands.add_parser(name)
        command.add_argument("case_id")
        command.add_argument("--apply", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return 0 if stop.code == 0 else 2

    from sqlalchemy import select
    from kalillac_db.models import BillingCase

    if args.command == "list":
        with session_factory() as session:
            query = select(BillingCase).order_by(BillingCase.opened_at)
            if not args.all:
                query = query.where(BillingCase.review_status != "closed")
            cases = list(session.scalars(query))
        for case in cases:
            print(describe(case))
        print(f"{len(cases)} case{'s' if len(cases) != 1 else ''}")
        return 0

    if not args.apply:
        print("refused: add --apply to change a case")
        return 2
    try:
        case_id = uuid.UUID(args.case_id)
    except ValueError:
        print("refused: not a case id")
        return 2
    with session_factory() as session, session.begin():
        case = session.get(BillingCase, case_id, with_for_update=True)
        if case is None:
            print("refused: no such case")
            return 2
        if case.review_status not in TRANSITIONS[args.command]:
            print(f"refused: the case is {case.review_status}")
            return 2
        case.review_status = TARGET[args.command]
        print(describe(case))
    return 0


def main(argv=None):
    from kalillac_db.engine import get_session_factory
    factory = get_session_factory()
    if factory is None:
        print("refused: database support is disabled (KALILLAC_DB_ENABLED)")
        return 2
    try:
        return run(sys.argv[1:] if argv is None else argv, factory)
    except Exception as error:  # A class name only: never a row, an id or a URL.
        print(f"unexpected_error {type(error).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
