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
    check_route_prevalidation(ctx)


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
    route_race(ctx, False)


def check_two_token_race(ctx):
    race(ctx, True)
    route_race(ctx, True)


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
    if ctx.engine.dialect.name == "postgresql":
        check_singleton_authority(ctx)


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



def route_application(ctx, gate, gateway, clock=lambda: NOW):
    from fastapi import FastAPI
    from kalillac_accounts.recovery_router import build_recovery_router
    from kalillac_accounts.request_guard import UnsafeRequestGuard
    app = FastAPI()
    environ = {
        "KALILLAC_ACCOUNT_RECOVERY_ENABLED": "true", "KALILLAC_ACCOUNTS_ENABLED": "true",
        "KALILLAC_DB_ENABLED": "true", "KALILLAC_ACCOUNT_RECOVERY_URL": CONFIG.recovery_url,
        "KALILLAC_EMAIL_FROM": CONFIG.email_from,
    }
    app.include_router(build_recovery_router(
        session_factory=lambda: ctx.factory, gateway=gateway, environ=environ,
        clock=clock, password_gate=gate,
    ))
    app.add_middleware(UnsafeRequestGuard, origin="https://recovery.invalid")
    return app


def check_route_prevalidation(ctx):
    """Runtime-role HTTP path: rejected credentials cannot reach Argon2."""
    from fastapi.testclient import TestClient
    from sqlalchemy import event
    import kalillac_accounts.recovery_router as router
    from kalillac_accounts.password_work import PasswordWorkGate
    from kalillac_db.models import AccountEmailVerificationToken
    import hashlib
    calls, active = [], set()
    real_hash = router.hash_password
    def forbidden(password):
        calls.append(True)
        raise AssertionError("unexpected password work")
    def begin(connection):
        active.add(id(connection))
    def end(connection):
        active.discard(id(connection))
    class CheckedGate(PasswordWorkGate):
        async def run(self, *args, **kwargs):
            assert not active
            return await super().run(*args, **kwargs)
    event.listen(ctx.engine, "begin", begin)
    event.listen(ctx.engine, "commit", end)
    event.listen(ctx.engine, "rollback", end)
    router.hash_password = forbidden
    try:
        for kind in ("malformed", "unknown", "expired", "consumed", "reissued",
                     "wrong-purpose", "missing-account", "inactive"):
            user_id, email = ctx.user(active=kind != "inactive")
            token = ctx.token(user_id, expires=NOW if kind == "expired" else NOW + timedelta(minutes=30),
                              consumed=NOW if kind == "consumed" else None)
            if kind == "malformed":
                token = "bad"
            elif kind == "unknown":
                token = new_recovery_token()
            elif kind == "reissued":
                ctx.service().request_reset(email)
            elif kind == "wrong-purpose":
                token = new_recovery_token()
                with ctx.factory() as session, session.begin():
                    session.add(AccountEmailVerificationToken(
                        user_id=user_id, email_hash=hashlib.sha256(email.encode()).hexdigest(),
                        token_hash=hash_recovery_token(token), created_at=NOW,
                        expires_at=NOW + timedelta(minutes=30),
                    ))
            elif kind == "missing-account":
                with ctx.factory() as session, session.begin():
                    session.execute(delete(User).where(User.id == user_id))
            before = ctx.snapshot(user_id)
            gateway = Gateway()
            with TestClient(route_application(ctx, CheckedGate(), gateway),
                            base_url="https://recovery.invalid") as client:
                response = client.post("/api/account/recovery/confirm",
                    headers={"Origin": "https://recovery.invalid"},
                    json={"token": token, "password": NEW_PASSWORD})
            assert response.status_code == 400
            assert response.json() == {"error": "invalid_or_expired_token"}
            assert calls == [] and ctx.snapshot(user_id) == before and gateway.changed == []
        # Move state at an explicit post-precheck barrier. The final locked
        # transaction must reject it even though hashing was admitted.
        for change in ("expiry", "consumed", "reissued", "inactive"):
            user_id, _ = ctx.user()
            token = ctx.token(user_id)
            clock = [NOW]
            captured = []
            value = real_hash(NEW_PASSWORD)
            def counted(password):
                assert not active
                calls.append(True)
                return value
            router.hash_password = counted
            class ChangingGate(PasswordWorkGate):
                async def run(self, *args, **kwargs):
                    assert not active
                    with ctx.factory() as session, session.begin():
                        if change == "expiry":
                            clock[0] = NOW + timedelta(minutes=30)
                        elif change == "consumed":
                            session.execute(update(AccountRecoveryToken).where(
                                AccountRecoveryToken.user_id == user_id).values(consumed_at=NOW))
                        elif change == "reissued":
                            repository.lock_user(session, user_id)
                            repository.issue_token(session, user_id, hash_recovery_token(new_recovery_token()), NOW)
                        else:
                            session.execute(update(User).where(User.id == user_id).values(is_active=False))
                    captured.append(ctx.snapshot(user_id))
                    return await super().run(*args, **kwargs)
            gateway = Gateway()
            previous = len(calls)
            with TestClient(route_application(ctx, ChangingGate(), gateway, clock=lambda: clock[0]),
                            base_url="https://recovery.invalid") as client:
                response = client.post("/api/account/recovery/confirm",
                    headers={"Origin": "https://recovery.invalid"},
                    json={"token": token, "password": NEW_PASSWORD})
            assert response.status_code == 400 and len(calls) == previous + 1
            assert ctx.snapshot(user_id) == captured[0] and gateway.changed == []
        assert not active
    finally:
        router.hash_password = real_hash
        event.remove(ctx.engine, "begin", begin)
        event.remove(ctx.engine, "commit", end)
        event.remove(ctx.engine, "rollback", end)


