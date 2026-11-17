"""Account support operations through the operator role P (V1-02D, D4). Operator command only; there is
no HTTP route for any of this, and the application never connects as P.

Usage (from candidate/, with the application's database environment plus the operator settings below):
    python tools/v1_account_support.py show <account-uuid>
    python tools/v1_account_support.py suspend <account-uuid> --reason <code> [--operation-id <uuid>] [--apply]
    python tools/v1_account_support.py reinstate <account-uuid> --reason <code> [--operation-id <uuid>] [--apply]
    python tools/v1_account_support.py request-deletion <account-uuid> --reason <code> [--operation-id <uuid>] [--apply]
    python tools/v1_account_support.py export <account-uuid>

* suspend, reinstate and request-deletion call v1_quarantine, v1_reinstate and v1_request_deletion as P.
  Without --apply they are a dry run: P's connection and boundary are verified and the account's state
  shown, and nothing is called. Every call has an operation id (generated, or given to retry the same
  operation, which the database reports as replayed and never applies twice) and a required reason code.
* Before the call, the intent (time, operation id, action, account id, reason) is appended to the
  operator record file; after it, the outcome. If the intent cannot be recorded nothing is called. The
  audit table records the event itself; the record file is where the reason lives.
* show and export read through the runtime login, as the application does, and only columns that are
  not credentials: never a password hash, a session or token digest, a password or a database URL.
  export is a bounded JSON document of what is held about the account (for a data-subject request);
  it contains the account's email and is written to standard output only.

Settings: KALILLAC_V1_SECURITY_OPERATOR_ROLE, KALILLAC_V1_SECURITY_EXECUTOR_ROLE, KALILLAC_DEPLOYMENT_ID,
KALILLAC_V1_OPERATOR_CREDENTIALS_FILE (P's protected credential file, in the S file's format and bound to
this database instance and deployment; never the application's), KALILLAC_V1_OPERATOR_RECORD_FILE (the
append-only record; required for --apply). Writing this tool is not permission to run it against
production: P has no login until the operator transition (E2) gives it one.

Exit codes: 0 done (including a refusal the database itself reported, e.g. already_quarantined);
1 unexpected error; 2 refused before any change (arguments, missing --apply, settings, P's boundary,
the record file, or database support disabled).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CREDENTIAL_FILE_ENV = "KALILLAC_V1_OPERATOR_CREDENTIALS_FILE"
RECORD_FILE_ENV = "KALILLAC_V1_OPERATOR_RECORD_FILE"
# Fixed reason codes; free text is never recorded.
REASONS = ("customer_request", "support_review", "abuse_report", "fraud_review", "security_incident",
           "legal_request", "payment_dispute", "review_cleared")
ACTIONS = {"suspend": "quarantine", "reinstate": "reinstate", "request-deletion": "request_deletion"}
EXPORT_LIMIT = 500          # Rows per collection in an export; the count beyond is reported, not listed.


class Refused(Exception):
    pass


def _iso(value):
    return None if value is None else value.isoformat()


def account_state(session, account_id):
    """The account's state through the runtime login, from non-credential columns only; None if absent."""
    from sqlalchemy import func, select
    from kalillac_db.models import AccountSession, User
    row = session.execute(select(User.id, User.created_at, User.is_active, User.email_verified_at,
                                 User.quarantined_at, User.deletion_requested_at, User.sessions_valid_after)
                          .where(User.id == account_id)).one_or_none()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    if session.get_bind().dialect.name == "sqlite":          # Tests only: SQLite stores naive UTC.
        now = now.replace(tzinfo=None)
    live = session.scalar(select(func.count()).select_from(AccountSession).where(
        AccountSession.user_id == account_id, AccountSession.revoked_at.is_(None), AccountSession.expires_at > now,
        *([AccountSession.created_at > row.sessions_valid_after] if row.sessions_valid_after else [])))
    return {"account_id": str(row.id), "created_at": _iso(row.created_at), "active": row.is_active,
            "email_verified": row.email_verified_at is not None, "quarantined_at": _iso(row.quarantined_at),
            "deletion_requested_at": _iso(row.deletion_requested_at), "live_sessions": live}


