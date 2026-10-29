"""Disposable PostgreSQL email-verification verifier; fixed database/role, no real email or Stripe.

Only kalillac_staging as kalillac_staging_app on literal loopback is accepted.
Migrations are applied by CI's separate migration role, never by this tool,
which performs no DDL. It deletes only its own per-run tagged users (their
tokens and sessions cascade). All output is fixed codes, check names and
counts: no SQL, parameters, emails, tokens, links or exceptions.

Usage (from candidate/, against the disposable CI service container):
    python tools/verify_email_verification_postgres.py \\
        --expect-db kalillac_staging --expect-user kalillac_staging_app
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import URL, create_engine, delete, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from kalillac_accounts import email_verification as service_module
from kalillac_accounts.email_gateway import EmailDeliveryError
from kalillac_accounts.email_verification import (
    VerificationService, hash_verification_token, new_verification_token,
)
from kalillac_accounts.passwords import hash_password
from kalillac_accounts.tokens import hash_session_token
from kalillac_accounts.verification_config import VerificationConfig
from kalillac_db.models import (
    AccountBilling, AccountEmailVerificationToken, AccountEntitlement, AccountRecoveryToken,
    AccountSession, AccountUsageDaily, User,
)
from kalillac_db.repositories import email_verification as repository
from kalillac_db.repositories.accounts import create_account_session
from kalillac_db.repositories.recovery import lock_user

REQUIRED_DATABASE = "kalillac_staging"
REQUIRED_ROLE = "kalillac_staging_app"
HOST = "127.0.0.1"
APPLICATION_NAME = "kalillac-verification-verify"
LOCK_TIMEOUT, STATEMENT_TIMEOUT = "10s", "45s"
WAIT_SECONDS = 15
NOW = datetime(2030, 6, 15, 12, tzinfo=timezone.utc)
CONFIG = VerificationConfig("https://verification.invalid/verify", "sender@example.invalid")
PASSWORD = "local-verifier-password"


class VerificationRefused(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise VerificationRefused()


def validate_destination(args, environ):
    if sys.flags.optimize or not __debug__:
        raise VerificationRefused()
    if args.expect_db != REQUIRED_DATABASE or args.expect_user != REQUIRED_ROLE:
        raise VerificationRefused()
    if environ.get("KALILLAC_DATABASE_URL"):
        raise VerificationRefused()
    if environ.get("KALILLAC_DB_NAME") != REQUIRED_DATABASE or environ.get("KALILLAC_DB_USER") != REQUIRED_ROLE:
        raise VerificationRefused()
    if environ.get("KALILLAC_DB_HOST", HOST) != HOST:
        raise VerificationRefused()
    port = environ.get("KALILLAC_DB_PORT", "5432")
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise VerificationRefused()
    # Workflow-owned throwaway values are not libpq settings. Refuse all other PG*
    # environment overrides (service files, hostaddr, options, TLS, etc.).
    workflow_values = {"PG_ADMIN_PASSWORD", "PG_MIGRATE_PASSWORD", "PG_APP_PASSWORD"}
    if any(name.upper().startswith("PG") and name not in workflow_values for name in environ):
        raise VerificationRefused()
    if not environ.get("KALILLAC_DB_PASSWORD"):
        raise VerificationRefused()


def build_engine(environ):
    return create_engine(
        URL.create(
            "postgresql+psycopg", username=REQUIRED_ROLE,
            password=environ["KALILLAC_DB_PASSWORD"], host=HOST,
            port=int(environ.get("KALILLAC_DB_PORT", "5432")), database=REQUIRED_DATABASE,
        ),
        pool_size=8, max_overflow=0, pool_timeout=5, hide_parameters=True, echo=False,
        connect_args={
            "hostaddr": HOST, "connect_timeout": 3, "sslmode": "disable", "gssencmode": "disable",
            "passfile": os.devnull, "application_name": APPLICATION_NAME,
            "options": f"-c lock_timeout={LOCK_TIMEOUT} -c statement_timeout={STATEMENT_TIMEOUT}",
        },
    )


def guard(engine):
    with engine.connect() as connection:
        db, current, session_user, address, port = connection.execute(text(
            "SELECT current_database(), current_user, session_user, "
            "inet_server_addr()::text, inet_server_port()"
        )).one()
        exists = connection.execute(text(
            "SELECT to_regclass('kalillac.account_email_verification_tokens') IS NOT NULL"
        )).scalar()
        limits = tuple(connection.execute(text(
            "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
        )).one())
        info = connection.connection.driver_connection.info
        peer = (info.hostaddr, info.port)
    if (db, current, session_user) != (REQUIRED_DATABASE, REQUIRED_ROLE, REQUIRED_ROLE):
        raise VerificationRefused()
    if peer != (HOST, engine.url.port) or not address or not port or not exists:
        raise VerificationRefused()
    if limits != (LOCK_TIMEOUT, STATEMENT_TIMEOUT):
        raise VerificationRefused()


@dataclass
class Gateway:
    sent: list = field(default_factory=list, repr=False)
    fail: bool = False

    def send_email_verification(self, to, link):
        self.sent.append((to, link))
        if self.fail:
            raise EmailDeliveryError()


def email_digest(email):
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


def token_of(link):
    return link.split("#token=", 1)[1]


@dataclass
class Context:
    engine: object
    factory: object
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ids: list = field(default_factory=list)
    workers: list = field(default_factory=list)

    def user(self, *, active=True, paid=False, verified=False):
        user_id = uuid.uuid4()
        email = f"verification-verify+{self.run_id}-{user_id.hex}@example.invalid"
        with self.factory() as session, session.begin():
            session.add(User(id=user_id, email=email, password_hash=hash_password(PASSWORD),
                             is_active=active, email_verified_at=NOW if verified else None))
            session.flush()
            session.add(AccountEntitlement(user_id=user_id, tier="paid" if paid else "free",
                                           source="billing" if paid else "registration"))
            if paid:
                session.add(AccountBilling(user_id=user_id, stripe_customer_id="cus_" + user_id.hex,
                                           stripe_subscription_id="sub_" + user_id.hex,
                                           stripe_price_id="price_verification_local",
                                           subscription_status="active"))
                session.add(AccountUsageDaily(user_id=user_id, usage_date=NOW.date(), successful_chats=4))
                session.add(AccountRecoveryToken(user_id=user_id, purpose="password_reset",
                                                 token_hash=uuid.uuid4().hex * 2, created_at=NOW,
                                                 expires_at=NOW + timedelta(minutes=30)))
            for _ in range(2):
                create_account_session(session, user_id, hash_session_token(uuid.uuid4().hex),
                                       NOW + timedelta(days=1))
        self.ids.append(user_id)
        return user_id, email

    def service(self, gateway=None, clock=lambda: NOW):
        return VerificationService(lambda: self.factory, gateway if gateway is not None else Gateway(),
                                   CONFIG, clock)

    def token(self, user_id, email, *, expires=NOW + timedelta(hours=24), consumed=None):
        token = new_verification_token()
        with self.factory() as session, session.begin():
            session.add(AccountEmailVerificationToken(
                user_id=user_id, email_hash=email_digest(email), token_hash=hash_verification_token(token),
                created_at=NOW - timedelta(hours=25) if expires <= NOW else NOW,
                expires_at=expires, consumed_at=consumed,
            ))
        return token

    def snapshot(self, user_id):
        models = (User, AccountSession, AccountEmailVerificationToken, AccountRecoveryToken,
                  AccountBilling, AccountEntitlement, AccountUsageDaily)
        with self.factory() as session:
            return {model.__tablename__: list(session.execute(
                select(model.__table__).where((model.id if model is User else model.user_id) == user_id)
                .order_by(*model.__table__.primary_key.columns)
            ).all()) for model in models}

    def verified_at(self, user_id):
        with self.factory() as session:
            return session.scalar(select(User.email_verified_at).where(User.id == user_id))

    def start(self, work):
        box = {}
        def run():
            # No stdout redirection here: it is process-wide, and nested
            # redirections from several threads would restore out of order.
            try:
                box["value"] = work()
            except Exception as error:
                box["error"] = error
        thread = threading.Thread(target=run, daemon=True)
        worker = (thread, box)
        self.workers.append(worker)
        thread.start()
        return worker

    def finish(self, worker):
        thread, box = worker
        thread.join(WAIT_SECONDS)
        assert not thread.is_alive()
        return box

    def settle(self):
        deadline = time.monotonic() + WAIT_SECONDS
        for thread, _ in self.workers:
            thread.join(max(0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread, _ in self.workers)


def quietly(work):
    """Run service code whose fixed event codes are not part of this tool's output.
    Main thread only (see Context.start)."""
    with redirect_stdout(io.StringIO()) as captured:
        result = work()
    return result, captured.getvalue()


@contextmanager
def held_account(ctx, user_id):
    with ctx.factory() as session, session.begin():
        assert lock_user(session, user_id) is not None
        yield


def wait_for_lock(ctx):
    deadline = time.monotonic() + WAIT_SECONDS
    with ctx.engine.connect() as connection:
        while time.monotonic() < deadline:
            found = connection.execute(text(
                f"SELECT count(*) FROM pg_stat_activity WHERE application_name = '{APPLICATION_NAME}' "
                "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
            )).scalar()
            connection.rollback()  # Refresh pg_stat_activity's transaction snapshot.
            if found:
                return
            time.sleep(0.02)
    raise AssertionError()


def expect_invalid(work):
    try:
        work()
    except repository.InvalidVerificationToken:
        return
    raise AssertionError()


def check_schema_and_grants(ctx):
    with ctx.engine.connect() as connection:
        columns = {row[0]: row[1:] for row in connection.execute(text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema='kalillac' AND table_name='account_email_verification_tokens'"
        ))}
        assert set(columns) == {"id", "user_id", "email_hash", "token_hash", "created_at", "expires_at",
                                "consumed_at"}
        assert columns["id"][0] == "uuid"
        assert all(nullable == ("YES" if name == "consumed_at" else "NO")
                   for name, (_, nullable) in columns.items())
        verified = connection.execute(text(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema='kalillac' AND table_name='users' AND column_name='email_verified_at'"
        )).one()
        assert tuple(verified) == ("timestamp with time zone", "YES", None)
        grants = {row[0] for row in connection.execute(text(
            "SELECT privilege_type FROM information_schema.role_table_grants WHERE table_schema='kalillac' "
            "AND table_name='account_email_verification_tokens' AND grantee=current_user"
        ))}
        assert grants == {"SELECT", "INSERT", "UPDATE"}
        assert connection.execute(text(
            "SELECT count(*) FROM pg_indexes WHERE schemaname='kalillac' AND "
            "tablename='account_email_verification_tokens' AND "
            "indexname='ix_account_email_verification_tokens_user_id'"
        )).scalar() == 1


def check_storage_and_reissue(ctx):
    user_id, email = ctx.user()
    gateway = Gateway()
    service = ctx.service(gateway)
    quietly(lambda: service.request_verification(user_id))
    quietly(lambda: service.request_verification(user_id))
    first, second = (token_of(link) for _, link in gateway.sent)
    assert [to for to, _ in gateway.sent] == [email, email]
    with ctx.factory() as session:
        tokens = list(session.scalars(select(AccountEmailVerificationToken)
                                      .where(AccountEmailVerificationToken.user_id == user_id)))
        assert {row.token_hash for row in tokens} == {hash_verification_token(first), hash_verification_token(second)}
        assert all(row.email_hash == email_digest(email) for row in tokens)
        assert sum(row.consumed_at is None for row in tokens) == 1
        assert all(row.expires_at - row.created_at == timedelta(hours=24) for row in tokens)
        assert all(first not in str(row.__dict__) and email not in str(row.__dict__) for row in tokens)
    expect_invalid(lambda: service.confirm_verification(first))
    service.confirm_verification(second)
    assert ctx.verified_at(user_id) is not None


def check_success_changes_only_verification(ctx):
    user_id, email = ctx.user(paid=True)
    other_id, _ = ctx.user(paid=True)
    token = ctx.token(user_id, email)
    before, other = ctx.snapshot(user_id), ctx.snapshot(other_id)
    ctx.service().confirm_verification(token)
    after = ctx.snapshot(user_id)
    for table in ("account_sessions", "account_recovery_tokens", "account_billing",
                  "account_entitlements", "account_usage_daily"):
        assert before[table] == after[table]
    [old_user], [new_user] = before["users"], after["users"]
    assert new_user.email_verified_at is not None
    assert {k: v for k, v in old_user._mapping.items() if k != "email_verified_at"} == {
        k: v for k, v in new_user._mapping.items() if k != "email_verified_at"}
    assert ctx.snapshot(other_id) == other


def check_rejections_change_nothing(ctx):
    user_id, email = ctx.user()
    inactive_id, inactive_email = ctx.user(active=False)
    tokens = [new_verification_token(), ctx.token(user_id, email, expires=NOW),
              ctx.token(user_id, email, consumed=NOW), ctx.token(user_id, "previous@example.invalid")]
    inactive = ctx.token(inactive_id, inactive_email)
    before, inactive_before = ctx.snapshot(user_id), ctx.snapshot(inactive_id)
    for token in tokens:
        expect_invalid(lambda: ctx.service().confirm_verification(token))
    expect_invalid(lambda: ctx.service().confirm_verification(inactive))
    assert ctx.snapshot(user_id) == before and ctx.snapshot(inactive_id) == inactive_before


def race(ctx, different):
    user_id, email = ctx.user()
    first = ctx.token(user_id, email)
    second = ctx.token(user_id, email) if different else first
    barrier = threading.Barrier(2)
    def work(token):
        barrier.wait(WAIT_SECONDS)
        ctx.service().confirm_verification(token)
    workers = [ctx.start(lambda token=token: work(token)) for token in (first, second)]
    results = [ctx.finish(worker) for worker in workers]
    assert sum("error" not in box for box in results) == 1
    assert sum(isinstance(box.get("error"), repository.InvalidVerificationToken) for box in results) == 1
    assert ctx.verified_at(user_id) is not None


def check_same_token_race(ctx):
    race(ctx, False)


def check_sibling_token_race(ctx):
    race(ctx, True)


def check_concurrent_issuance_leaves_one_usable_token(ctx):
    user_id, _ = ctx.user()
    gateway = Gateway()
    service = ctx.service(gateway)
    barrier = threading.Barrier(3)
    def issue():
        barrier.wait(WAIT_SECONDS)
        service.request_verification(user_id)
    workers = [ctx.start(issue) for _ in range(3)]
    assert all("error" not in ctx.finish(worker) for worker in workers)
    with ctx.factory() as session:
        tokens = list(session.scalars(select(AccountEmailVerificationToken)
                                      .where(AccountEmailVerificationToken.user_id == user_id)))
    assert len(tokens) == len(gateway.sent) == 3
    assert sum(row.consumed_at is None for row in tokens) == 1


def check_expiry_after_lock(ctx):
    user_id, email = ctx.user()
    token = ctx.token(user_id, email)
    clock = [NOW]
    service = ctx.service(clock=lambda: clock[0])
    before = ctx.snapshot(user_id)
    with held_account(ctx, user_id):
        worker = ctx.start(lambda: service.confirm_verification(token))
        wait_for_lock(ctx)
        clock[0] = NOW + timedelta(hours=24)
    box = ctx.finish(worker)
    assert isinstance(box.get("error"), repository.InvalidVerificationToken)
    assert ctx.snapshot(user_id) == before


def check_account_lock_isolation(ctx):
    locked, _ = ctx.user()
    other, email = ctx.user()
    token = ctx.token(other, email)
    with held_account(ctx, locked):
        worker = ctx.start(lambda: ctx.service().confirm_verification(token))
        assert "error" not in ctx.finish(worker)
    assert ctx.verified_at(other) is not None and ctx.verified_at(locked) is None


def check_transaction_limits_precede_account_lock(ctx):
    # Confirms the token issuance actually delivered: issuance voids every
    # earlier unused token, so a pre-seeded one could never be confirmed.
    user_id, _ = ctx.user()
    gateway = Gateway()
    real_service_lock, real_repository_lock = service_module.lock_user, repository.lock_user
    seen = []
    def observed(real):
        def lock(session, locked_id):
            seen.append(tuple(session.execute(text(
                "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
            )).one()))
            return real(session, locked_id)
        return lock
    service_module.lock_user = observed(real_service_lock)
    repository.lock_user = observed(real_repository_lock)
    try:
        quietly(lambda: ctx.service(gateway).request_verification(user_id))
        assert len(gateway.sent) == 1
        ctx.service().confirm_verification(token_of(gateway.sent[0][1]))
    finally:
        service_module.lock_user, repository.lock_user = real_service_lock, real_repository_lock
    assert seen == [("5s", "30s"), ("5s", "30s")]
    assert ctx.verified_at(user_id) is not None
    with ctx.engine.connect() as connection:
        assert tuple(connection.execute(text(
            "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
        )).one()) == (LOCK_TIMEOUT, STATEMENT_TIMEOUT)


def check_lock_timeout_rolls_back(ctx):
    user_id, email = ctx.user()
    token = ctx.token(user_id, email)
    gateway = Gateway()
    before = ctx.snapshot(user_id)
    with held_account(ctx, user_id):
        confirming = ctx.start(lambda: ctx.service().confirm_verification(token))
        wait_for_lock(ctx)
        error = ctx.finish(confirming).get("error")
        assert isinstance(error, DBAPIError) and getattr(error.orig, "sqlstate", None) == "55P03"
        issuing = ctx.start(lambda: ctx.service(gateway).request_verification(user_id))
        wait_for_lock(ctx)
        assert "error" not in ctx.finish(issuing)
    assert gateway.sent == []
    assert ctx.snapshot(user_id) == before


def check_rollback_after_mutation(ctx):
    user_id, email = ctx.user()
    token = ctx.token(user_id, email)
    ctx.token(user_id, email)
    before = ctx.snapshot(user_id)
    real = repository.invalidate_unused
    def failing(*args):
        real(*args)
        raise RuntimeError()
    repository.invalidate_unused = failing
    try:
        try:
            ctx.service().confirm_verification(token)
        except RuntimeError:
            pass
        else:
            raise AssertionError()
    finally:
        repository.invalidate_unused = real
    assert ctx.snapshot(user_id) == before


def check_delivery_failure_rolls_back_issuance(ctx):
    user_id, _ = ctx.user()
    gateway = Gateway()
    service = ctx.service(gateway)
    quietly(lambda: service.request_verification(user_id))
    previous = token_of(gateway.sent[-1][1])
    gateway.fail = True
    _, output = quietly(lambda: service.request_verification(user_id))
    assert output == "ACCOUNT_EMAIL_VERIFICATION_DELIVERY_FAILED\n"
    failed = token_of(gateway.sent[-1][1])
    with ctx.factory() as session:
        stored = list(session.scalars(select(AccountEmailVerificationToken)
                                      .where(AccountEmailVerificationToken.user_id == user_id)))
    assert [row.token_hash for row in stored] == [hash_verification_token(previous)]
    assert stored[0].consumed_at is None
    expect_invalid(lambda: service.confirm_verification(failed))
    service.confirm_verification(previous)


def check_verified_and_inactive_accounts_get_nothing(ctx):
    verified, _ = ctx.user(verified=True)
    inactive, _ = ctx.user(active=False)
    gateway = Gateway()
    for user_id in (verified, inactive):
        quietly(lambda: ctx.service(gateway).request_verification(user_id))
    assert gateway.sent == []
    with ctx.factory() as session:
        assert session.scalar(select(func.count()).select_from(AccountEmailVerificationToken).where(
            AccountEmailVerificationToken.user_id.in_([verified, inactive]))) == 0


def check_constraints_and_cascade(ctx):
    user_id, email = ctx.user()
    with ctx.factory() as session, session.begin():
        for field_name, value in (
            ("token_hash", "g" * 64), ("token_hash", "a" * 63), ("token_hash", "A" * 64),
            ("email_hash", "g" * 64), ("email_hash", "a" * 65), ("email_hash", "A" * 64),
            ("expires_at", NOW), ("user_id", uuid.UUID(int=0)),
        ):
            values = dict(user_id=user_id, email_hash=email_digest(email), token_hash=uuid.uuid4().hex * 2,
                          created_at=NOW, expires_at=NOW + timedelta(hours=24))
            values[field_name] = value
            try:
                with session.begin_nested():
                    session.add(AccountEmailVerificationToken(**values))
                    session.flush()
            except DBAPIError:
                pass
            else:
                raise AssertionError()
    token = ctx.token(user_id, email)
    with ctx.factory() as session, session.begin():
        try:
            with session.begin_nested():
                session.add(AccountEmailVerificationToken(
                    user_id=user_id, email_hash=email_digest(email), token_hash=hash_verification_token(token),
                    created_at=NOW, expires_at=NOW + timedelta(hours=24),
                ))
                session.flush()
        except IntegrityError:
            pass
        else:
            raise AssertionError()
        session.execute(delete(User).where(User.id == user_id))
    with ctx.factory() as session:
        assert session.scalar(select(func.count()).select_from(AccountEmailVerificationToken)
                              .where(AccountEmailVerificationToken.user_id == user_id)) == 0


CHECKS = (
    check_schema_and_grants, check_storage_and_reissue, check_success_changes_only_verification,
    check_rejections_change_nothing, check_same_token_race, check_sibling_token_race,
    check_concurrent_issuance_leaves_one_usable_token, check_expiry_after_lock,
    check_account_lock_isolation, check_transaction_limits_precede_account_lock,
    check_lock_timeout_rolls_back, check_rollback_after_mutation,
    check_delivery_failure_rolls_back_issuance, check_verified_and_inactive_accounts_get_nothing,
    check_constraints_and_cascade,
)


def cleanup(ctx):
    prefix = f"verification-verify+{ctx.run_id}-"
    with ctx.factory() as session, session.begin():
        session.execute(delete(User).where(User.id.in_(ctx.ids), User.email.startswith(prefix, autoescape=True)))
    with ctx.factory() as session:
        assert session.scalar(select(func.count()).select_from(User).where(User.id.in_(ctx.ids))) == 0
        assert session.scalar(select(func.count()).select_from(AccountEmailVerificationToken)
                              .where(AccountEmailVerificationToken.user_id.in_(ctx.ids))) == 0


def execute(ctx, checks=CHECKS):
    failed = 0
    for check in checks:
        passed = False
        try:
            # Service event codes from a check (and its workers) are not output.
            with redirect_stdout(io.StringIO()):
                check(ctx)
            passed = True
        except Exception:
            pass
        if passed:
            print("PASS " + check.__name__)
        else:
            failed += 1
            print("FAIL " + check.__name__)
        if not ctx.settle():
            print("VERIFICATION_VERIFY_WORKERS_ACTIVE")
            return 3
    try:
        cleanup(ctx)
    except Exception:
        print("VERIFICATION_VERIFY_CLEANUP_FAILED")
        return 1
    print(f"RESULT: {len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    engine = None
    result = 1
    try:
        parser = Parser(description="Disposable PostgreSQL email-verification verification")
        parser.add_argument("--expect-db", required=True)
        parser.add_argument("--expect-user", required=True)
        args = parser.parse_args(argv)
        validate_destination(args, environ)
        engine = build_engine(environ)
        guard(engine)
        ctx = Context(engine, sessionmaker(bind=engine, expire_on_commit=False))
        result = execute(ctx)
    except VerificationRefused:
        print("VERIFICATION_VERIFY_REFUSED")
        result = 2
    except Exception:
        print("VERIFICATION_VERIFY_FAILED")
        result = 1
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                print("VERIFICATION_VERIFY_DISPOSE_FAILED")
                result = 1
    return result


if __name__ == "__main__":
    sys.exit(main())
