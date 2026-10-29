"""Real separate SQLite connections: races, rollback, locking order and transaction bounds."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import threading

import pytest
from sqlalchemy import event, update
from sqlalchemy.dialects import postgresql

from account_test_db import make_session_factory, make_sqlite_file_engine
from verification_test_support import EMAIL, ENV, NOW, FakeVerificationEmail, add_user, rows, user_row
from kalillac_accounts import email_verification as service_module
from kalillac_accounts.email_verification import (
    VerificationService, VerificationUnavailable, apply_transaction_limits,
    hash_verification_token, new_verification_token,
)
from kalillac_accounts.verification_config import load_verification_config
from kalillac_db.models import AccountEmailVerificationToken, Base, User
from kalillac_db.repositories import email_verification as repository


@pytest.fixture
def db(tmp_path):
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def seed(factory, user_id, email=EMAIL):
    token = new_verification_token()
    with factory() as session, session.begin():
        session.add(AccountEmailVerificationToken(
            user_id=user_id, email_hash=hashlib.sha256(email.encode()).hexdigest(),
            token_hash=hash_verification_token(token), created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        ))
    return token


def service_for(db, gateway=None, clock=lambda: NOW):
    gateway = gateway or FakeVerificationEmail()
    return VerificationService(lambda: db, gateway, load_verification_config(gateway, ENV), clock)


@pytest.mark.parametrize("different_tokens", [False, True])
def test_parallel_confirmations_have_exactly_one_winner(db, different_tokens):
    user_id = add_user(db)
    first = seed(db, user_id)
    second = seed(db, user_id) if different_tokens else first
    service = service_for(db)
    barrier = threading.Barrier(2)
    def confirm(token):
        barrier.wait(timeout=10)
        try:
            service.confirm_verification(token)
            return True
        except repository.InvalidVerificationToken:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=15) for future in
                   [pool.submit(confirm, token) for token in (first, second)]]
    assert sorted(results) == [False, True]
    assert user_row(db, user_id)["email_verified_at"] is not None
    assert all(row["consumed_at"] is not None for row in rows(db, AccountEmailVerificationToken))


def test_parallel_issuance_leaves_exactly_one_usable_token(db):
    user_id = add_user(db)
    gateway = FakeVerificationEmail()
    service = service_for(db, gateway)
    barrier = threading.Barrier(3)
    def issue():
        barrier.wait(timeout=10)
        service.request_verification(user_id)
    with ThreadPoolExecutor(max_workers=3) as pool:
        for future in [pool.submit(issue) for _ in range(3)]:
            future.result(timeout=30)
    stored = rows(db, AccountEmailVerificationToken)
    assert len(stored) == len(gateway.sent) == 3
    assert sum(row["consumed_at"] is None for row in stored) == 1


@pytest.mark.parametrize("stage", ["after_consume", "after_verify", "commit"])
def test_failed_confirmation_rolls_back_completely(db, monkeypatch, stage):
    user_id = add_user(db)
    token = seed(db, user_id)
    sibling = seed(db, user_id)
    before = {model: rows(db, model) for model in (User, AccountEmailVerificationToken)}
    def fail(*args, **kwargs):
        raise RuntimeError("private database detail")
    if stage == "after_consume":
        # The token UPDATE has run; the users UPDATE that follows fails.
        @event.listens_for(db.kw["bind"], "before_cursor_execute")
        def failing_user_update(connection, cursor, statement, params, context, many):
            if statement.startswith("UPDATE kalillac.users"):
                event.remove(db.kw["bind"], "before_cursor_execute", failing_user_update)
                fail()
    elif stage == "after_verify":
        monkeypatch.setattr(repository, "invalidate_unused", fail)
    else:
        @event.listens_for(db.class_, "before_commit", once=True)
        def failed_commit(session):
            fail()
    with pytest.raises(RuntimeError):
        service_for(db).confirm_verification(token)
    monkeypatch.undo()
    for model, original in before.items():
        assert rows(db, model) == original
    # Nothing was consumed, so both tokens still work afterwards (one winner).
    service_for(db).confirm_verification(sibling)
    with pytest.raises(repository.InvalidVerificationToken):
        service_for(db).confirm_verification(token)


def test_limits_precede_lock_and_expiry_is_read_after_lock(db, monkeypatch):
    user_id = add_user(db)
    token = seed(db, user_id)
    order = []
    clock = [NOW]
    real_limits, real_lock = service_module.apply_transaction_limits, repository.lock_user
    def limits(session):
        order.append("limits")
        return real_limits(session)
    def lock(session, locked_id):
        order.append("lock")
        clock[0] = NOW + timedelta(hours=24)  # time passes while waiting for the lock
        return real_lock(session, locked_id)
    monkeypatch.setattr(service_module, "apply_transaction_limits", limits)
    monkeypatch.setattr(repository, "lock_user", lock)
    with pytest.raises(repository.InvalidVerificationToken):
        service_for(db, clock=lambda: clock[0]).confirm_verification(token)
    assert order == ["limits", "lock"]
    assert user_row(db, user_id)["email_verified_at"] is None


def test_issuance_rereads_under_lock_after_limits(db, monkeypatch):
    user_id = add_user(db)
    order = []
    real_limits, real_lock = service_module.apply_transaction_limits, service_module.lock_user
    monkeypatch.setattr(service_module, "apply_transaction_limits",
                        lambda session: order.append("limits") or real_limits(session))
    def lock(session, locked_id):
        order.append("lock")
        # Verified by another transaction just before the lock was granted.
        session.execute(update(User).where(User.id == locked_id).values(email_verified_at=NOW))
        return real_lock(session, locked_id)
    monkeypatch.setattr(service_module, "lock_user", lock)
    gateway = FakeVerificationEmail()
    service_for(db, gateway).request_verification(user_id)
    assert order == ["limits", "lock"] and gateway.sent == []
    assert rows(db, AccountEmailVerificationToken) == []


def test_lock_statement_uses_postgresql_for_update(db):
    from kalillac_db.repositories.recovery import lock_user
    statements = []
    @event.listens_for(db.kw["bind"], "before_cursor_execute")
    def capture(connection, cursor, statement, params, context, many):
        if "FROM kalillac.users" in statement:
            statements.append(str(context.compiled.statement.compile(dialect=postgresql.dialect())))
    user_id = add_user(db)
    with db() as session, session.begin():
        lock_user(session, user_id)
    event.remove(db.kw["bind"], "before_cursor_execute", capture)
    assert len(statements) == 1 and statements[0].endswith("FOR UPDATE")


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite", "mysql"])
def test_fixed_transaction_timeouts_and_explicit_test_exception(dialect):
    from types import SimpleNamespace
    class Session:
        def __init__(self):
            self.statements = []
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        def execute(self, statement):
            self.statements.append(str(statement))
    session = Session()
    if dialect == "mysql":
        with pytest.raises(VerificationUnavailable):
            apply_transaction_limits(session)
    else:
        apply_transaction_limits(session)
    assert session.statements == (
        ["SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '30s'"]
        if dialect == "postgresql" else []
    )


def test_recovery_limits_are_unchanged():
    from kalillac_accounts import recovery
    assert recovery.TIMEOUT_STATEMENTS == (
        "SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '30s'",
    )
