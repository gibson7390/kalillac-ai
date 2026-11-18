"""The V1 account-security operator transition (V1-02E, E2), and its rollback. Operator command only.

Usage (from candidate/, with the application's settings, during a maintenance window):
    python tools/v1_security_transition.py check
    python tools/v1_security_transition.py activate --release-sha <40 hex> [--apply]
    python tools/v1_security_transition.py rollback [--apply]

activate, in ONE transaction on the operator's administrative connection:
  1. the database is at this release's head and the classifier accepts it as `prepared` (exact catalog,
     legacy runtime grants); no application login (R, A, S, P) has an open session (ingress closed);
  2. E1a: the runtime login's final account grants (kalillac_accounts.v1_final_grants);
  3. S and P get LOGIN with the passwords in their own protected credential files, sent to the server
     only as SCRAM-SHA-256 verifiers (no password ever appears in SQL or a server log), and CONNECT on
     this database if they do not already have it;
  4. the latch moves to `active`, bound to this database instance, KALILLAC_DEPLOYMENT_ID, the release
     and the authority version;
  5. inside the same transaction the classifier must now accept the catalog as `active` and the
     binding must be exactly the one written; otherwise everything rolls back.
  Without --apply the transaction is always rolled back at the end: a dry run executes and verifies
  every step and changes nothing. With --apply it commits, then logs in as S through the real
  application path; if that fails, the rollback runs automatically.

rollback is the exact inverse (legacy grants, S and P NOLOGIN without passwords, CONNECT grants removed,
latch back to `prepared`), verified the same way. A V1-off or older binary refuses to start against an
active latch, so redeploying an older application is never a rollback: this command is.

Settings: KALILLAC_V1_TRANSITION_ADMIN_URL_FILE (a protected file holding the operator's administrative
database URL: superuser, or CREATEROLE with ADMIN OPTION on S and P and SET on the owner role),
KALILLAC_V1_SERVICE_CREDENTIALS_FILE, KALILLAC_V1_OPERATOR_CREDENTIALS_FILE, KALILLAC_DEPLOYMENT_ID,
the role settings, KALILLAC_V1_OPERATOR_RECORD_FILE (append-only record; required for --apply).

Writing or testing this tool is not authorization to run it against production. Exit codes: 0 done or
dry run passed; 1 failed and rolled back; 2 refused before any change.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HEAD = "0026_account_provider_attempts"
ADMIN_URL_FILE_ENV = "KALILLAC_V1_TRANSITION_ADMIN_URL_FILE"
SERVICE_FILE_ENV = "KALILLAC_V1_SERVICE_CREDENTIALS_FILE"
OPERATOR_FILE_ENV = "KALILLAC_V1_OPERATOR_CREDENTIALS_FILE"
RECORD_FILE_ENV = "KALILLAC_V1_OPERATOR_RECORD_FILE"
_SHA = re.compile(r"[0-9a-f]{40}")


class Refused(Exception):
    pass


class Failed(Exception):
    pass


def scram_verifier(password, iterations=4096, salt=None):
    """PostgreSQL's SCRAM-SHA-256 verifier for password (RFC 5802/7677): the server stores only this."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def _roles():
    from kalillac_db.migration_roles import (migration_app_role, migration_owner_role, v1_security_executor_role,
                                             v1_security_operator_role, v1_security_service_role)
    return {"runtime": migration_app_role(), "owner": migration_owner_role(),
            "service": v1_security_service_role(), "executor": v1_security_executor_role(),
            "operator": v1_security_operator_role()}


def _append(path, entry):
    line = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _state(session, roles):
    """(alembic version, classifier state, registration role, database instance)."""
    from sqlalchemy import text
    from kalillac_accounts.v1_schema import classify
    version = session.execute(text("SELECT version_num FROM kalillac.alembic_version")).scalar_one()
    state = classify(session)
    receipt = json.loads(session.execute(text(
        "SELECT obj_description('kalillac.account_registration_activation'::regclass, 'pg_class')")).scalar_one())
    instance = session.execute(text("SELECT database_instance_id FROM kalillac.account_registration_activation"
                                    " WHERE singleton_id = 1")).scalar_one()
    return version, state, receipt["registration_role"], instance