def route_race(ctx, different):
    """Separate HTTP event loops, both prechecked before either hash starts."""
    from fastapi.testclient import TestClient
    from starlette.concurrency import run_in_threadpool
    from kalillac_accounts.password_work import PasswordWorkGate
    user_id, _ = ctx.user()
    first = ctx.token(user_id)
    second = ctx.token(user_id) if different else first
    barrier = threading.Barrier(2)
    class BarrierGate(PasswordWorkGate):
        async def run(self, *args, **kwargs):
            await run_in_threadpool(barrier.wait, WAIT_SECONDS)
            return await super().run(*args, **kwargs)
    gateway = Gateway()
    app = route_application(ctx, BarrierGate(), gateway)
    def request(token):
        with TestClient(app, base_url="https://recovery.invalid") as client:
            return client.post("/api/account/recovery/confirm",
                headers={"Origin": "https://recovery.invalid"},
                json={"token": token, "password": NEW_PASSWORD}).status_code
    workers = [ctx.start(lambda token=token: request(token)) for token in (first, second)]
    boxes = [ctx.finish(worker) for worker in workers]
    assert all("error" not in box for box in boxes)
    assert sorted(box["value"] for box in boxes) == [200, 400]
    assert len(gateway.changed) == 1


def check_singleton_authority(ctx):
    from types import SimpleNamespace
    from kalillac_server import DatabaseSingleton, SingletonHeld, SingletonMonitor
    identity = "slice2-" + ctx.run_id
    first = DatabaseSingleton(ctx.engine, identity)
    duplicate = DatabaseSingleton(ctx.engine, identity)
    staging = DatabaseSingleton(ctx.engine, identity + "-staging")
    first.acquire()
    try:
        first.check()
        try:
            duplicate.acquire()
        except SingletonHeld:
            pass
        else:
            raise AssertionError()
        staging.acquire()
        staging.check()
        staging.close()
        with ctx.engine.connect() as connection:
            assert connection.execute(text("SELECT pg_terminate_backend(:pid)"),
                                      {"pid": first._pid}).scalar()
        server = SimpleNamespace(should_exit=False)
        monitor = SingletonMonitor(first, server)
        monitor.check_once()
        assert monitor.lost and server.should_exit
    finally:
        first.close()
        duplicate.close()
        staging.close()
    duplicate.acquire()
    duplicate.check()
    duplicate.close()
    check_launcher_crash_rollback(ctx)

