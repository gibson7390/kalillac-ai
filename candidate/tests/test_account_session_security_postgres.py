"""V1-01 real PostgreSQL locks/snapshots/cleanup with disposable R/M roles.

The existing registration harness refuses non-loopback/non-disposable targets.
CI sets its REQUIRE_POSTGRES switch, so absent PostgreSQL is a failure.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import threading
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.orm import sessionmaker

from kalillac_accounts.auth import (AccountIdentity, RequestRejected, authorize_original_identity,
                                    resolve_original_identity)
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_accounts.tokens import hash_session_token
from kalillac_accounts.v1_security import (FLAG,V1AccountSecurity,V1SecurityConfigError,
                                         V1SecurityUnavailable,load_v1_security,_prepare)
from kalillac_db.models import User,AccountSession
from kalillac_db.repositories.accounts import AccountAuthorizationUnavailable
from registration_test_support import postgres_world

ENV={FLAG:"true","KALILLAC_ACCOUNTS_ENABLED":"true","KALILLAC_DB_ENABLED":"true"}
TOKEN="synthetic-v1-postgres-session"
DIGEST=hash_session_token(TOKEN)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv(FLAG,raising=False)
    with postgres_world(monkeypatch,active=False) as w:
        w.uid,w.sid=uuid.uuid4(),uuid.uuid4()
        # Operator-only negative-fixture state: disable this immutable-field
        # guard solely to seed epochs/quarantine. Runtime authorization remains
        # actual R, never an owner; this does not test or grant mutation powers.
        with w.migration.begin() as c:
            c.exec_driver_sql("SET ROLE "+w.names["owner"])
            c.exec_driver_sql("ALTER TABLE kalillac.users DISABLE TRIGGER trg_users_registration_identity")
            w.now=c.scalar(text("SELECT clock_timestamp()"))
            c.execute(text("INSERT INTO kalillac.users(id,email,password_hash,is_active,email_verified_at) VALUES (:id,'synthetic-v1@example.test','synthetic-hash',true,:now)"),{"id":w.uid,"now":w.now})
            c.execute(text("INSERT INTO kalillac.account_entitlements(user_id,tier,source) VALUES (:id,'free','registration')"),{"id":w.uid})
        with w.runtime_factory.begin() as s:
            s.add(AccountSession(id=w.sid,user_id=w.uid,token_hash=DIGEST,created_at=w.now,expires_at=w.now+timedelta(days=1)))
        w.security=load_v1_security(lambda:w.runtime_factory,ENV)
        w.identity=AccountIdentity(w.uid,w.sid,DIGEST)
        yield w


@contextmanager
def owner(w):
    with w.migration.begin() as c:
        c.exec_driver_sql("SET ROLE "+w.names["owner"])
        yield c


def read(w):
    return w.security.read(DIGEST,lambda s,a:a)


class LockWatch:
    def __init__(self,w):
        self.w=w;self.entered=threading.Event();self.pid=None
    def before(self,connection,cursor,statement,parameters,context,many):
        if "FOR UPDATE" in statement and "kalillac.users" in statement:
            self.pid=connection.connection.driver_connection.info.backend_pid
            self.entered.set()
    def __enter__(self):
        event.listen(self.w.runtime_engine,"before_cursor_execute",self.before);return self
    def __exit__(self,*args):
        event.remove(self.w.runtime_engine,"before_cursor_execute",self.before)
    def waiting(self):
        assert self.entered.wait(5),"authorization never attempted the account lock"
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with self.w.migration.connect() as c:
                if c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"),{"pid":self.pid}):return
            threading.Event().wait(0.005)
        pytest.fail("no real ungranted PostgreSQL lock was observed")


def test_real_runtime_success_and_immutable_original_binding(world):
    w=world
    with w.runtime_factory() as s,s.begin():
        _prepare(s)
        assert resolve_original_identity(s,DIGEST)==w.identity
    result=read(w)
    assert result.id==w.uid and result.email=="synthetic-v1@example.test"
    assert result.authorized_at>=w.now
    assert w.runtime_engine.pool.checkedout()==0


@pytest.mark.parametrize("part",["user_id","session_id","token_hash"])
def test_exact_original_triple_cannot_be_rebound(world,part):
    w=world;forged=replace(w.identity,**{part:hash_session_token("other") if part=="token_hash" else uuid.uuid4()})
    with w.runtime_factory() as s,s.begin():
        _prepare(s)
        with pytest.raises(RequestRejected) as error:authorize_original_identity(s,forged)
    assert error.value.status==401


@pytest.mark.parametrize("state,status,code",[
    ("missing",401,"not_authenticated"),("revoked",401,"not_authenticated"),
    ("expired",401,"not_authenticated"),("inactive",401,"not_authenticated"),
    ("quarantined",403,"account_unavailable"),("unverified",403,"email_verification_required"),
    ("epoch_equal",401,"not_authenticated"),("epoch_after",401,"not_authenticated"),
])
def test_real_invalid_state_rejected(world,state,status,code):
    w=world
    changes={
        "missing":"DELETE FROM kalillac.account_sessions",
        "revoked":"UPDATE kalillac.account_sessions SET revoked_at=clock_timestamp()",
        "expired":"UPDATE kalillac.account_sessions SET expires_at=clock_timestamp()-interval '1 second'",
        "inactive":"UPDATE kalillac.users SET is_active=false",
        "quarantined":"UPDATE kalillac.users SET quarantined_at=clock_timestamp()",
        "unverified":"UPDATE kalillac.users SET email_verified_at=NULL",
        "epoch_equal":"UPDATE kalillac.users SET sessions_valid_after=(SELECT created_at FROM kalillac.account_sessions)",
        "epoch_after":"UPDATE kalillac.users SET sessions_valid_after=(SELECT created_at+interval '1 microsecond' FROM kalillac.account_sessions)",
    }
    with owner(w) as c:c.exec_driver_sql(changes[state])
    with pytest.raises(RequestRejected) as error:read(w)
    assert (error.value.status,error.value.code)==(status,code)
    assert w.runtime_engine.pool.checkedout()==0


@pytest.mark.parametrize("epoch",["NULL","(SELECT created_at-interval '1 microsecond' FROM kalillac.account_sessions)"])
def test_real_nullable_or_strictly_earlier_epoch_accepts(world,epoch):
    with owner(world) as c:c.exec_driver_sql("UPDATE kalillac.users SET sessions_valid_after="+epoch)
    assert read(world).id==world.uid


@pytest.mark.parametrize("change,status",[
    ("UPDATE kalillac.users SET is_active=false",401),
    ("UPDATE kalillac.users SET quarantined_at=clock_timestamp()",403),
    ("UPDATE kalillac.users SET email_verified_at=NULL",403),
    ("UPDATE kalillac.users SET sessions_valid_after=(SELECT created_at FROM kalillac.account_sessions)",401),
    ("UPDATE kalillac.account_sessions SET revoked_at=clock_timestamp()",401),
    ("DELETE FROM kalillac.account_sessions",401),
])
def test_account_and_session_changes_while_authorization_waits(world,change,status):
    w=world
    with ThreadPoolExecutor(1) as pool,LockWatch(w) as watch:
        with owner(w) as c:
            c.execute(text("SELECT id FROM kalillac.users WHERE id=:id FOR UPDATE"),{"id":w.uid})
            future=pool.submit(read,w)
            watch.waiting();assert not future.done()
            c.exec_driver_sql(change)
        with pytest.raises(RequestRejected) as error:future.result(6)
    assert error.value.status==status and w.runtime_engine.pool.checkedout()==0


def test_same_user_replacement_after_resolution_cannot_replace_original(world):
    w=world
    with ThreadPoolExecutor(1) as pool,LockWatch(w) as watch:
        with owner(w) as c:
            c.execute(text("SELECT id FROM kalillac.users WHERE id=:id FOR UPDATE"),{"id":w.uid})
            future=pool.submit(read,w);watch.waiting()
            c.execute(text("DELETE FROM kalillac.account_sessions WHERE id=:id"),{"id":w.sid})
            # Even reusing a stored digest cannot substitute another row ID.
            c.execute(text("INSERT INTO kalillac.account_sessions(id,user_id,token_hash,expires_at) VALUES (:id,:uid,:token,clock_timestamp()+interval '1 day')"),{"id":uuid.uuid4(),"uid":w.uid,"token":DIGEST})
        with pytest.raises(RequestRejected) as error:future.result(6)
    assert error.value.status==401


def test_time_is_fresh_after_a_proven_lock_wait(world):
    w=world
    with ThreadPoolExecutor(1) as pool,LockWatch(w) as watch:
        with owner(w) as c:
            c.execute(text("SELECT id FROM kalillac.users WHERE id=:id FOR UPDATE"),{"id":w.uid})
            future=pool.submit(read,w);watch.waiting()
            released_after=c.scalar(text("SELECT clock_timestamp()"))
        result=future.result(6)
    assert result.authorized_at>=released_after


def test_expiry_during_lock_wait_cannot_use_transaction_start_time(world):
    w=world
    with owner(w) as c:c.exec_driver_sql("UPDATE kalillac.account_sessions SET expires_at=clock_timestamp()+interval '1 second'")
    with ThreadPoolExecutor(1) as pool,LockWatch(w) as watch:
        with owner(w) as c:
            c.execute(text("SELECT id FROM kalillac.users WHERE id=:id FOR UPDATE"),{"id":w.uid})
            future=pool.submit(read,w);watch.waiting()
            deadline=time.monotonic()+2
            while not c.scalar(text("SELECT clock_timestamp()>=expires_at FROM kalillac.account_sessions")):
                assert time.monotonic()<deadline
                threading.Event().wait(0.01)
        with pytest.raises(RequestRejected) as error:future.result(6)
    assert error.value.status==401


def test_scalar_recheck_ignores_stale_orm_state(world):
    w=world
    with w.runtime_factory() as s,s.begin():
        _prepare(s);cached=s.get(User,w.uid);assert cached.is_active
        with owner(w) as c:c.exec_driver_sql("UPDATE kalillac.users SET is_active=false")
        assert cached.is_active
        with pytest.raises(RequestRejected):authorize_original_identity(s,w.identity)


@pytest.mark.parametrize("isolation",["REPEATABLE READ","SERIALIZABLE"])
def test_repeatable_read_snapshot_refused(world,isolation):
    w=world
    factory=sessionmaker(bind=w.runtime_engine.execution_options(isolation_level=isolation))
    with factory() as s,s.begin():
        assert s.get(User,w.uid).is_active
        with owner(w) as c:c.exec_driver_sql("UPDATE kalillac.users SET is_active=false")
        with pytest.raises(AccountAuthorizationUnavailable):authorize_original_identity(s,w.identity)


def test_fresh_transaction_can_qualify_a_repeatable_read_default(world):
    w=world;factory=sessionmaker(bind=w.runtime_engine.execution_options(isolation_level="REPEATABLE READ"))
    security=load_v1_security(lambda:factory,ENV)
    assert security.read(DIGEST,lambda s,a:s.scalar(text("SELECT current_setting('transaction_isolation')")))=="read committed"


def test_read_only_transaction_refused(world):
    w=world;factory=sessionmaker(bind=w.runtime_engine.execution_options(postgresql_readonly=True))
    with pytest.raises(V1SecurityConfigError):load_v1_security(lambda:factory,ENV)


@pytest.mark.parametrize("table,column",[("users","registration_intent_id"),("users","quarantined_at"),("users","sessions_valid_after"),("account_sessions","token_hash"),("account_sessions","created_at"),("account_sessions","expires_at")])
def test_missing_required_schema_refuses_startup(world,table,column):
    with owner(world) as c:c.exec_driver_sql("ALTER TABLE kalillac."+table+" DROP COLUMN "+column+" CASCADE")
    with pytest.raises(V1SecurityConfigError):load_v1_security(lambda:world.runtime_factory,ENV)
    assert world.runtime_engine.pool.checkedout()==0


def test_lock_timeout_rolls_back_and_releases_connection(world):
    w=world
    with ThreadPoolExecutor(1) as pool,LockWatch(w) as watch:
        with owner(w) as c:
            c.execute(text("SELECT id FROM kalillac.users WHERE id=:id FOR UPDATE"),{"id":w.uid})
            future=pool.submit(read,w);watch.waiting()
            with pytest.raises(V1SecurityUnavailable):future.result(6)
            assert w.runtime_engine.pool.checkedout()==0
    assert read(w).id==w.uid


def test_query_exception_rolls_back_and_releases_connection(world):
    w=world
    def fail(connection,cursor,statement,parameters,context,many):
        if "session_created_at" in statement:raise RuntimeError("synthetic read failure")
    event.listen(w.runtime_engine,"before_cursor_execute",fail)
    try:
        with pytest.raises(V1SecurityUnavailable):read(w)
    finally:event.remove(w.runtime_engine,"before_cursor_execute",fail)
    assert w.runtime_engine.pool.checkedout()==0 and read(w).id==w.uid


def test_async_cancellation_drains_real_locked_transaction(world):
    w=world;payload_calls=[]
    async def scenario():
        with LockWatch(w) as watch:
            with owner(w) as c:
                c.execute(text("SELECT id FROM kalillac.users WHERE id=:id FOR UPDATE"),{"id":w.uid})
                task=asyncio.create_task(w.security.read_async(DIGEST,lambda *args:payload_calls.append(True)))
                await asyncio.to_thread(watch.waiting)
                task.cancel();await asyncio.sleep(0)
                assert not task.done()
            with pytest.raises(asyncio.CancelledError):await task
        assert not payload_calls and w.runtime_engine.pool.checkedout()==0
    asyncio.run(scenario())
    assert read(w).id==w.uid


def test_existing_transaction_and_dirty_state_fail_without_flush(world):
    w=world
    with w.runtime_factory() as s:
        s.add(User(email="never-persist@example.test",password_hash="synthetic"))
        with pytest.raises(V1SecurityUnavailable):V1AccountSecurity(lambda:lambda:s).read(DIGEST,lambda *args:None)
    with w.runtime_factory() as s:
        assert s.scalar(text("SELECT count(*) FROM kalillac.users"))==1


@pytest.mark.parametrize("path",["me","entitlements"])
@pytest.mark.parametrize("verification",[False,True])
def test_real_route_success_and_no_database_mutation(world,path,verification):
    w=world;statements=[]
    def record(connection,cursor,statement,parameters,context,many):statements.append(statement.strip().split()[0].upper())
    with w.runtime_factory() as s:
        before=s.execute(text("SELECT row_to_json(u) FROM kalillac.users u")).all(),s.execute(text("SELECT row_to_json(a) FROM kalillac.account_sessions a")).all()
    api=FastAPI();api.include_router(build_account_router(lambda:w.runtime_factory,AccountSettings(email_verification_enabled=verification),v1_security=w.security))
    event.listen(w.runtime_engine,"before_cursor_execute",record)
    try:
        with TestClient(api,base_url="https://testserver") as client:response=client.get("/api/account/"+path,headers={"Cookie":"__Host-kalillac_account="+TOKEN})
    finally:event.remove(w.runtime_engine,"before_cursor_execute",record)
    assert response.status_code==200 and response.headers["cache-control"]=="no-store"
    assert set(statements)<={"SET","SELECT"}
    if path=="me":
        assert response.json()["user"]["id"]==str(w.uid)
        assert set(response.json()["user"])==({"id","email","created_at","email_verified"} if verification else {"id","email","created_at"})
    else:assert response.json()=={"entitlements":{"tier":"free","saved_mode_access":False,"higher_usage_access":False,"persistent_memory_access":False}}
    with w.runtime_factory() as s:
        after=s.execute(text("SELECT row_to_json(u) FROM kalillac.users u")).all(),s.execute(text("SELECT row_to_json(a) FROM kalillac.account_sessions a")).all()
        assert s.scalar(text("SHOW lock_timeout"))=="0"
    assert before==after and w.runtime_engine.pool.checkedout()==0


def test_active_registration_v2_authority_remains_compatible(monkeypatch):
    monkeypatch.delenv(FLAG,raising=False)
    with postgres_world(monkeypatch,active=True) as w:
        w.authority.issue_after_response("v1-compatible@example.test")
        w.authority.confirm_after_hash(w.authority.prevalidate(w.token()),"synthetic-hash")
        with w.runtime_factory.begin() as s:
            uid=s.scalar(select(User.id));now=s.scalar(text("SELECT clock_timestamp()"))
            s.add(AccountSession(user_id=uid,token_hash=DIGEST,created_at=now,expires_at=now+timedelta(days=1)))
        security=load_v1_security(lambda:w.runtime_factory,ENV)
        assert security.read(DIGEST,lambda s,a:a.id)==uid


@pytest.mark.parametrize("phase",["startup","read","direct"])
def test_dbapi_autocommit_cannot_fake_a_locking_transaction(world,phase):
    w=world;factory=sessionmaker(bind=w.runtime_engine.execution_options(isolation_level="AUTOCOMMIT"))
    if phase=="startup":
        with pytest.raises(V1SecurityConfigError):load_v1_security(lambda:factory,ENV)
    elif phase=="read":
        with pytest.raises(V1SecurityUnavailable):V1AccountSecurity(lambda:factory).read(DIGEST,lambda *args:None)
    else:
        with factory() as s,s.begin():
            with pytest.raises(AccountAuthorizationUnavailable):authorize_original_identity(s,w.identity)
    assert w.runtime_engine.pool.checkedout()==0
