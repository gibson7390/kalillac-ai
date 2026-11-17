"""The owner's read-only report (O1, read-only first): how the service stands, in counts.

Operator command only; there is no HTTP route. It reads one REPEATABLE READ, READ ONLY snapshot with the
application's database environment and writes nothing to the database. It reports counts and totals
only: never an email address, account id, name, Stripe id, message, file, prompt, key, database URL or
exception text. Actions stay in their own reviewed tools (v1_account_support.py, billing_cases.py,
finance_ledger_admin.py), which this report names but never runs.

    python tools/owner_report.py report [--json] [--html PATH]

Sections: accounts (total, verified, quarantined, deletion requested, new in 7 and 30 days, live
sessions), plan (accounts on the paid entitlement now), subscriptions by Stripe status (and how many
end at the period's end), billing cases by kind and review status, usage today and over the last 7 UTC
days, anonymous visitors today, and saved work (Spaces, conversations, messages). A section the
database role may not read, or that this database does not have, is reported as unavailable with a
fixed reason; the others are still reported. Account security events are not here: they belong to the
V1 authorities, the runtime login may not read them, and no runtime module names their tables
(test_v1_schema); the operator's support tool reads one account's history.

--html writes one self-contained page (no script, no external resource) to a new file, mode 0600; it
refuses an existing path. Exit codes: 0 done, 1 failed (rolled back), 2 refused.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EXIT_OK, EXIT_FAILED, EXIT_REFUSED = 0, 1, 2
INSUFFICIENT_PRIVILEGE, UNDEFINED_TABLE, UNDEFINED_COLUMN = "42501", "42P01", "42703"


@dataclass(frozen=True)
class Unavailable:
    reason: str        # "not permitted for this database role", "not in this database", "not readable"


def _sqlstate(error):
    code = getattr(getattr(error, "orig", None), "sqlstate", None)
    return code if type(code) is str else None


def _section(session, read):
    """read(session) inside a savepoint: its value, or Unavailable with a fixed reason."""
    try:
        with session.begin_nested():
            return read(session)
    except Exception as error:
        code = _sqlstate(error)
        if code == INSUFFICIENT_PRIVILEGE:
            return Unavailable("not permitted for this database role")
        # PostgreSQL's SQLSTATE, or SQLite's own wording (its driver has no SQLSTATE); never printed.
        if code in (UNDEFINED_TABLE, UNDEFINED_COLUMN) or "no such table" in str(getattr(error, "orig", "")):
            return Unavailable("not in this database")
        return Unavailable("not readable")


def _accounts(now):
    def read(session):
        from sqlalchemy import func, select
        from kalillac_db.models import AccountSession, User
        week, month = now - timedelta(days=7), now - timedelta(days=30)
        row = session.execute(select(
            func.count(),
            func.count().filter(User.email_verified_at.is_not(None)),
            func.count().filter(User.quarantined_at.is_not(None)),
            func.count().filter(User.deletion_requested_at.is_not(None)),
            func.count().filter(User.created_at > week),
            func.count().filter(User.created_at > month),
        ).select_from(User)).one()
        sessions = session.scalar(select(func.count()).select_from(AccountSession).where(
            AccountSession.revoked_at.is_(None), AccountSession.expires_at > now))
        keys = ("total", "verified", "quarantined", "deletion_requested", "new_7_days", "new_30_days")
        return {**dict(zip(keys, (int(v) for v in row))), "live_sessions": int(sessions)}
    return read


def _plan(now):
    def read(session):
        from sqlalchemy import func, or_, select
        from kalillac_db.models import AccountEntitlement
        from kalillac_db.models.account_entitlement import TIER_PAID
        # The same rule as kalillac_accounts.entitlements.effective_entitlement: an unexpired paid row.
        paid = session.scalar(select(func.count()).select_from(AccountEntitlement).where(
            AccountEntitlement.tier == TIER_PAID,
            or_(AccountEntitlement.expires_at.is_(None), AccountEntitlement.expires_at > now)))
        return {"paid_now": int(paid)}
    return read


def _subscriptions(session):
    from sqlalchemy import func, select
    from kalillac_db.models import AccountBilling
    rows = session.execute(select(AccountBilling.subscription_status, func.count()).where(
        AccountBilling.subscription_status.is_not(None)).group_by(AccountBilling.subscription_status)).all()
    ending = session.scalar(select(func.count()).select_from(AccountBilling).where(
        AccountBilling.cancel_at_period_end.is_(True), AccountBilling.subscription_status.in_(("active", "trialing"))))
    return {"by_status": {str(status): int(count) for status, count in sorted(rows)},
            "ending_at_period_end": int(ending)}


def _billing_cases(session):
    from sqlalchemy import func, select
    from kalillac_db.models.billing_case import BillingCase
    rows = session.execute(select(BillingCase.kind, BillingCase.review_status, func.count())
                           .group_by(BillingCase.kind, BillingCase.review_status)).all()
    return {f"{kind} {status}": int(count) for kind, status, count in sorted(rows)}


def _usage(today):
    def read(session):
        from sqlalchemy import func, select
        from kalillac_db.models import AccountUsageDaily
        first = today - timedelta(days=6)
        rows = session.execute(select(AccountUsageDaily.usage_date, func.count(),
                                      func.coalesce(func.sum(AccountUsageDaily.successful_chats), 0))
                               .where(AccountUsageDaily.usage_date >= first, AccountUsageDaily.usage_date <= today)
                               .group_by(AccountUsageDaily.usage_date)).all()
        by_day = {date.fromisoformat(str(day)[:10]): (int(accounts), int(chats)) for day, accounts, chats in rows}
        days = [{"date": (first + timedelta(days=n)).isoformat(),
                 "accounts": by_day.get(first + timedelta(days=n), (0, 0))[0],
                 "chats": by_day.get(first + timedelta(days=n), (0, 0))[1]} for n in range(7)]
        return {"today": days[-1], "last_7_days": days}
    return read


def _anonymous(today):
    def read(session):
        from sqlalchemy import func, select
        from kalillac_db.models.anonymous_usage import AnonymousUsageDay
        return {"visitors_today": int(session.scalar(select(func.count()).select_from(AnonymousUsageDay)
                                                     .where(AnonymousUsageDay.usage_date == today)))}
    return read


def _saved_work(session):
    from sqlalchemy import func, select
    from kalillac_db.models import Conversation, Message, Space
    return {name: int(session.scalar(select(func.count()).select_from(model)))
            for name, model in (("spaces", Space), ("conversations", Conversation), ("messages", Message))}


SECTIONS = ("accounts", "plan", "subscriptions", "billing_cases", "usage", "anonymous", "saved_work")


def collect(session, now):
    """Every section from the session's current snapshot."""
    today = now.date()
    readers = {"accounts": _accounts(now), "plan": _plan(now), "subscriptions": _subscriptions,
               "billing_cases": _billing_cases, "usage": _usage(today), "anonymous": _anonymous(today),
               "saved_work": _saved_work}
    return {name: _section(session, readers[name]) for name in SECTIONS}