def _ingress_closed(session, logins):
    from sqlalchemy import text
    return session.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                                " AND usename = ANY(:l) AND pid <> pg_backend_pid()"), {"l": logins}).scalar_one() == 0


def _binding(session):
    from sqlalchemy import text
    return tuple(session.execute(text("SELECT state, authority_version, deployment_id, release_sha"
                                      " FROM kalillac.account_security_activation WHERE singleton_id = 1")).one())


def _passwords(environ, read_file, roles, instance, deployment):
    from kalillac_accounts.v1_authority import V1AuthorityUnavailable, parse_service_credential
    try:
        service = parse_service_credential(read_file(environ.get(SERVICE_FILE_ENV, ""), 8192), role=roles["service"],
                                           database_instance_id=instance, deployment_id=deployment)
        operator = parse_service_credential(read_file(environ.get(OPERATOR_FILE_ENV, ""), 8192),
                                            role=roles["operator"], database_instance_id=instance,
                                            deployment_id=deployment, forbidden_passwords=(service,))
    except (V1AuthorityUnavailable, OSError, ValueError):
        raise Refused("the S or P credential file is missing, not protected, or not bound to this database and"
                      " deployment, or both hold the same password") from None
    return service, operator


def transition(connection, command, *, environ, read_file, release_sha=None, apply=False):
    """One transaction on connection (the operator's administrative login). Returns a summary; raises
    Refused (nothing changed) or Failed (rolled back)."""
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from kalillac_accounts.v1_final_grants import e1a_statements, rollback_statements, user_columns
    from kalillac_accounts.v1_schema import ACTIVE, AUTHORITY_VERSION, PREPARED, V1SchemaRefused
    roles = _roles()
    deployment = environ.get("KALILLAC_DEPLOYMENT_ID", "")
    if command == "activate" and (not release_sha or not _SHA.fullmatch(release_sha)):
        raise Refused("--release-sha must be the release's full 40-character commit")
    transaction = connection.begin()
    try:
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout = '120s'")
        connection.exec_driver_sql("SET LOCAL ROLE " + roles["owner"])
        session = Session(bind=connection)
        try:
            version, state, registration, instance = _state(session, roles)
        except V1SchemaRefused as refused:
            raise Refused(f"the classifier refuses the catalog ({refused.reason})") from None
        connection.execute(text("SELECT 1 FROM kalillac.account_security_activation WHERE singleton_id = 1"
                                " FOR UPDATE"))
        if version != HEAD:
            raise Refused(f"the database is at {version}, not this release's head {HEAD}")
        if command == "check":
            return {"version": version, "state": state}
        expected = PREPARED if command == "activate" else ACTIVE
        if state != expected:
            raise Refused(f"the latch is {state}; {command} needs {expected}")
        if not _ingress_closed(session, [roles["runtime"], registration, roles["service"], roles["operator"]]):
            raise Refused("an application login still has an open session: close ingress first")
        columns = user_columns(session)
        if command == "activate":
            service, operator = _passwords(environ, read_file, roles, instance, deployment)
            for statement in e1a_statements(roles["runtime"], columns):
                connection.exec_driver_sql(statement)
            connection.execute(text(
                "UPDATE kalillac.account_security_activation SET state = 'active', authority_version = :v,"
                " activation_id = :a, deployment_id = :d, release_sha = :r, activated_at = clock_timestamp()"
                " WHERE singleton_id = 1"), {"v": AUTHORITY_VERSION, "a": uuid.uuid4(), "d": deployment,
                                             "r": release_sha})
            connection.exec_driver_sql("RESET ROLE")
            for role, password in ((roles["service"], service), (roles["operator"], operator)):
                # The verifier is base64 and '$'/':' only: it cannot end the literal.
                connection.exec_driver_sql(f"ALTER ROLE {role} LOGIN PASSWORD '{scram_verifier(password)}'")
                if not connection.execute(text("SELECT has_database_privilege(:r, current_database(), 'CONNECT')"),
                                          {"r": role}).scalar_one():
                    connection.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{connection.engine.url.database}" TO {role}')
        else:
            for statement in rollback_statements(roles["runtime"], columns):
                connection.exec_driver_sql(statement)
            connection.execute(text(
                "UPDATE kalillac.account_security_activation SET state = 'prepared', authority_version = NULL,"
                " activation_id = NULL, deployment_id = NULL, release_sha = NULL, activated_at = NULL"
                " WHERE singleton_id = 1"))
            connection.exec_driver_sql("RESET ROLE")
            for role in (roles["service"], roles["operator"]):
                connection.exec_driver_sql(f"ALTER ROLE {role} NOLOGIN PASSWORD NULL")
                connection.exec_driver_sql(f'REVOKE CONNECT ON DATABASE "{connection.engine.url.database}" FROM {role}')
        # Verify inside the same transaction, as the owner, exactly as startup will.
        connection.exec_driver_sql("SET LOCAL ROLE " + roles["owner"])
        try:
            after = _state(session, roles)[1]
        except V1SchemaRefused as refused:
            raise Failed(f"the classifier refused the result ({refused.reason})") from None
        binding = _binding(session)
        wanted = (ACTIVE, AUTHORITY_VERSION, deployment, release_sha) if command == "activate" else (
            PREPARED, None, None, None)
        if after != wanted[0] or binding != wanted:
            raise Failed("the result did not verify")
        summary = {"command": command, "from": state, "to": after, "applied": bool(apply)}
        if apply:
            transaction.commit()
        else:
            transaction.rollback()
        return summary
    except BaseException:
        if transaction.is_active:
            transaction.rollback()
        raise


