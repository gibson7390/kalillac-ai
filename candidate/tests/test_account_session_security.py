"""V1-01 production policy, route contracts and worker ownership; no providers."""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import threading
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

import kalillac_accounts.auth as auth
from kalillac_accounts.auth import AccountIdentity, AuthorizedAccount, RequestRejected
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_accounts.tokens import hash_session_token
from kalillac_accounts.v1_security import (FLAG, V1AccountSecurity, V1SecurityConfigError,
                                         V1SecurityUnavailable, load_v1_security)
from account_test_db import make_sqlite_engine, make_session_factory

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
USER = uuid.uuid4()
SESSION = uuid.uuid4()
TOKEN = "synthetic-v1-session-only"
DIGEST = hash_session_token(TOKEN)
IDENTITY = AccountIdentity(USER, SESSION, DIGEST)
ENV = {FLAG: "true", "KALILLAC_ACCOUNTS_ENABLED": "true", "KALILLAC_DB_ENABLED": "true"}


@pytest.fixture(autouse=True)
def off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-never-real")


def account_row(**changes):
    row = dict(id=USER, email="synthetic@example.test", created_at=NOW-timedelta(days=1),
               is_active=True, email_verified_at=NOW, quarantined_at=None,
               sessions_valid_after=None, session_created_at=NOW,
               expires_at=NOW+timedelta(days=1), revoked_at=None)
    row.update(changes)
    return SimpleNamespace(**row)


def test_original_identity_is_frozen_and_repr_has_no_credentials():
    assert (IDENTITY.user_id, IDENTITY.session_id, IDENTITY.token_hash) == (USER, SESSION, DIGEST)
    assert repr(IDENTITY) == "AccountIdentity()"
    with pytest.raises(FrozenInstanceError):
        IDENTITY.session_id = uuid.uuid4()


def test_identity_resolution_preserves_exact_database_binding(monkeypatch):
    seen = []
    monkeypatch.setattr(auth, "original_session_binding",
                        lambda s, d: seen.append((s, d)) or SimpleNamespace(user_id=USER, id=SESSION, token_hash=DIGEST))
    marker = object()
    assert auth.resolve_original_identity(marker, DIGEST) == IDENTITY
    assert seen == [(marker, DIGEST)]
    monkeypatch.setattr(auth, "original_session_binding", lambda s, d: None)
    assert auth.resolve_original_identity(marker, DIGEST) is None


@pytest.mark.parametrize("changes,status,code", [
    ({"revoked_at": NOW}, 401, "not_authenticated"),
    ({"expires_at": NOW}, 401, "not_authenticated"),
    ({"expires_at": NOW-timedelta(microseconds=1)}, 401, "not_authenticated"),
    ({"is_active": False}, 401, "not_authenticated"),
    ({"quarantined_at": NOW}, 403, "account_unavailable"),
    ({"email_verified_at": None}, 403, "email_verification_required"),
    ({"sessions_valid_after": NOW}, 401, "not_authenticated"),
    ({"sessions_valid_after": NOW+timedelta(microseconds=1)}, 401, "not_authenticated"),
])
def test_production_predicate_rejects_invalid_state(monkeypatch, changes, status, code):
    monkeypatch.setattr(auth, "locked_original_account", lambda *args: (account_row(**changes), NOW))
    with pytest.raises(RequestRejected) as rejected:
        auth.authorize_original_identity(object(), IDENTITY)
    assert (rejected.value.status, rejected.value.code) == (status, code)


@pytest.mark.parametrize("epoch", [None, NOW-timedelta(microseconds=1)])
def test_null_or_strictly_earlier_epoch_accepts_and_passes_original_triple(monkeypatch, epoch):
    seen = []
    def locked(*args):
        seen.append(args)
        return account_row(sessions_valid_after=epoch), NOW
    monkeypatch.setattr(auth, "locked_original_account", locked)
    marker = object()
    actual = auth.authorize_original_identity(marker, IDENTITY)
    assert actual == AuthorizedAccount(USER, "synthetic@example.test", NOW-timedelta(days=1), NOW, NOW)
    assert seen == [(marker, USER, SESSION, DIGEST)]


def test_missing_original_session_is_not_authenticated(monkeypatch):
    monkeypatch.setattr(auth, "locked_original_account", lambda *args: (None, NOW))
    with pytest.raises(RequestRejected) as rejected:
        auth.authorize_original_identity(object(), IDENTITY)
    assert rejected.value.status == 401


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", " FALSE "])
def test_off_mode_does_not_call_factory_or_require_schema(value):
    def forbidden():
        pytest.fail("off-mode schema/database access")
    assert load_v1_security(forbidden, {FLAG: value}) is None


