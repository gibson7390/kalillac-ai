"""Disposable PostgreSQL recovery verifier; fixed database/role, no real email or Stripe.

Only kalillac_staging as kalillac_staging_app on literal loopback is accepted.
Migrations are applied by CI's separate migration role, never by this tool.
All output is fixed codes/check names/counts; no SQL, parameters or exceptions.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import URL, create_engine, delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from kalillac_accounts.email_gateway import EmailDeliveryError
from kalillac_accounts.passwords import hash_password, verify_password
from kalillac_accounts.recovery import RecoveryService
from kalillac_accounts.recovery_config import RecoveryConfig
from kalillac_accounts.recovery_tokens import hash_recovery_token, new_recovery_token
from kalillac_db.models import (
    AccountBilling, AccountEntitlement, AccountRecoveryToken, AccountSession,
    AccountUsageDaily, User,
)
from kalillac_db.repositories import recovery as repository
from kalillac_db.repositories.accounts import create_account_session
from kalillac_accounts.tokens import hash_session_token

REQUIRED_DATABASE = "kalillac_staging"
REQUIRED_ROLE = "kalillac_staging_app"
HOST = "127.0.0.1"
LOCK_TIMEOUT, STATEMENT_TIMEOUT = "10s", "45s"
WAIT_SECONDS = 15
NOW = datetime(2030, 6, 15, 12, tzinfo=timezone.utc)
CONFIG = RecoveryConfig("https://recovery.invalid/reset", "sender@example.invalid")
PASSWORD = "local-verifier-old-password"
NEW_PASSWORD = "local-verifier-new-password"


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
            "passfile": os.devnull, "application_name": "kalillac-recovery-verify",
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
            "SELECT to_regclass('kalillac.account_recovery_tokens') IS NOT NULL"
        )).scalar()
        limits = tuple(connection.execute(text("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")).one())
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
    reset: list = field(default_factory=list, repr=False)
    changed: list = field(default_factory=list, repr=False)
    fail_reset: bool = False
    fail_changed: bool = False

    def send_password_reset(self, to, link):
        self.reset.append((to, link))
        if self.fail_reset:
            raise EmailDeliveryError()

    def send_password_changed(self, to):
        self.changed.append(to)
        if self.fail_changed:
            raise EmailDeliveryError()


@dataclass
class Context:
    engine: object
    factory: object
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ids: list = field(default_factory=list)
    workers: list = field(default_factory=list)

    def user(self, *, active=True, paid=False):
        user_id = uuid.uuid4()
        email = f"recovery-verify+{self.run_id}-{user_id.hex}@example.invalid"
        with self.factory() as session, session.begin():
            session.add(User(id=user_id, email=email, password_hash=hash_password(PASSWORD), is_active=active))
            session.flush()
            session.add(AccountEntitlement(user_id=user_id, tier="paid" if paid else "free", source="billing" if paid else "registration"))
            if paid:
                session.add(AccountBilling(user_id=user_id, stripe_customer_id="cus_" + user_id.hex,
                                           stripe_subscription_id="sub_" + user_id.hex,
                                           stripe_price_id="price_recovery_local", subscription_status="active"))
                session.add(AccountUsageDaily(user_id=user_id, usage_date=NOW.date(), successful_chats=4))
            for _ in range(2):
                create_account_session(session, user_id, hash_session_token(uuid.uuid4().hex), NOW + timedelta(days=1))
        self.ids.append(user_id)
        return user_id, email

    def service(self, gateway=None, clock=lambda: NOW):
        return RecoveryService(lambda: self.factory, gateway if gateway is not None else Gateway(), CONFIG, clock)

    def token(self, user_id, *, expires=NOW + timedelta(minutes=30), consumed=None):
        token = new_recovery_token()
        with self.factory() as session, session.begin():
            session.add(AccountRecoveryToken(
                user_id=user_id, purpose="password_reset", token_hash=hash_recovery_token(token),
                created_at=NOW - timedelta(hours=1) if expires <= NOW else NOW,
                expires_at=expires, consumed_at=consumed,
            ))
        return token

    def snapshot(self, user_id):
        models = (User, AccountSession, AccountRecoveryToken, AccountBilling, AccountEntitlement, AccountUsageDaily)
        with self.factory() as session:
            return {model.__tablename__: list(session.execute(
                select(model.__table__).where((model.id if model is User else model.user_id) == user_id)
                .order_by(*model.__table__.primary_key.columns)
            ).all()) for model in models}

    def start(self, work):
        box = {}
        def run():
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


@contextmanager
def held_account(ctx, user_id):
    with ctx.factory() as session, session.begin():
        assert repository.lock_user(session, user_id) is not None
        yield


def wait_for_lock(ctx):
    deadline = time.monotonic() + WAIT_SECONDS
    with ctx.engine.connect() as connection:
        while time.monotonic() < deadline:
            found = connection.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'kalillac-recovery-verify' "
                "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
            )).scalar()
            connection.rollback()  # Refresh pg_stat_activity's transaction snapshot.
            if found:
                return
            time.sleep(0.02)
    raise AssertionError()


def check_schema_and_grants(ctx):
    with ctx.engine.connect() as connection:
        columns = connection.execute(text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema='kalillac' AND table_name='account_recovery_tokens'"
        )).all()
        assert {row[0] for row in columns} == {
            "id", "user_id", "purpose", "token_hash", "created_at", "expires_at", "consumed_at",
        }
        assert next(row[1] for row in columns if row[0] == "id") == "uuid"
        assert all(row[2] == ("YES" if row[0] == "consumed_at" else "NO") for row in columns)
        grants = {row[0] for row in connection.execute(text(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema='kalillac' AND table_name='account_recovery_tokens' AND grantee=current_user"
        ))}
        assert grants == {"SELECT", "INSERT", "UPDATE"}
        assert connection.execute(text(
            "SELECT count(*) FROM pg_indexes WHERE schemaname='kalillac' "
            "AND tablename='account_recovery_tokens' AND indexname='ix_account_recovery_tokens_user_id'"
        )).scalar() == 1


def check_storage_and_reissue(ctx):
    user_id, email = ctx.user()
    gateway = Gateway()
    service = ctx.service(gateway)
    service.request_reset(email)
    first = gateway.reset[-1][1].split("#token=")[1]
    service.request_reset(email)
    second = gateway.reset[-1][1].split("#token=")[1]
    with ctx.factory() as session:
        tokens = list(session.scalars(select(AccountRecoveryToken).where(AccountRecoveryToken.user_id == user_id).order_by(AccountRecoveryToken.created_at, AccountRecoveryToken.id)))
        assert len(tokens) == 2
        assert {row.token_hash for row in tokens} == {hash_recovery_token(first), hash_recovery_token(second)}
        assert sum(row.consumed_at is None for row in tokens) == 1
        assert all(row.expires_at - row.created_at == timedelta(minutes=30) for row in tokens)
        assert all(first not in str(row.__dict__) and second not in str(row.__dict__) for row in tokens)
    try:
        service.confirm_reset(first, hash_password(NEW_PASSWORD))
    except repository.InvalidRecoveryToken:
        pass
    else:
        raise AssertionError()


def check_success_preserves_account(ctx):
    user_id, _ = ctx.user(paid=True)
    other_id, _ = ctx.user(paid=True)
    token = ctx.token(user_id)
    before, other = ctx.snapshot(user_id), ctx.snapshot(other_id)
    ctx.service().confirm_reset(token, hash_password(NEW_PASSWORD))
    after = ctx.snapshot(user_id)
    for table in ("account_billing", "account_entitlements", "account_usage_daily"):
        assert before[table] == after[table]
    assert ctx.snapshot(other_id) == other
    with ctx.factory() as session:
        user = session.get(User, user_id)
        assert verify_password(user.password_hash, NEW_PASSWORD)
        assert not verify_password(user.password_hash, PASSWORD)
        assert all(row.revoked_at is not None for row in session.scalars(select(AccountSession).where(AccountSession.user_id == user_id)))


def check_rejection_and_expiry(ctx):
    user_id, _ = ctx.user()
    inactive_id, _ = ctx.user(active=False)
    tokens = [new_recovery_token(), ctx.token(user_id, expires=NOW),
              ctx.token(user_id, consumed=NOW), ctx.token(inactive_id)]
    before = ctx.snapshot(user_id)
    for token in tokens:
        try:
            ctx.service().confirm_reset(token, hash_password(NEW_PASSWORD))
        except repository.InvalidRecoveryToken:
            pass
        else:
            raise AssertionError()
    assert ctx.snapshot(user_id) == before


def race(ctx, different):
    user_id, _ = ctx.user()
    first = ctx.token(user_id)
    second = ctx.token(user_id) if different else first
    barrier = threading.Barrier(2)
    password_hash = hash_password(NEW_PASSWORD)
    def work(token):
        barrier.wait(WAIT_SECONDS)
        ctx.service().confirm_reset(token, password_hash)
    workers = [ctx.start(lambda token=token: work(token)) for token in (first, second)]
    results = [ctx.finish(worker) for worker in workers]
    assert sum("error" not in box for box in results) == 1
    assert sum(isinstance(box.get("error"), repository.InvalidRecoveryToken) for box in results) == 1


def check_same_token_race(ctx):
    race(ctx, False)


def check_two_token_race(ctx):
    race(ctx, True)


def check_expiry_after_lock(ctx):
    user_id, _ = ctx.user()
    token = ctx.token(user_id)
    clock = [NOW]
    service = ctx.service(clock=lambda: clock[0])
    password_hash = hash_password(NEW_PASSWORD)
    before = ctx.snapshot(user_id)
    with held_account(ctx, user_id):
        worker = ctx.start(lambda: service.confirm_reset(token, password_hash))
        wait_for_lock(ctx)
        clock[0] = NOW + timedelta(minutes=30)
    box = ctx.finish(worker)
    assert isinstance(box.get("error"), repository.InvalidRecoveryToken)
    assert ctx.snapshot(user_id) == before


def check_account_lock_isolation(ctx):
    locked, _ = ctx.user()
    other, _ = ctx.user()
    token = ctx.token(other)
    password_hash = hash_password(NEW_PASSWORD)
    with held_account(ctx, locked):
        worker = ctx.start(lambda: ctx.service().confirm_reset(token, password_hash))
        assert "error" not in ctx.finish(worker)


def check_transaction_timeouts(ctx):
    user_id, _ = ctx.user()
    token = ctx.token(user_id)
    real = repository.lock_user
    seen = []
    def observed(session, user_id):
        seen.append(tuple(session.execute(text(
            "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
        )).one()))
        return real(session, user_id)
    repository.lock_user = observed
    try:
        ctx.service().confirm_reset(token, hash_password(NEW_PASSWORD))
    finally:
        repository.lock_user = real
    assert seen == [("5s", "30s")]
    with ctx.engine.connect() as connection:
        assert tuple(connection.execute(text(
            "SELECT current_setting('lock_timeout'), current_setting('statement_timeout')"
        )).one()) == (LOCK_TIMEOUT, STATEMENT_TIMEOUT)


def check_lock_timeout_rollback(ctx):
    user_id, _ = ctx.user()
    token = ctx.token(user_id)
    before = ctx.snapshot(user_id)
    password_hash = hash_password(NEW_PASSWORD)
    with held_account(ctx, user_id):
        worker = ctx.start(lambda: ctx.service().confirm_reset(token, password_hash))
        wait_for_lock(ctx)
        box = ctx.finish(worker)
        error = box.get("error")
        assert isinstance(error, DBAPIError)
        assert getattr(error.orig, "sqlstate", None) == "55P03"
    assert ctx.snapshot(user_id) == before


def check_rollback_after_mutation(ctx):
    user_id, _ = ctx.user()
    token = ctx.token(user_id)
    before = ctx.snapshot(user_id)
    real = repository.revoke_all_account_sessions
    def failing(*args):
        real(*args)
        raise RuntimeError()
    repository.revoke_all_account_sessions = failing
    try:
        try:
            ctx.service().confirm_reset(token, hash_password(NEW_PASSWORD))
        except RuntimeError:
            pass
        else:
            raise AssertionError()
    finally:
        repository.revoke_all_account_sessions = real
    assert ctx.snapshot(user_id) == before


def check_email_failures(ctx):
    user_id, email = ctx.user()
    gateway = Gateway(fail_reset=True)
    service = ctx.service(gateway)
    service.request_reset(email)
    with ctx.factory() as session:
        assert all(row.consumed_at is not None for row in session.scalars(
            select(AccountRecoveryToken).where(AccountRecoveryToken.user_id == user_id)))
    gateway.fail_reset = False
    service.request_reset(email)
    token = gateway.reset[-1][1].split("#token=")[1]
    gateway.fail_changed = True
    service.confirm_reset(token, hash_password(NEW_PASSWORD))
    with ctx.factory() as session:
        assert verify_password(session.get(User, user_id).password_hash, NEW_PASSWORD)


def check_login_racing_reset_gets_no_session(ctx):
    # The reset is uncommitted while a login with the old password verifies it
    # and then waits on the account lock; after commit the login must refuse.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from kalillac_accounts.router import AccountSettings, build_account_router
    user_id, email = ctx.user()
    token = ctx.token(user_id)
    app = FastAPI()
    app.include_router(build_account_router(
        session_factory=lambda: ctx.factory, settings=AccountSettings(cookie_secure=False),
    ))
    client = TestClient(app, base_url="https://localhost")
    def login(password):
        return client.post("/api/account/login", json={"email": email, "password": password}).status_code
    with ctx.factory() as session, session.begin():
        repository.confirm_token(session, hash_recovery_token(token), hash_password(NEW_PASSWORD), lambda: NOW)
        worker = ctx.start(lambda: login(PASSWORD))
        wait_for_lock(ctx)
    assert ctx.finish(worker).get("value") == 401
    with ctx.factory() as session:
        assert all(row.revoked_at is not None for row in session.scalars(
            select(AccountSession).where(AccountSession.user_id == user_id)))
    assert login(NEW_PASSWORD) == 200


def check_constraints_and_cascade(ctx):
    user_id, _ = ctx.user()
    with ctx.factory() as session, session.begin():
        for field_name, value in (
            ("purpose", "wrong"), ("token_hash", "g" * 64), ("token_hash", "a" * 63),
            ("token_hash", "A" * 64), ("expires_at", NOW), ("user_id", uuid.UUID(int=0)),
        ):
            values = dict(user_id=user_id, purpose="password_reset", token_hash=uuid.uuid4().hex * 2,
                          created_at=NOW, expires_at=NOW + timedelta(minutes=30))
            values[field_name] = value
            try:
                with session.begin_nested():
                    session.add(AccountRecoveryToken(**values))
                    session.flush()
            except IntegrityError:
                pass
            else:
                raise AssertionError()
    token = ctx.token(user_id)
    with ctx.factory() as session, session.begin():
        try:
            with session.begin_nested():
                session.add(AccountRecoveryToken(
                    user_id=user_id, purpose="password_reset", token_hash=hash_recovery_token(token),
                    created_at=NOW, expires_at=NOW + timedelta(minutes=30),
                ))
                session.flush()
        except IntegrityError:
            pass
        else:
            raise AssertionError()
        session.execute(delete(User).where(User.id == user_id))
    with ctx.factory() as session:
        assert session.scalar(select(func.count()).select_from(AccountRecoveryToken).where(AccountRecoveryToken.user_id == user_id)) == 0


CHECKS = (
    check_schema_and_grants, check_storage_and_reissue, check_success_preserves_account,
    check_rejection_and_expiry, check_same_token_race, check_two_token_race,
    check_expiry_after_lock, check_account_lock_isolation, check_transaction_timeouts,
    check_lock_timeout_rollback, check_rollback_after_mutation, check_email_failures,
    check_login_racing_reset_gets_no_session, check_constraints_and_cascade,
)


def cleanup(ctx):
    prefix = f"recovery-verify+{ctx.run_id}-"
    with ctx.factory() as session, session.begin():
        session.execute(delete(User).where(User.id.in_(ctx.ids), User.email.startswith(prefix, autoescape=True)))
    with ctx.factory() as session:
        assert session.scalar(select(func.count()).select_from(User).where(User.id.in_(ctx.ids))) == 0
        assert session.scalar(select(func.count()).select_from(AccountRecoveryToken).where(AccountRecoveryToken.user_id.in_(ctx.ids))) == 0


def execute(ctx, checks=CHECKS):
    failed = 0
    for check in checks:
        try:
            check(ctx)
            print("PASS " + check.__name__)
        except Exception:
            failed += 1
            print("FAIL " + check.__name__)
        if not ctx.settle():
            print("RECOVERY_VERIFY_WORKERS_ACTIVE")
            return 3
    try:
        cleanup(ctx)
    except Exception:
        print("RECOVERY_VERIFY_CLEANUP_FAILED")
        return 1
    print(f"RESULT: {len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    engine = None
    result = 1
    try:
        parser = Parser(description="Disposable PostgreSQL account-recovery verification")
        parser.add_argument("--expect-db", required=True)
        parser.add_argument("--expect-user", required=True)
        args = parser.parse_args(argv)
        validate_destination(args, environ)
        engine = build_engine(environ)
        guard(engine)
        ctx = Context(engine, sessionmaker(bind=engine, expire_on_commit=False))
        result = execute(ctx)
    except VerificationRefused:
        print("RECOVERY_VERIFY_REFUSED")
        result = 2
    except Exception:
        print("RECOVERY_VERIFY_FAILED")
        result = 1
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                print("RECOVERY_VERIFY_DISPOSE_FAILED")
                result = 1
    return result


if __name__ == "__main__":
    sys.exit(main())