def as_json(report, now):
    return {"generated_at": now.isoformat(timespec="seconds"),
            "sections": {name: ({"unavailable": value.reason} if isinstance(value, Unavailable) else value)
                         for name, value in report.items()}}


TITLES = {"accounts": "Accounts", "plan": "Plan", "subscriptions": "Subscriptions", "billing_cases": "Billing cases",
          "usage": "Usage (signed-in accounts, UTC days)", "anonymous": "Anonymous use", "saved_work": "Saved work"}
LABELS = {"total": "Accounts", "verified": "Verified", "quarantined": "Quarantined",
          "deletion_requested": "Deletion requested", "new_7_days": "New in 7 days", "new_30_days": "New in 30 days",
          "live_sessions": "Live sessions", "paid_now": "On the paid plan now",
          "ending_at_period_end": "Ending at period end", "visitors_today": "Visitors today",
          "spaces": "Spaces", "conversations": "Conversations", "messages": "Messages"}


def _rows(name, value):
    """(label, value) rows of one available section, in a fixed order."""
    if name == "subscriptions":
        return [*[(f"Status: {status}", count) for status, count in value["by_status"].items()],
                (LABELS["ending_at_period_end"], value["ending_at_period_end"])]
    if name == "usage":
        return [(f"{day['date']}", f"{day['chats']} chats, {day['accounts']} accounts") for day in value["last_7_days"]]
    return [(LABELS.get(key, key), count) for key, count in value.items()]