def run(argv, *, admin_engine_factory, environ, read_file, verify_service=None):
    parser = argparse.ArgumentParser(prog="v1_security_transition.py", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    activate = commands.add_parser("activate")
    activate.add_argument("--release-sha", required=True)
    activate.add_argument("--apply", action="store_true")
    commands.add_parser("rollback").add_argument("--apply", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return 0 if stop.code == 0 else 2
    apply = getattr(args, "apply", False)
    record = environ.get(RECORD_FILE_ENV, "")
    if apply and not record:
        print(f"refused: {RECORD_FILE_ENV} is required with --apply")
        return 2
    entry = {"action": "v1_transition_" + args.command, "release_sha": getattr(args, "release_sha", None),
             "deployment_id": environ.get("KALILLAC_DEPLOYMENT_ID", "")}
    try:
        engine = admin_engine_factory()
    except Exception:
        print("refused: the administrative connection is not configured or not readable")
        return 2
    try:
        if apply:
            try:
                _append(record, {**entry, "at": datetime.now(timezone.utc).isoformat(), "phase": "intent"})
            except OSError:
                print("refused: the operator record could not be written; nothing was changed")
                return 2
        try:
            with engine.connect() as connection:
                summary = transition(connection, args.command, environ=environ, read_file=read_file,
                                     release_sha=getattr(args, "release_sha", None), apply=apply)
        except Refused as refused:
            print(f"refused: {refused}")
            outcome, code = "refused", 2
        except Failed as failed:
            print(f"failed and rolled back: {failed}")
            outcome, code = "failed", 1
        else:
            outcome, code = "applied" if apply else ("checked" if args.command == "check" else "dry_run_passed"), 0
            print(json.dumps(summary, sort_keys=True))
            if apply and args.command == "activate" and verify_service is not None and not verify_service():
                print("failed: S could not log in through the application path; rolling back")
                with engine.connect() as connection:
                    transition(connection, "rollback", environ=environ, read_file=read_file, apply=True)
                outcome, code = "failed_and_rolled_back", 1
        if apply:
            _append(record, {**entry, "at": datetime.now(timezone.utc).isoformat(), "phase": "outcome",
                             "outcome": outcome})
        return code
    finally:
        engine.dispose()


def main(argv=None):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from kalillac_accounts.registration_files import read_protected

    def admin_engine():
        url = read_protected(os.environ.get(ADMIN_URL_FILE_ENV, ""), 4096).decode("utf-8").strip()
        return create_engine(make_url(url), hide_parameters=True, pool_size=1, max_overflow=0)

    def verify_service():
        from kalillac_accounts.v1_authority import load_v1_authority
        from kalillac_db.engine import get_session_factory
        try:
            load_v1_authority(get_session_factory, os.environ).dispose()
            return True
        except Exception:
            return False
    try:
        return run(sys.argv[1:] if argv is None else argv, admin_engine_factory=admin_engine, environ=os.environ,
                   read_file=read_protected, verify_service=verify_service)
    except Exception as error:  # A class name only: never a URL, a password or a row.
        print(f"unexpected_error {type(error).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