def export_account(session, account_id):
    """What is held about the account, bounded per collection; None if absent."""
    from sqlalchemy import func, select, text
    from kalillac_db.models import (AccountBilling, AccountEntitlement, AccountSession, AccountUsageDaily,
                                    BillingCase, Conversation, Message, Space, User)
    user = session.execute(select(User.id, User.email, User.created_at, User.updated_at, User.is_active,
                                  User.email_verified_at, User.quarantined_at, User.deletion_requested_at)
                           .where(User.id == account_id)).one_or_none()
    if user is None:
        return None

    def bounded(name, query, shape):
        rows = session.execute(query.limit(EXPORT_LIMIT + 1)).all()
        listed = [shape(r) for r in rows[:EXPORT_LIMIT]]
        return {name: listed, name + "_truncated": len(rows) > EXPORT_LIMIT}

    document = {"format": "kalillac-account-export-v1", "exported_at": _iso(datetime.now(timezone.utc)),
                "account": {"id": str(user.id), "email": user.email, "created_at": _iso(user.created_at),
                            "updated_at": _iso(user.updated_at), "active": user.is_active,
                            "email_verified_at": _iso(user.email_verified_at),
                            "quarantined_at": _iso(user.quarantined_at),
                            "deletion_requested_at": _iso(user.deletion_requested_at)}}
    document.update(bounded("sessions", select(AccountSession.created_at, AccountSession.expires_at,
                                               AccountSession.revoked_at)
                            .where(AccountSession.user_id == account_id).order_by(AccountSession.created_at),
                            lambda r: {"created_at": _iso(r.created_at), "expires_at": _iso(r.expires_at),
                                       "revoked_at": _iso(r.revoked_at)}))
    entitlement = session.execute(select(AccountEntitlement.tier, AccountEntitlement.source,
                                         AccountEntitlement.expires_at)
                                  .where(AccountEntitlement.user_id == account_id)).one_or_none()
    document["entitlement"] = None if entitlement is None else {
        "tier": entitlement.tier, "source": entitlement.source, "expires_at": _iso(entitlement.expires_at)}
    billing = session.execute(select(AccountBilling.stripe_customer_id, AccountBilling.stripe_subscription_id,
                                     AccountBilling.subscription_status, AccountBilling.cancel_at_period_end,
                                     AccountBilling.current_period_end)
                              .where(AccountBilling.user_id == account_id)).one_or_none()
    document["billing"] = None if billing is None else {
        "stripe_customer_id": billing.stripe_customer_id, "stripe_subscription_id": billing.stripe_subscription_id,
        "subscription_status": billing.subscription_status, "cancel_at_period_end": billing.cancel_at_period_end,
        "current_period_end": _iso(billing.current_period_end)}
    document.update(bounded("billing_cases", select(BillingCase.kind, BillingCase.provider_object_id,
                                                    BillingCase.provider_status, BillingCase.review_status,
                                                    BillingCase.opened_at)
                            .where(BillingCase.user_id == account_id).order_by(BillingCase.opened_at),
                            lambda r: {"kind": r.kind, "provider_object_id": r.provider_object_id,
                                       "provider_status": r.provider_status, "review_status": r.review_status,
                                       "opened_at": _iso(r.opened_at)}))
    document.update(bounded("daily_usage", select(AccountUsageDaily.usage_date, AccountUsageDaily.successful_chats)
                            .where(AccountUsageDaily.user_id == account_id).order_by(AccountUsageDaily.usage_date),
                            lambda r: {"date": r.usage_date.isoformat(), "responses": r.successful_chats}))
    document.update(bounded("spaces", select(Space.id, Space.name, Space.created_at)
                            .where(Space.user_id == account_id).order_by(Space.created_at),
                            lambda r: {"id": str(r.id), "name": r.name, "created_at": _iso(r.created_at)}))
    document.update(bounded("conversations", select(Conversation.id, Conversation.space_id, Conversation.title,
                                                    Conversation.message_count, Conversation.created_at)
                            .where(Conversation.user_id == account_id).order_by(Conversation.created_at),
                            lambda r: {"id": str(r.id), "space_id": str(r.space_id), "title": r.title,
                                       "messages": r.message_count, "created_at": _iso(r.created_at)}))
    document["messages_held"] = session.scalar(select(func.count()).select_from(Message)
                                               .where(Message.user_id == account_id))
    events = session.execute(text("SELECT event_type, occurred_at FROM kalillac.account_security_events"
                                  " WHERE user_id = :u ORDER BY occurred_at LIMIT :n"),
                             {"u": account_id, "n": EXPORT_LIMIT + 1}).all() \
        if _readable(session, "kalillac.account_security_events") else None
    document["security_events"] = None if events is None else [
        {"type": e.event_type, "at": _iso(e.occurred_at)} for e in events[:EXPORT_LIMIT]]
    document["security_events_truncated"] = bool(events) and len(events) > EXPORT_LIMIT
    if events is None:
        # The audit table is owner-only by design (V1-02A): the runtime login cannot read it, so this
        # export does not hold it. The database owner can export it separately.
        document["security_events_note"] = "not readable through the runtime login; held by the owner"
    return document


def _readable(session, relation):
    from sqlalchemy import text
    if session.get_bind().dialect.name != "postgresql":
        return False
    return bool(session.scalar(text("SELECT to_regclass(:r) IS NOT NULL AND has_table_privilege(:r, 'SELECT')"),
                               {"r": relation}))