@pytest.mark.parametrize("missing", ["KALILLAC_ACCOUNTS_ENABLED", "KALILLAC_DB_ENABLED"])
def test_on_requires_both_prerequisites_before_database_access(missing):
    env = dict(ENV);env.pop(missing)
    with pytest.raises(V1SecurityConfigError, match="requires accounts and database"):
        load_v1_security(lambda: pytest.fail("unexpected database access"), env)


def test_malformed_flag_refuses():
    with pytest.raises(V1SecurityConfigError, match="must be a boolean"):
        load_v1_security(environ={FLAG: "tru"})


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_on_validates_the_supplied_factory(monkeypatch, value):
    calls = []
    monkeypatch.setattr(V1AccountSecurity, "validate", lambda self: calls.append(self._factory))
    factory = lambda: None
    assert isinstance(load_v1_security(factory, {**ENV, FLAG: value}), V1AccountSecurity)
    assert calls == [factory]


@pytest.fixture
def legacy_db():
    # Deliberately pre-0011: no deferred provenance, quarantine or epoch columns.
    engine = make_sqlite_engine()
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE kalillac.users (id CHAR(32) PRIMARY KEY,email VARCHAR(320) NOT NULL,password_hash VARCHAR(255) NOT NULL,created_at DATETIME NOT NULL,updated_at DATETIME NOT NULL,is_active BOOLEAN NOT NULL,email_verified_at DATETIME)")
        c.exec_driver_sql("CREATE TABLE kalillac.account_sessions (id CHAR(32) PRIMARY KEY,user_id CHAR(32) NOT NULL,token_hash VARCHAR(64) NOT NULL,created_at DATETIME NOT NULL,expires_at DATETIME NOT NULL,revoked_at DATETIME)")
        c.exec_driver_sql("CREATE TABLE kalillac.account_entitlements (user_id CHAR(32) PRIMARY KEY,tier VARCHAR(16) NOT NULL,source VARCHAR(32) NOT NULL,created_at DATETIME NOT NULL,updated_at DATETIME NOT NULL,expires_at DATETIME)")
        c.execute(text("INSERT INTO kalillac.users VALUES (:id,'synthetic@example.test','synthetic-hash',:now,:now,true,NULL)"), {"id":USER.hex,"now":NOW})
        c.execute(text("INSERT INTO kalillac.account_sessions VALUES (:id,:uid,:digest,:now,:expiry,NULL)"), {"id":SESSION.hex,"uid":USER.hex,"digest":DIGEST,"now":NOW,"expiry":datetime(2099,1,1,tzinfo=timezone.utc)})
        c.execute(text("INSERT INTO kalillac.account_entitlements VALUES (:id,'free','registration',:now,:now,NULL)"), {"id":USER.hex,"now":NOW})
    yield make_session_factory(engine)
    engine.dispose()


def test_on_refuses_sqlite_and_sanitizes_configuration_failure(legacy_db):
    with pytest.raises(V1SecurityConfigError, match="PostgreSQL") as error:
        load_v1_security(lambda: legacy_db, ENV)
    assert "synthetic" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("path", ["me", "entitlements"])
@pytest.mark.parametrize("verification", [False, True])
def test_pre0011_off_mode_preserves_success_and_no_store(legacy_db, path, verification):
    api = FastAPI();api.include_router(build_account_router(lambda: legacy_db, AccountSettings(email_verification_enabled=verification)))
    with TestClient(api, base_url="https://testserver") as client:
        response = client.get("/api/account/"+path, headers={"Cookie":"__Host-kalillac_account="+TOKEN})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    if path == "me":
        assert response.json()["user"]["email"] == "synthetic@example.test"
        assert set(response.json()["user"]) == ({"id","email","created_at","email_verified"} if verification else {"id","email","created_at"})
        if verification:assert response.json()["user"]["email_verified"] is False
    else:assert response.json() == {"entitlements":{"tier":"free","saved_mode_access":False,"higher_usage_access":False,"persistent_memory_access":False}}


class RouteSecurity:
    def __init__(self, factory, error=None):
        self.factory, self.error, self.calls = factory, error, []

    async def read_async(self, token_hash, work):
        self.calls.append(token_hash)
        if self.error is not None:raise self.error
        with self.factory() as s, s.begin():
            return work(s, AuthorizedAccount(USER,"synthetic@example.test",NOW,NOW,NOW))