def as_text(report, now):
    lines = [f"Kalillac owner report, {now.isoformat(timespec='seconds')} (counts only)"]
    for name, value in report.items():
        lines.append("")
        lines.append(TITLES[name])
        if isinstance(value, Unavailable):
            lines.append(f"  unavailable: {value.reason}")
            continue
        rows = _rows(name, value) or [("none", "")]
        lines.extend(f"  {label}: {count}" if count != "" else f"  {label}" for label, count in rows)
    return "\n".join(lines) + "\n"


def as_html(report, now):
    cards = []
    for name, value in report.items():
        if isinstance(value, Unavailable):
            body = f'<p class="muted">Unavailable: {html.escape(value.reason)}</p>'
        else:
            rows = _rows(name, value)
            body = ("<table>" + "".join(f"<tr><th scope=\"row\">{html.escape(str(label))}</th>"
                                        f"<td>{html.escape(str(count))}</td></tr>" for label, count in rows)
                    + "</table>") if rows else '<p class="muted">None</p>'
        cards.append(f"<section><h2>{html.escape(TITLES[name])}</h2>{body}</section>")
    stamp = html.escape(now.isoformat(timespec="seconds"))
    return ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            "<title>Kalillac owner report</title><style>"
            ":root{color-scheme:light dark;--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e2e2dc}"
            "@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ececea;--muted:#9a9a94;"
            "--line:#33332f}}"
            "body{margin:0;padding:24px 16px;background:var(--bg);color:var(--ink);"
            "font:15px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif}"
            "main{max-width:1040px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}"
            ".muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));"
            "gap:14px;margin-top:20px}section{background:var(--card);border:1px solid var(--line);border-radius:10px;"
            "padding:14px 16px}h2{font-size:15px;margin:0 0 8px}table{width:100%;border-collapse:collapse}"
            "th{font-weight:400;text-align:left;color:var(--muted);padding:3px 0}"
            "td{text-align:right;font-variant-numeric:tabular-nums;padding:3px 0}"
            "</style></head><body><main><h1>Kalillac owner report</h1>"
            f"<p class=\"muted\">Read-only snapshot, {stamp} UTC. Counts only. Actions: the support, billing-case and "
            "ledger tools.</p><div class=\"grid\">" + "".join(cards) + "</div></main></body></html>\n")


def _write_new(path, content):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def run(argv, factory=None, clock=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="owner_report.py")
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report")
    report.add_argument("--json", action="store_true")
    report.add_argument("--html", metavar="PATH")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return EXIT_REFUSED
    if args.html and os.path.lexists(args.html):
        print("refused: the --html path already exists", file=sys.stderr)
        return EXIT_REFUSED
    if factory is None:
        from kalillac_db.engine import get_session_factory
        factory = get_session_factory()
    if factory is None:
        print("refused: the database is not enabled or not configured", file=sys.stderr)
        return EXIT_REFUSED
    try:
        with factory() as session:
            try:
                session.begin()
                if session.get_bind().dialect.name == "postgresql":
                    from sqlalchemy import text
                    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                now = clock() if clock else datetime.now(timezone.utc)
                collected = collect(session, now)
                if session.new or session.dirty or session.deleted:
                    raise RuntimeError()
            finally:
                session.rollback()                   # Nothing to keep: the snapshot ends here.
    except Exception as error:
        print(f"failed: {type(error).__name__}", file=sys.stderr)
        return EXIT_FAILED
    if args.html:
        _write_new(args.html, as_html(collected, now))
    out.write(json.dumps(as_json(collected, now), indent=2, sort_keys=True) + "\n" if args.json
              else as_text(collected, now))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