def append_record(path, entry):
    """Append one JSON line; the file is created owner-only and opened append-only."""
    line = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def run(argv, *, reader_factory, operator_factory, environ):
    """reader_factory: the runtime login's sessionmaker; operator_factory(): a verified V1Operator."""
    parser = argparse.ArgumentParser(prog="v1_account_support.py", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("show", "export"):
        commands.add_parser(name).add_argument("account_id")
    for name in ACTIONS:
        command = commands.add_parser(name)
        command.add_argument("account_id")
        command.add_argument("--reason", required=True, choices=REASONS)
        command.add_argument("--operation-id")
        command.add_argument("--apply", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return 0 if stop.code == 0 else 2
    try:
        account_id = uuid.UUID(args.account_id)
    except ValueError:
        print("refused: not an account id")
        return 2

    if args.command in ("show", "export"):
        from sqlalchemy import text
        with reader_factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET TRANSACTION READ ONLY"))
            found = (account_state if args.command == "show" else export_account)(session, account_id)
        if found is None:
            print("refused: no such account")
            return 2
        print(json.dumps(found, indent=2 if args.command == "export" else None, sort_keys=True))
        return 0

    try:
        operation_id = uuid.UUID(args.operation_id) if args.operation_id else uuid.uuid4()
    except ValueError:
        print("refused: not an operation id")
        return 2
    action = ACTIONS[args.command]
    record_path = environ.get(RECORD_FILE_ENV, "")
    if args.apply and not record_path:
        print(f"refused: {RECORD_FILE_ENV} is required with --apply")
        return 2
    from kalillac_accounts.v1_authority import V1AuthorityUnavailable
    try:
        operator = operator_factory()
    except V1AuthorityUnavailable:
        print("refused: the operator connection or P's boundary could not be verified")
        return 2
    try:
        with reader_factory() as session, session.begin():
            state = account_state(session, account_id)
        print(json.dumps({"before": state}, sort_keys=True))
        if not args.apply:
            print(f"dry run: would call {action} for this account with operation {operation_id}"
                  f" (reason {args.reason}); add --apply")
            return 0
        entry = {"at": _iso(datetime.now(timezone.utc)), "operation_id": str(operation_id), "action": action,
                 "account_id": str(account_id), "reason": args.reason}
        try:
            append_record(record_path, {**entry, "phase": "intent"})
        except OSError:
            print("refused: the operator record could not be written; nothing was called")
            return 2
        try:
            result = operator.operate(action, operation_id=operation_id, account_id=account_id)
            outcome, replayed = result.outcome, result.replayed
        except V1AuthorityUnavailable:
            outcome, replayed = "unavailable", False
        append_record(record_path, {**entry, "at": _iso(datetime.now(timezone.utc)), "phase": "outcome",
                                    "outcome": outcome, "replayed": replayed})
        print(json.dumps({"operation_id": str(operation_id), "outcome": outcome, "replayed": replayed},
                         sort_keys=True))
        if outcome == "unavailable":
            return 1
        with reader_factory() as session, session.begin():
            print(json.dumps({"after": account_state(session, account_id)}, sort_keys=True))
        return 0
    finally:
        dispose = getattr(operator, "dispose", None)
        if dispose is not None:
            dispose()


def operator_from_settings(reader_factory, environ, *, read_file=None, production=True):
    """build_v1_authority(..., authority_class=V1Operator) from the operator settings: P's own protected
    credential file, bound to the database instance registration activation recorded and to this
    deployment, and differing from the runtime login's password."""
    from sqlalchemy import text
    from kalillac_accounts.v1_authority import V1AuthorityUnavailable, V1Operator, build_v1_authority
    from kalillac_db.migration_roles import v1_security_executor_role, v1_security_operator_role
    try:
        if read_file is None:
            from kalillac_accounts.registration_files import read_protected as read_file
        with reader_factory() as session, session.begin():
            ordinary_url = session.get_bind().url
            instance = session.execute(text("SELECT database_instance_id FROM kalillac.account_registration_activation"
                                            " WHERE singleton_id = 1")).scalar_one()
        if instance is None:
            raise V1AuthorityUnavailable()
        credential = read_file(environ.get(CREDENTIAL_FILE_ENV, ""), 8192)
        return build_v1_authority(ordinary_url, role=v1_security_operator_role(), executor=v1_security_executor_role(),
                                  credential=credential, database_instance_id=instance,
                                  deployment_id=environ.get("KALILLAC_DEPLOYMENT_ID", ""), production=production,
                                  authority_class=V1Operator)
    except Exception:
        raise V1AuthorityUnavailable() from None


def main(argv=None):
    from kalillac_db.engine import get_session_factory
    factory = get_session_factory()
    if factory is None:
        print("refused: database support is disabled (KALILLAC_DB_ENABLED)")
        return 2
    try:
        return run(sys.argv[1:] if argv is None else argv, reader_factory=factory,
                   operator_factory=lambda: operator_from_settings(factory, os.environ), environ=os.environ)
    except Exception as error:  # A class name only: never a row, an id or a URL.
        print(f"unexpected_error {type(error).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