@pytest.mark.parametrize("path", ["me", "entitlements"])
@pytest.mark.parametrize("verification", [False, True])
def test_on_route_preserves_success_contract_and_passes_only_digest(legacy_db, path, verification):
    security = RouteSecurity(legacy_db)
    api = FastAPI();api.include_router(build_account_router(lambda: legacy_db, AccountSettings(email_verification_enabled=verification),v1_security=security))
    with TestClient(api,base_url="https://testserver") as client:
        response=client.get("/api/account/"+path,headers={"Cookie":"__Host-kalillac_account="+TOKEN})
    assert response.status_code==200 and response.headers["cache-control"]=="no-store"
    assert security.calls==[DIGEST] and TOKEN not in repr(security.calls)
    if path=="me":
        expected={"id":str(USER),"email":"synthetic@example.test","created_at":NOW.isoformat()}
        if verification:expected["email_verified"]=True
        assert response.json()=={"user":expected}
    else:assert response.json()=={"entitlements":{"tier":"free","saved_mode_access":False,"higher_usage_access":False,"persistent_memory_access":False}}


@pytest.mark.parametrize("path", ["me", "entitlements"])
@pytest.mark.parametrize("error,status,code", [
    (RequestRejected(401,"not_authenticated"),401,"not_authenticated"),
    (RequestRejected(403,"account_unavailable"),403,"account_unavailable"),
    (RequestRejected(403,"email_verification_required"),403,"email_verification_required"),
    (V1SecurityUnavailable(),503,"account_unavailable"),
])
def test_on_route_maps_errors_without_exposing_context(legacy_db,path,error,status,code):
    security=RouteSecurity(legacy_db,error);api=FastAPI();api.include_router(build_account_router(lambda:legacy_db,v1_security=security))
    with TestClient(api,base_url="https://testserver") as client:
        response=client.get("/api/account/"+path,headers={"Cookie":"__Host-kalillac_account="+TOKEN})
    assert (response.status_code,response.json())==(status,{"error":code})
    assert response.headers["cache-control"]=="no-store"


@pytest.mark.parametrize("path", ["me", "entitlements"])
def test_missing_cookie_never_calls_security(legacy_db,path):
    security=RouteSecurity(legacy_db);api=FastAPI();api.include_router(build_account_router(lambda:legacy_db,v1_security=security))
    with TestClient(api) as client:response=client.get("/api/account/"+path)
    assert response.status_code==401 and not security.calls


@pytest.mark.parametrize("v2",[False,True])
def test_only_two_reads_use_the_security_path(legacy_db,v2):
    security=RouteSecurity(legacy_db)
    registration=SimpleNamespace(request=None,confirm=None) if v2 else None
    api=FastAPI();api.include_router(build_account_router(lambda:legacy_db,v1_security=security,registration_http=registration))
    with TestClient(api) as client:
        assert client.post("/api/account/logout").json()=={"ok":True}
        assert client.post("/api/account/login",content=b"",headers={"Content-Type":"text/plain"}).status_code==415
        response=client.get("/api/account/registration-contract",headers={"X-Kalillac-Registration-Nonce":"a"*32})
    assert response.json()["contract"]==("email-first-v1" if v2 else "legacy-v1")
    assert not security.calls


def test_async_cancellation_waits_for_actual_worker_cleanup(monkeypatch):
    started,release,finished=threading.Event(),threading.Event(),threading.Event()
    stopped=[];security=V1AccountSecurity(lambda:None)
    def worker(token_hash,work,cancel):
        stopped.append(cancel);started.set()
        try:
            assert release.wait(5)
            assert cancel.is_set()
            raise V1SecurityUnavailable()
        finally:finished.set()
    monkeypatch.setattr(security,"read",worker)
    async def scenario():
        task=asyncio.create_task(security.read_async(DIGEST,lambda *args:None))
        while not started.is_set():await asyncio.sleep(0)
        task.cancel();await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
        task.cancel();await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        assert finished.is_set() and stopped[0].is_set()
    try:asyncio.run(scenario())
    finally:release.set()


def test_anonymous_chat_equivalence_with_security_router_present(monkeypatch,legacy_db):
    import app_fastapi_candidate as app
    monkeypatch.setattr(app,"chat",lambda *args,**kwargs:"synthetic anonymous reply")
    original=list(app.api.router.routes);responses=[]
    try:
        for enabled in [False,True]:
            app.api.router.routes[:]=original
            security=RouteSecurity(legacy_db) if enabled else None
            app.api.include_router(build_account_router(lambda:legacy_db,v1_security=security))
            with TestClient(app.api) as client:
                response=client.post("/api/chat?unchanged=1",json={"message":"hi","history":[]},headers={"Origin":"https://untrusted.invalid"})
            body=response.json();body.pop("session_id",None)
            responses.append((response.status_code,body,response.headers.get("cache-control")))
            if security:assert not security.calls
        assert responses[0]==responses[1] and responses[0][0]==200
    finally:app.api.router.routes[:]=original


def test_cancelled_before_worker_start_never_acquires_a_database():
    stopped=threading.Event();stopped.set()
    security=V1AccountSecurity(lambda:pytest.fail("cancelled worker acquired a database"))
    with pytest.raises(V1SecurityUnavailable):security.read(DIGEST,lambda *args:None,stopped)
