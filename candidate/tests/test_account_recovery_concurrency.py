"""Real separate SQLite connections and transaction rollback/locking contracts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql

from account_test_db import make_session_factory, make_sqlite_file_engine
from recovery_test_support import ENV, NOW, NEW_PASSWORD, FakeEmail, add_sessions, add_user, rows
from kalillac_accounts.passwords import hash_password, verify_password
from kalillac_accounts.recovery import RecoveryService, apply_transaction_limits
from kalillac_accounts.recovery_config import load_recovery_config
from kalillac_accounts.recovery_tokens import hash_recovery_token, new_recovery_token
from kalillac_db.models import AccountRecoveryToken, AccountSession, Base, User
from kalillac_db.repositories import recovery as repository


@pytest.fixture
def db(tmp_path):
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


def seed(factory, user_id):
    token = new_recovery_token()
    with factory() as session, session.begin():
        session.add(AccountRecoveryToken(
            user_id=user_id, purpose="password_reset", token_hash=hash_recovery_token(token),
            created_at=NOW, expires_at=NOW + timedelta(minutes=30),
        ))
    return token


@pytest.mark.parametrize("different_tokens", [False, True])
def test_parallel_confirmations_have_exactly_one_success(db, different_tokens):
    user_id = add_user(db)
    add_sessions(db, user_id)
    first = seed(db, user_id)
    second = seed(db, user_id) if different_tokens else first
    gateway = FakeEmail()
    service = RecoveryService(lambda: db, gateway, load_recovery_config(gateway, ENV), lambda: NOW)
    password_hashes = [hash_password(NEW_PASSWORD + str(i)) for i in range(2)]
    barrier = threading.Barrier(2)
    def confirm(index):
        barrier.wait(timeout=10)
        try:
            service.confirm_reset([first, second][index], password_hashes[index])
            return index
        except repository.InvalidRecoveryToken:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm, i) for i in range(2)]
        results = [future.result(timeout=15) for future in futures]
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    with db() as session:
        assert session.get(User, user_id).password_hash == password_hashes[winners[0]]
    assert all(row["consumed_at"] is not None for row in rows(db, AccountRecoveryToken))
    assert all(row["revoked_at"] is not None for row in rows(db, AccountSession))
    assert len(gateway.changed) == 1


@pytest.mark.parametrize("stage", ["after_password", "after_revocation", "commit"])
def test_failed_reset_rolls_back_password_tokens_and_sessions(db, monkeypatch, stage):
    user_id = add_user(db)
    add_sessions(db, user_id)
    token = seed(db, user_id)
    gateway = FakeEmail()
    service = RecoveryService(lambda: db, gateway, load_recovery_config(gateway, ENV), lambda: NOW)
    before = {model: rows(db, model) for model in (User, AccountSession, AccountRecoveryToken)}
    def fail(*args, **kwargs):
        raise RuntimeError("private database detail")
    if stage == "after_password":
        monkeypatch.setattr(repository, "invalidate_unused", fail)
    elif stage == "after_revocation":
        real = repository.revoke_all_account_sessions
        def revoked(*args):
            real(*args)
            fail()
        monkeypatch.setattr(repository, "revoke_all_account_sessions", revoked)
    else:
        @event.listens_for(db.class_, "before_commit", once=True)
        def failed_commit(session):
            fail()
    with pytest.raises(RuntimeError):
        service.confirm_reset(token, hash_password(NEW_PASSWORD))
    for model, original in before.items():
        assert rows(db, model) == original
    assert gateway.changed == []


def test_confirmation_locks_user_and_checks_expiry_after_lock(db, monkeypatch):
    user_id = add_user(db)
    token = seed(db, user_id)
    clock = [NOW]
    real = repository.lock_user
    def delayed_lock(session, target):
        user = real(session, target)
        clock[0] = NOW + timedelta(minutes=30)
        return user
    monkeypatch.setattr(repository, "lock_user", delayed_lock)
    gateway = FakeEmail()
    service = RecoveryService(lambda: db, gateway, load_recovery_config(gateway, ENV), lambda: clock[0])
    before = rows(db, User)
    with pytest.raises(repository.InvalidRecoveryToken):
        service.confirm_reset(token, hash_password(NEW_PASSWORD))
    assert rows(db, User) == before
    assert rows(db, AccountRecoveryToken)[0]["consumed_at"] is None


def test_lock_statement_uses_postgresql_for_update(db):
    user_id = add_user(db)
    statements = []
    with db() as session, session.begin():
        real = session.scalar
        def record(statement):
            statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return real(statement)
        session.scalar = record
        repository.lock_user(session, user_id)
    assert len(statements) == 1 and statements[0].endswith("FOR UPDATE")
    assert "kalillac.users" in statements[0]


@pytest.mark.parametrize("dialect", ["postgresql", "sqlite", "mysql"])
def test_fixed_transaction_timeouts_and_explicit_test_exception(dialect):
    from types import SimpleNamespace
    from kalillac_accounts.recovery import RecoveryUnavailable
    class Session:
        def __init__(self):
            self.statements = []
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        def execute(self, statement):
            self.statements.append(str(statement))
    session = Session()
    if dialect == "mysql":
        with pytest.raises(RecoveryUnavailable):
            apply_transaction_limits(session)
    else:
        apply_transaction_limits(session)
    assert session.statements == (
        ["SET LOCAL lock_timeout = '5s'", "SET LOCAL statement_timeout = '30s'"]
        if dialect == "postgresql" else []
    )


@pytest.mark.parametrize("change", ["password", "inactive"])
def test_login_rechecks_identity_under_the_account_lock(db, monkeypatch, change):
    """A reset committed while login ran Argon2 must not yield a surviving session."""
    import kalillac_accounts.router as router
    from recovery_test_support import PASSWORD, client_for
    from sqlalchemy import update
    user_id = add_user(db)
    replaced = hash_password(NEW_PASSWORD)
    real = router.lock_account
    def reset_while_verifying(session, locked_id):
        values = {"password_hash": replaced} if change == "password" else {"is_active": False}
        session.execute(update(User).where(User.id == locked_id).values(**values))
        return real(session, locked_id)
    monkeypatch.setattr(router, "lock_account", reset_while_verifying)
    with client_for(db, FakeEmail()) as client:
        response = client.post("/api/account/login", json={"email": "person@example.com", "password": PASSWORD})
    assert response.status_code == 401 and response.json() == {"error": "invalid_credentials"}
    assert "set-cookie" not in response.headers
    assert rows(db, AccountSession) == []


def test_login_locks_the_account_after_verifying_and_before_creating_a_session(db, monkeypatch):
    import kalillac_accounts.router as router
    from recovery_test_support import PASSWORD, client_for
    user_id = add_user(db)
    order = []
    real_verify, real_lock, real_create = router.verify_password, router.lock_account, router.create_account_session
    monkeypatch.setattr(router, "verify_password", lambda *a: order.append("verify") or real_verify(*a))
    monkeypatch.setattr(router, "lock_account", lambda s, u: order.append(("lock", u)) or real_lock(s, u))
    monkeypatch.setattr(router, "create_account_session", lambda *a: order.append("session") or real_create(*a))
    with client_for(db, FakeEmail()) as client:
        response = client.post("/api/account/login", json={"email": "person@example.com", "password": PASSWORD})
    assert response.status_code == 200
    assert order == ["verify", ("lock", user_id), "session"]
    assert len(rows(db, AccountSession)) == 1