LAUNCHER_CHILD = Path(__file__).resolve().parents[1] / "tests/launcher_child_support.py"


class LauncherVerificationChild:
    """Bounded stdlib harness shared by the real PostgreSQL CI check."""
    def __init__(self, directory, identity, environ):
        import json
        import queue
        import subprocess
        self.events, self.pending = [], []
        self.queue = queue.Queue()
        self.process = subprocess.Popen(
            [sys.executable, "-B", str(LAUNCHER_CHILD), "rollback", str(directory), identity],
            cwd=LAUNCHER_CHILD.parents[1], env=environ, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        def read():
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                self.events.append(event)
                self.queue.put(event)
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def event(self, name):
        for index, event in enumerate(self.pending):
            if event.get("event") == name:
                return self.pending.pop(index)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            event = self.queue.get(timeout=max(0.01, deadline - time.monotonic()))
            if event.get("event") == name:
                return event
            self.pending.append(event)
        raise TimeoutError("launcher event missing")

    def command(self, value):
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=8)
        self.reader.join(timeout=8)
        assert not self.reader.is_alive()
        self.process.stdin.close()
        self.process.stdout.close()


def check_launcher_crash_rollback(ctx):
    """Real child crash, separate application transaction, independent proof.

    A parent-owned large object is isolated test state: no application rows,
    schema changes, grants or migration-role privileges are needed.
    """
    from concurrent.futures import ThreadPoolExecutor
    import tempfile
    from urllib.request import ProxyHandler, build_opener
    from kalillac_server import DatabaseSingleton, lock_key

    tag = uuid.uuid4().hex
    identity = "rollback-" + tag
    key = lock_key("rollback-transaction-" + tag)
    assert key != lock_key(identity)
    env = dict(os.environ)
    env.update({
        "KALILLAC_DB_HOST": HOST, "KALILLAC_DB_PORT": str(ctx.engine.url.port),
        "KALILLAC_DB_NAME": REQUIRED_DATABASE, "KALILLAC_DB_USER": REQUIRED_ROLE,
        "KALILLAC_DB_PASSWORD": ctx.engine.url.password,
        "SLICE2_CHILD_REAL_PG": "1", "SLICE2_ROLLBACK_TAG": tag,
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    child = replacement = None
    oid = app_pid = None
    evidence = {"tag": tag, "lock_key": key}
    try:
        with ctx.engine.begin() as connection:
            oid = connection.execute(text("SELECT lo_create(0)")).scalar_one()
            assert connection.execute(text("SELECT lo_get(:oid)"), {"oid": oid}).scalar_one() == b""
        env["SLICE2_ROLLBACK_OID"] = str(oid)
        with tempfile.TemporaryDirectory(prefix="kalillac-pg-rollback-") as directory:
            child = LauncherVerificationChild(directory, identity, env)
            singleton_pid = child.event("held")["pid"]
            port = child.event("ready")["port"]
            def request():
                try:
                    with build_opener(ProxyHandler({})).open(
                            f"http://127.0.0.1:{port}/", timeout=15) as response:
                        return response.status
                except Exception:
                    return "disconnected"
            with ThreadPoolExecutor(max_workers=1) as pool:
                response = pool.submit(request)
                try:
                    active = child.event("transaction")
                    app_pid = active["pid"]
                    assert active == {"event": "transaction", "pid": app_pid,
                        "singleton_pid": singleton_pid, "oid": oid, "tag": tag, "lock_key": key}
                    assert app_pid != singleton_pid
                    with ctx.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                        row = connection.execute(text(
                            "SELECT datname, usename, application_name, state "
                            "FROM pg_stat_activity WHERE pid=:pid"), {"pid": app_pid}).one()
                        assert tuple(row) == (REQUIRED_DATABASE, REQUIRED_ROLE,
                                             "slice2-rollback-" + tag, "idle in transaction")
                        assert connection.execute(text("SELECT lo_get(:oid)"),
                                                  {"oid": oid}).scalar_one() == b""
                        assert connection.execute(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                                  {"key": key}).scalar_one() is False
                        assert connection.execute(text(
                            "SELECT usename, application_name FROM pg_stat_activity WHERE pid=:pid"),
                            {"pid": singleton_pid}).one() == (REQUIRED_ROLE, "kalillac-recovery-verify")
                        assert connection.execute(text("SELECT pg_terminate_backend(:pid)"),
                                                  {"pid": singleton_pid}).scalar_one()
                    evidence.update(application_pid=app_pid, singleton_pid=singleton_pid,
                                    oid=oid, separate_application_transaction=True,
                                    uncommitted_write_observed_by_child=True,
                                    parent_saw_original_value_before_crash=True,
                                    parent_lock_attempt_blocked_before_crash=True)
                    child.command("FAIL")
                    fatal = child.event("fatal")
                    child.event("lost")
                    replacement = DatabaseSingleton(ctx.engine, identity)
                    replacement.acquire()
                    replacement.check()
                    assert child.process.poll() is None
                    status = child.process.wait(timeout=5)
                    assert status != 0
                    assert time.monotonic() - fatal["expires_at"] < 1.0
                    assert response.result(timeout=5) == "disconnected"
                    replacement.check()
                    with ctx.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                        deadline = time.monotonic() + 5
                        while True:
                            exists = connection.execute(text(
                                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=:pid)"),
                                {"pid": app_pid}).scalar_one()
                            if not exists:
                                break
                            if time.monotonic() >= deadline:
                                raise AssertionError("application backend survived crash")
                            threading.Event().wait(min(0.05, deadline - time.monotonic()))
                        assert connection.execute(text("SELECT lo_get(:oid)"),
                                                  {"oid": oid}).scalar_one() == b""
                    replacement.check()
                    with ctx.engine.begin() as connection:
                        assert connection.execute(text("SELECT pg_try_advisory_xact_lock(:key)"),
                                                  {"key": key}).scalar_one()
                        connection.execute(text("SELECT lo_put(:oid, 0, :value)"),
                                           {"oid": oid, "value": b"post-crash-transaction"})
                    replacement.check()
                    with ctx.engine.connect() as connection:
                        assert connection.execute(text("SELECT lo_get(:oid)"),
                            {"oid": oid}).scalar_one() == b"post-crash-transaction"
                    replacement.check()
                    evidence.update(exit_code=status, application_backend_disappeared=True,
                        tagged_uncommitted_write_absent=True, transaction_lock_released=True,
                        new_transaction_committed=True, replacement_healthy_through_checks=True,
                        fatal_deadline_seconds=2.0, child_events=child.events)
                finally:
                    # Close before executor shutdown, including readiness/assertion failures.
                    child.close()
    finally:
        if child is not None:
            child.close()
        if replacement is not None:
            replacement.close()
        # Clean only our uniquely named application backend and our own LO.
        # Termination is a failure-cleanup fallback, never rollback proof.
        with ctx.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            pids = list(connection.execute(text(
                "SELECT pid FROM pg_stat_activity WHERE usename=current_user "
                "AND application_name=:name"), {"name": "slice2-rollback-" + tag}).scalars())
            for pid in pids:
                connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            if oid is not None:
                assert connection.execute(text(
                    "SELECT lomowner=current_user::regrole::oid FROM pg_largeobject_metadata WHERE oid=:oid"),
                    {"oid": oid}).scalar_one()
                assert connection.execute(text("SELECT lo_unlink(:oid)"), {"oid": oid}).scalar_one() == 1
                assert not connection.execute(text(
                    "SELECT EXISTS(SELECT 1 FROM pg_largeobject_metadata WHERE oid=:oid)"),
                    {"oid": oid}).scalar_one()
    evidence["isolated_state_cleaned"] = True
    ctx.launcher_rollback_evidence = evidence
    print("PASS launcher_crash_application_backend_gone")
    print("PASS launcher_crash_uncommitted_write_absent")
    print("PASS launcher_crash_lock_released_and_new_transaction_committed")
    print("PASS launcher_crash_replacement_ownership_preserved")


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
