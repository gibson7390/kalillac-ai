"""Audit and reconcile the Financial Safeguards ledger.

Operator command only; there is no HTTP route for any of this. It reads and
writes financial and control metadata only (micro-US-dollar amounts, UTC
dates, provider and scope names): never an account, visitor, prompt, response,
key, database URL or exception text.

Commands (run from the candidate/ directory):

    python tools/finance_ledger_admin.py report [--json]
        Read-only. Exposure against each scope and provider ceiling for the
        current UTC day and month, outstanding reservations, unsafe latches,
        and a full recomputation of every bucket from the reservation rows.
        Runs with the application's database environment.

    python tools/finance_ledger_admin.py resolve-stale --older-than-hours N [--apply]
        Finalise outstanding reservations older than N hours (N >= 1) at
        their FULL reserved amount: the outcome of a reservation left behind
        by a process death is unknown, so it is charged, never refunded.
        Exposure is unchanged; the rows stop being outstanding. Without
        --apply it only reports what it would do. Each reservation is its own
        transaction and is recorded in finance_operator_actions. Runs with the
        application's database environment.

    python tools/finance_ledger_admin.py clear-latch --scope SCOPE --expect-reason REASON --apply
        Remove one unsafe latch, only if its reason is the one named, and
        record the action. Deleting a latch needs the owner role: run it with
        the MIGRATION login's database environment (the one Alembic uses);
        the command assumes the owner role exactly as a migration does. Clear
        a latch only after its cause is understood and corrected: the latch is
        what stops spending when a reservation's assumption failed.

Exit codes:
  0 done (report: the ledger is consistent)
  1 unexpected error, rolled back
  2 refused: arguments, or the database is not enabled or not configured
  3 report: at least one bucket differs from its reservations
  4 the database role lacks the privilege for this command
  5 clear-latch: no latch on that scope with that reason (nothing changed)
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_INCONSISTENT = 3
EXIT_PRIVILEGE = 4
EXIT_NO_LATCH = 5
INSUFFICIENT_PRIVILEGE = "42501"


class Refused(Exception):
    pass


def _usd(microusd):
    return f"${microusd // 1_000_000}.{microusd % 1_000_000:06d}"


def _now(session):
    from sqlalchemy import func, select
    if session.get_bind().dialect.name == "postgresql":
        return session.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _bounded(session, assume_owner=False):
    """Fixed operator limits and, for latch clearing, the owner role."""
    from sqlalchemy import text
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    session.execute(text("SET LOCAL statement_timeout = '30s'"))
    if assume_owner:
        from kalillac_db.migration_roles import migration_owner_role
        session.execute(text(f"SET LOCAL ROLE {migration_owner_role()}"))


def _limits():
    """The configured ceilings, or None when Financial Safeguards are off or
    their configuration does not load (the report then omits ceilings)."""
    try:
        from kalillac_finance.config import load_financial_config
        config = load_financial_config()
    except Exception:
        return None, "invalid"
    return (None, "off") if config is None else (config.limits, "on")


def report(factory, as_json, out):
    from kalillac_db.repositories import finance_admin
    limits, state = _limits()
    with factory() as session, session.begin():
        _bounded(session)
        data = finance_admin.summary(session, _now(session), limits)
    data["configuration"] = state
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True), file=out)
    else:
        print(f"financial safeguards configuration: {state}", file=out)
        for table in ("scopes", "providers"):
            for kind in ("day", "month"):
                for name, row in data[table][kind].items():
                    used = row["reserved"] + row["settled"]
                    ceiling = f" of {_usd(row['limit'])}" if "limit" in row else ""
                    print(f"{kind} {data['periods'][kind]} {name}: {_usd(used)}{ceiling} "
                          f"(reserved {_usd(row['reserved'])}, settled {_usd(row['settled'])}, "
                          f"overrun {_usd(row['overrun'])})", file=out)
        outstanding = data["outstanding"]
        print(f"outstanding reservations: {outstanding['count']} holding {_usd(outstanding['microusd'])}"
              + (f", oldest {outstanding['oldest']}" if outstanding["oldest"] else ""), file=out)
        print("unsafe latches: " + (", ".join(f"{scope}={reason}" for scope, reason in sorted(data["latches"].items()))
                                    or "none"), file=out)
        for table, kind, start, name, bucket, reservations in data["violations"]:
            print(f"INCONSISTENT {table} {kind} {start} {name}: bucket {tuple(bucket)} "
                  f"reservations {tuple(reservations)}", file=out)
    if data["violations"]:
        print("finance_ledger_inconsistent", file=out)
        return EXIT_INCONSISTENT
    print("finance_ledger_consistent", file=out)
    return EXIT_OK


def resolve_stale(factory, hours, apply, out):
    from kalillac_db.repositories import finance_admin
    seconds = hours * 3600
    with factory() as session, session.begin():
        _bounded(session)
        ids = finance_admin.stale_ids(session, _now(session), seconds)
    if not apply:
        print(f"stale_reservations_found {len(ids)} (nothing changed; pass --apply to finalise them)", file=out)
        return EXIT_OK
    resolved = total = 0
    while ids:
        for reservation_id in ids:
            # One reservation per transaction: reservation row, then its buckets.
            with factory() as session, session.begin():
                _bounded(session)
                amount = finance_admin.resolve_stale(session, reservation_id, _now(session), seconds)
            if amount is not None:
                resolved, total = resolved + 1, total + amount
        with factory() as session, session.begin():
            _bounded(session)
            remaining = finance_admin.stale_ids(session, _now(session), seconds)
        if set(remaining) & set(ids):
            break  # A row that would not resolve must not loop forever.
        ids = remaining
    print(f"stale_reservations_resolved {resolved} charged {_usd(total)}", file=out)
    return EXIT_OK


def clear_latch(factory, scope, reason, out):
    from kalillac_db.repositories import finance_admin
    with factory() as session, session.begin():
        _bounded(session, assume_owner=True)
        cleared = finance_admin.clear_latch(session, scope, reason, _now(session))
    if not cleared:
        print("latch_not_found", file=out)
        return EXIT_NO_LATCH
    print(f"latch_cleared {scope} {reason}", file=out)
    return EXIT_OK


def _parser():
    parser = argparse.ArgumentParser(prog="finance_ledger_admin.py", add_help=True, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    reporting = commands.add_parser("report", allow_abbrev=False)
    reporting.add_argument("--json", action="store_true")
    stale = commands.add_parser("resolve-stale", allow_abbrev=False)
    stale.add_argument("--older-than-hours", type=int, required=True)
    stale.add_argument("--apply", action="store_true")
    latch = commands.add_parser("clear-latch", allow_abbrev=False)
    latch.add_argument("--scope", required=True, choices=("global", "free", "paid"))
    latch.add_argument("--expect-reason", required=True, choices=("overrun", "bound_exceeded", "headroom_exhausted"))
    latch.add_argument("--apply", action="store_true")
    return parser


def run(argv, factory=None, out=None):
    out = out or sys.stdout
    try:
        args = _parser().parse_args(argv)
    except SystemExit as stop:
        return EXIT_OK if stop.code == 0 else EXIT_REFUSED
    try:
        if args.command == "resolve-stale" and not 1 <= args.older_than_hours <= 24 * 366:
            raise Refused("older_than_hours_out_of_range")
        if args.command == "clear-latch" and not args.apply:
            raise Refused("clear_latch_requires_apply")
        if factory is None:
            from kalillac_db.engine import get_session_factory
            factory = get_session_factory()
            if factory is None:
                raise Refused("database_not_enabled")
        if args.command == "report":
            return report(factory, args.json, out)
        if args.command == "resolve-stale":
            return resolve_stale(factory, args.older_than_hours, args.apply, out)
        return clear_latch(factory, args.scope, args.expect_reason, out)
    except Refused as refused:
        print("refused " + str(refused), file=out)
        return EXIT_REFUSED
    except Exception as error:
        # Never the exception text: it can carry connection details.
        original = getattr(error, "orig", None)
        if getattr(original, "sqlstate", None) == INSUFFICIENT_PRIVILEGE:
            print("insufficient_privilege", file=out)
            return EXIT_PRIVILEGE
        print("unexpected_error " + type(error).__name__, file=out)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
