"""R7/AR8 configuration, disabled-mode, guard, capacity and proof-work obligations."""
from __future__ import annotations
import asyncio,base64,json,os,threading,uuid
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from kalillac_accounts.registration_crypto import *
from kalillac_accounts.registration_startup import _check_credential,schema_probe
from kalillac_accounts.registration_limits import RegistrationLimits
from kalillac_accounts.registration_http import RegistrationHTTP
from kalillac_accounts.registration_dispatch import RegistrationDispatcher
from kalillac_accounts.router import build_account_router,AccountSettings
from kalillac_accounts.request_guard import UnsafeRequestGuard
from kalillac_accounts.password_work import PasswordWorkGate
from kalillac_db.models import AccountRegistrationIntent as Intent,User,AccountEntitlement
from registration_test_support import sqlite_world,key_bytes

@pytest.fixture
def world():
    w=sqlite_world();yield w;w.engine.dispose()

def test_unactivated_off_probe_does_not_open_any_protected_file(world,monkeypatch):
    import kalillac_accounts.registration_startup as startup
    monkeypatch.setattr(startup,"read_protected",lambda *a:pytest.fail("disabled secret read"))
    assert startup.bootstrap(world.factory,None,{}) is None
    with world.factory.begin() as s:s.execute(__import__("sqlalchemy").text("UPDATE kalillac.account_registration_activation SET state='active_zero_legacy',activation_id=:a,cutover_id=:c,deployment_id='dev-test',evidence_sha256=:e,inventory_at=CURRENT_TIMESTAMP,activated_at=CURRENT_TIMESTAMP,legacy_unverified_count=0"),{"a":uuid.uuid4().hex,"c":uuid.uuid4().hex,"e":"0"*64})
    with pytest.raises(RuntimeError):startup.bootstrap(world.factory,None,{})

@pytest.mark.parametrize("field,value",[
    ("format_version",True),("role","ordinary"),("database_instance_id","bad"),("deployment_id","other"),
    ("password","bad"),("password","+"*43),("extra",1),
])
def test_credential_exact_shape_and_bindings(world,field,value):
    instance=str(uuid.uuid4());password=base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    c={"format_version":1,"role":"registration","database_instance_id":instance,"deployment_id":"dev-test","password":password}
    assert _check_credential(json.dumps(c).encode(),"registration",instance,"dev-test","ordinary-password",world.ring)==password
    c[field]=value
    with pytest.raises((RegistrationUnavailable,ValueError)):_check_credential(json.dumps(c).encode(),"registration",instance,"dev-test","ordinary-password",world.ring)

def test_credential_reuse_actual_string_and_decoded_bytes(world):
    instance=str(uuid.uuid4());raw=next(iter(world.ring.encryption_keys.values()));password=base64.urlsafe_b64encode(raw).decode().rstrip("=")
    c={"format_version":1,"role":"registration","database_instance_id":instance,"deployment_id":"dev-test","password":password}
    with pytest.raises(RegistrationUnavailable):_check_credential(json.dumps(c).encode(),"registration",instance,"dev-test",None,world.ring)
    password=base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=");c["password"]=password
    with pytest.raises(RegistrationUnavailable):_check_credential(json.dumps(c).encode(),"registration",instance,"dev-test",password,world.ring)
    with pytest.raises(RegistrationUnavailable):_check_credential(json.dumps(c).encode(),"registration",instance,"dev-test",password+"=",world.ring)


    from contextlib import contextmanager
    from types import SimpleNamespace
    from sqlalchemy import URL
    from kalillac_accounts.registration_startup import _ordinary_auth,migration_app_role
    runtime=migration_app_role()
    ordinary=URL.create("postgresql+psycopg",username=runtime,password="synthetic-resolved",host="127.0.0.1",database="disposable")
    @contextmanager
    def factory(row):
        yield SimpleNamespace(execute=lambda statement:SimpleNamespace(one=lambda:row))
    assert _ordinary_auth(lambda:factory((runtime,runtime,"scram-sha-256:"+runtime)),ordinary)=="synthetic-resolved"
    for row,url in [((runtime,runtime,None),ordinary),((runtime,runtime,"scram-sha-256:"+runtime),ordinary.set(password=None)),(("wrong","wrong","scram-sha-256:wrong"),ordinary)]:
        # URL.set(password=None) retains the old value; construct the genuinely
        # unresolved password-backed configuration explicitly.
        if row[2]=="scram-sha-256:"+runtime:url=URL.create("postgresql+psycopg",username=runtime,host="127.0.0.1",database="disposable")
        with pytest.raises(RegistrationUnavailable):_ordinary_auth(lambda:factory(row),url)
    assert _ordinary_auth(lambda:factory((runtime,runtime,"cert:synthetic-principal")),ordinary) is None

def test_keyring_immutable_versions_and_eight_version_bound():
    obj=json.loads(key_bytes())
    for i in range(2,9):obj["lookup_keys"].append({"version":"reg-lookup-v"+str(i),"key_b64":base64.b64encode(os.urandom(32)).decode()})
    ring=Keyring.parse(json.dumps(obj).encode())
    with pytest.raises(TypeError):ring.lookup_keys["reg-lookup-v1"]=b"x"*32
    obj["lookup_keys"].append({"version":"reg-lookup-v9","key_b64":base64.b64encode(os.urandom(32)).decode()})
    with pytest.raises(RegistrationUnavailable):Keyring.parse(json.dumps(obj).encode())

def app_for(w,gate=None,dispatch=None):
    class Dispatch:
        def __init__(self):self.jobs=[]
        def submit(self,email):self.jobs.append(email)
    dispatch=dispatch or Dispatch()
    http=RegistrationHTTP(w.authority,dispatch,AccountSettings(cookie_secure=False),gate)
    app=FastAPI();app.include_router(build_account_router(session_factory=lambda:w.factory,registration_http=http))
    app.add_middleware(UnsafeRequestGuard,origin="https://registration.invalid")
    return app,dispatch

def test_final_body_release_precedes_account_work(world):
    w=world;events=[]
    class Dispatcher:
        def submit(self,email):events.append("job")
    app,_=app_for(w,dispatch=Dispatcher())
    body=json.dumps({"email":"synthetic@example.test"}).encode()
    scope={"type":"http","http_version":"1.1","method":"POST","path":"/api/account/register","raw_path":b"/api/account/register",
        "query_string":b"","scheme":"https","server":("registration.invalid",443),"client":("127.0.0.1",10),
        "headers":[(b"origin",b"https://registration.invalid"),(b"content-type",b"application/json"),(b"content-length",str(len(body)).encode())]}
    async def receive():return {"type":"http.request","body":body,"more_body":False}
    async def send(message):
        if message["type"]=="http.response.body" and not message.get("more_body",False):
            assert events==[];events.append("body")
    asyncio.run(app(scope,receive,send))
    assert events==["body","job"]
    assert not w.gateway.links

@pytest.mark.parametrize("path",["/api/account/register","/api/account/register/confirm"])
def test_query_and_origin_refuse_before_body_or_budget(world,path):
    app,_=app_for(world)
    with TestClient(app) as client:
        assert client.post(path+"?x=1",content=b"not-json").status_code==400
        assert client.post(path,content=b"not-json").status_code==403
    assert not world.limits._request and not world.limits._confirm

def test_confirmation_password_precedence_and_no_invalid_hash(world):
    import kalillac_accounts.registration_http as http
    app,_=app_for(world)
    original=http.hash_password
    def forbidden(*a):pytest.fail("unproved hash")
    http.hash_password=forbidden
    try:
        with TestClient(app) as client:
            headers={"Origin":"https://registration.invalid"}
            for password in [None,False,4]:
                r=client.post("/api/account/register/confirm",json={"token":"bad","password":password},headers=headers)
                assert r.status_code==422 and r.json()["error"]=="invalid_password"
            assert not world.limits._confirm
            r=client.post("/api/account/register/confirm",json={"token":"bad","password":"long synthetic password"},headers=headers)
            assert r.status_code==400 and len(world.limits._confirm)==1
    finally:http.hash_password=original

def test_shared_gate_reservation_charge_at_start_and_cancellation(world):
    w=world;w.authority.issue_after_response("synthetic@example.test")
    proof=w.authority.prevalidate(w.token());pairs=w.ring.pairs(proof.email)
    decision,reservation=w.limits.address(pairs,"work");assert decision.allowed
    started=threading.Event();release=threading.Event()
    def worker():started.set();assert release.wait(10)
    async def scenario():
        gate=PasswordWorkGate()
        task=asyncio.create_task(gate.run(worker,on_start=reservation.charge))
        await asyncio.to_thread(started.wait,5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await task
        reservation.release()
        await gate.acquire();gate.release()
    asyncio.run(scenario())
    assert next(iter(w.limits._buckets.values())).events["work"]
    with w.factory() as s:assert s.scalar(select(func.count()).select_from(User))==0

def test_dispatch_total_capacity_and_post_shutdown_refusal(world):
    dispatcher=RegistrationDispatcher(world.authority,capacity=2,workers=0)
    dispatcher.submit("synthetic@example.test");dispatcher.submit("other@example.test")
    dispatcher.submit("overflow@example.test")
    assert dispatcher._queue.qsize()==2
    dispatcher.close();dispatcher.submit("closed@example.test")
    assert dispatcher._queue.empty()

def test_guarded_user_failure_never_becomes_unique_conflict(world):
    # A forged/foreign private context fails before any user mutation.
    w=world;w.authority.issue_after_response("synthetic@example.test")
    proof=w.authority.prevalidate(w.token())
    with pytest.raises(RegistrationUnavailable):w.authority.confirm_after_hash(replace(proof,owner=object()),"hash")

def test_sqlite_inline_reference_really_nulls_only_provenance(world):
    w=world;w.authority.issue_after_response("synthetic@example.test")
    w.authority.confirm_after_hash(w.authority.prevalidate(w.token()),"hash")
    w.time[0]+=__import__("datetime").timedelta(hours=25)
    w.authority.cleanup_expired()
    with w.factory() as s:
        user=s.scalar(select(User));assert user.registration_intent_id is None
        assert s.scalar(select(func.count()).select_from(AccountEntitlement))==1

@pytest.mark.parametrize("field",["email","user_id","intent_id","digest","role"])
def test_confirm_extra_identity_fields_rejected(world,field):
    app,_=app_for(world)
    with TestClient(app) as client:
        r=client.post("/api/account/register/confirm",json={"token":new_token(),"password":"long synthetic password",field:"caller"},headers={"Origin":"https://registration.invalid"})
        assert r.status_code==400 and r.json()["error"]=="invalid_body"

def test_crypto_failure_does_not_hash_or_reserve_address(world,monkeypatch):
    w=world;w.authority.issue_after_response("synthetic@example.test")
    def corrupt(row):raise RegistrationUnavailable()
    monkeypatch.setattr(type(w.ring),"decrypt",lambda *a:corrupt(None))
    app,_=app_for(w)
    with TestClient(app) as client:
        r=client.post("/api/account/register/confirm",json={"token":w.token(),"password":"long synthetic password"},headers={"Origin":"https://registration.invalid"})
        assert r.status_code==503 and "retry-after" not in r.headers
    assert not next(iter(w.limits._buckets.values())).events["work"]

def test_dispatch_deadline_supervision_retains_blocked_worker_and_latches(world):
    from kalillac_accounts.registration_markers import IssuanceEntry
    w=world;started=threading.Event();release=threading.Event();cancelled=[]
    entry=IssuanceEntry("a"*64,uuid.uuid4(),uuid.uuid4(),1,None,None,(),0.)
    w.markers.add(entry);w.markers.uncertain(entry)
    def reconcile(entry,deadline=None):
        started.set();assert release.wait(10)
    w.authority.reconcile=reconcile
    w.authority.cancel_operation=lambda key:cancelled.append(key)
    w.authority.cancel_all=lambda:cancelled.append("all")
    dispatcher=RegistrationDispatcher(w.authority,workers=0)
    try:
        dispatcher._tick();assert started.wait(5)
        worker=dispatcher._io_thread
        w.mono[0]=6;dispatcher._tick()
        assert cancelled==[("reconcile",entry.intent_id)]
        dispatcher._tick()
        assert dispatcher._io_thread is worker and worker.is_alive()
        w.mono[0]=60;dispatcher._tick()
        assert cancelled[-1]=="all" and not w.markers.accepting()
        dispatcher._tick();assert cancelled.count("all")==1
    finally:
        release.set()
        if dispatcher._io_thread:dispatcher._io_thread.join(10)
        dispatcher.close()

def test_marker_clock_rollback_cannot_extend_deadline():
    from kalillac_accounts.registration_markers import IssuanceMap,IssuanceEntry
    now=[0.];markers=IssuanceMap(clock=lambda:now[0])
    markers.add(IssuanceEntry("a"*64,uuid.uuid4(),uuid.uuid4(),1,None,None,(),0.))
    now[0]=31;assert markers.snapshot()[0].state=="uncertain"
    now[0]=1;assert markers.snapshot()[0].state=="uncertain"
    now[0]=60;assert not markers.accepting()

@pytest.mark.parametrize("path",["/api/account/register","/api/account/register/confirm"])
@pytest.mark.parametrize("kind",["duplicate","nonfinite","array","oversize","wrongtype"])
def test_v2_strict_json_and_body_limits_before_accounting(world,path,kind):
    app,_=app_for(world)
    raw={
        "duplicate":b'{"email":"synthetic@example.test","email":"other@example.test"}',
        "nonfinite":b'{"email":NaN}',
        "array":b'[]',
        "oversize":b" "*4097,
        "wrongtype":b'{"email":true}',
    }[kind]
    with TestClient(app) as client:
        result=client.post(path,content=raw,headers={"Origin":"https://registration.invalid","Content-Type":"application/json"})
    assert result.status_code in (400,413,422)
    assert not world.limits._request and not world.limits._confirm

def test_v2_unicode_body_boundary_and_duplicate_origin(world):
    app,_=app_for(world)
    raw=json.dumps({"email":"synthetic@example.test"}).encode()
    with TestClient(app) as client:
        exact=client.post("/api/account/register",content=raw+b" "*(4096-len(raw)),headers={"Origin":"https://registration.invalid","Content-Type":"application/json"})
        assert exact.status_code==202
        duplicate=client.post("/api/account/register",json={"email":"synthetic@example.test"},headers=[("Origin","https://registration.invalid"),("Origin","https://registration.invalid")])
        assert duplicate.status_code==403
    assert len(next(iter(world.limits._request.values())))==1

@pytest.mark.parametrize("state",["unknown","marker","latch","database"])
def test_retryable_miss_headers_and_no_password_or_address_work(world,monkeypatch,state):
    from kalillac_accounts.registration_markers import IssuanceEntry
    w=world;token=new_token()
    if state=="marker":w.markers.add(IssuanceEntry(token_digest(token),uuid.uuid4(),uuid.uuid4(),1,None,None,(),0.))
    if state=="latch":w.markers._latched=True
    if state=="database":
        def fail(*a):raise RegistrationUnavailable(retry=True)
        monkeypatch.setattr(w.authority,"_read",fail)
    import kalillac_accounts.registration_http as http
    monkeypatch.setattr(http,"hash_password",lambda *a:pytest.fail("miss must not hash"))
    app,_=app_for(w)
    with TestClient(app) as client:
        result=client.post("/api/account/register/confirm",json={"token":token,"password":"valid synthetic password"},headers={"Origin":"https://registration.invalid"})
    assert result.status_code==(400 if state=="unknown" else 503)
    assert result.headers["cache-control"]=="no-store" and result.headers["content-type"]=="application/json"
    assert "set-cookie" not in result.headers
    if state!="unknown":assert result.headers["retry-after"]=="1"
    assert not w.limits._buckets and not w.gateway.links
    assert sum(len(events) for events in w.limits._confirm.values())==1

def test_registration_transactions_end_before_gate_and_hash(world,monkeypatch):
    w=world;w.authority.issue_after_response("synthetic@example.test")
    from sqlalchemy import event
    depth=[0];calls=[]
    event.listen(w.engine,"begin",lambda connection:depth.__setitem__(0,depth[0]+1))
    event.listen(w.engine,"commit",lambda connection:depth.__setitem__(0,depth[0]-1))
    event.listen(w.engine,"rollback",lambda connection:depth.__setitem__(0,depth[0]-1))
    import kalillac_accounts.registration_http as http
    def hash_only(password):
        assert depth[0]==0;calls.append("hash");return "synthetic-hash"
    class Gate:
        async def run(self,function,*args,on_start=None):
            assert depth[0]==0
            if on_start:on_start()
            return function(*args)
    monkeypatch.setattr(http,"hash_password",hash_only)
    app,_=app_for(w,gate=Gate())
    with TestClient(app) as client:
        result=client.post("/api/account/register/confirm",json={"token":w.token(),"password":"valid synthetic password"},headers={"Origin":"https://registration.invalid"})
    assert result.status_code==200 and "set-cookie" not in result.headers and calls==["hash"]
    assert depth[0]==0

@pytest.mark.parametrize("state",["unverified","quarantined","inactive","verified"])
def test_v2_login_retains_uniform_password_work_and_rejects_unproved_accounts(world,monkeypatch,state):
    from datetime import datetime,timezone
    w=world;now=datetime.now(timezone.utc)
    with w.factory.begin() as session:
        session.add(User(email="synthetic@example.test",password_hash="synthetic-hash",email_verified_at=None if state=="unverified" else now,
            quarantined_at=now if state=="quarantined" else None,is_active=state!="inactive"))
    import kalillac_accounts.router as router
    calls=[]
    monkeypatch.setattr(router,"verify_password",lambda *a:calls.append("verify") or True)
    monkeypatch.setattr(router,"needs_rehash",lambda *a:False)
    app,_=app_for(w)
    with TestClient(app) as client:
        result=client.post("/api/account/login",json={"email":"synthetic@example.test","password":"valid synthetic password"},headers={"Origin":"https://registration.invalid"})
    assert result.status_code==(200 if state=="verified" else 401) and calls==["verify"]
    if state!="verified":assert result.json()=={"error":"invalid_credentials"} and "set-cookie" not in result.headers

def test_no_secret_payload_in_failure_status_or_repr(world,capsys):
    w=world;w.authority.issue_after_response("synthetic@example.test")
    token=w.token();proof=w.authority.prevalidate(token)
    with w.factory() as session:
        row=session.scalar(select(Intent));ciphertext=bytes(row.email_ciphertext);nonce=bytes(row.email_nonce)
    def fail():raise RuntimeError("synthetic@example.test "+token)
    w.gateway.before_send=fail;w.authority.issue_after_response("synthetic@example.test")
    output=capsys.readouterr().out+repr(w.authority)+repr(proof)+repr(w.ring)+str(RegistrationUnavailable())
    canaries=["synthetic@example.test",token,ciphertext.hex(),nonce.hex()]
    canaries += [base64.b64encode(k).decode() for k in (*w.ring.encryption_keys.values(),*w.ring.lookup_keys.values())]
    assert not any(value in output for value in canaries)

def test_whole_application_v2_route_inventory_is_guarded():
    import subprocess,sys
    from pathlib import Path
    code="""
import os,sys
sys.path.insert(0,'tests')
from registration_test_support import sqlite_world
w=sqlite_world()
os.environ.update(KALILLAC_DB_ENABLED='true',KALILLAC_ACCOUNTS_ENABLED='true',KALILLAC_ACCOUNT_REGISTRATION_V2_ENABLED='true',KALILLAC_SITE_ORIGIN='https://registration.invalid')
import kalillac_db.engine as engine
engine.get_session_factory=lambda:w.factory
import kalillac_accounts.email_provider as email
email.build_email_gateway=lambda:w.gateway
import kalillac_accounts.registration_startup as startup
# Explicit synthetic application assembly; production bootstrap is tested
# separately with real protected Linux descriptors and SCRAM connections.
w.authority._dispose=w.engine.dispose
startup.bootstrap=lambda *args,**kwargs:w.authority
import app_fastapi_candidate as app
from fastapi.testclient import TestClient
from kalillac_accounts.request_guard import guarded_route
with TestClient(app.api) as client:
    for path in ('/api/account/register','/api/account/register/confirm'):
        response=client.post(path,content=b'{}')
        assert response.status_code==403
        assert guarded_route('POST',path) is not None
w.engine.dispose()
print('WHOLE_APPLICATION_V2_GUARD_OK')
"""
    env={k:v for k,v in os.environ.items() if not k.startswith(("KALILLAC_","OPENAI_","GROQ_","TAVILY_","RESEND_","STRIPE_"))}
    result=subprocess.run([sys.executable,"-B","-c",code],cwd=Path(__file__).resolve().parents[1],env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode==0 and "WHOLE_APPLICATION_V2_GUARD_OK" in result.stdout,"whole-application V2 guard inventory failed"

@pytest.mark.parametrize("v2",[False,True])
def test_authoritative_contract_is_mounted_handler_not_environment(world,monkeypatch,v2):
    monkeypatch.setenv("KALILLAC_ACCOUNT_REGISTRATION_V2_ENABLED","false" if v2 else "true")
    app=FastAPI()
    app.include_router(build_account_router(session_factory=lambda:pytest.fail("contract accessed DB"),
        registration_http=object() if v2 else None))
    with TestClient(app) as client:
        for nonce in ("0"*32,"f"*32):
            response=client.get("/api/account/registration-contract",headers={"X-Kalillac-Registration-Nonce":nonce})
            assert response.status_code==200
            assert response.json()=={"contract":"email-first-v1" if v2 else "legacy-v1","nonce":nonce}
            assert response.headers["cache-control"]=="no-store"
        for headers in ({},{"X-Kalillac-Registration-Nonce":"bad"},
            [("x-kalillac-registration-nonce","0"*32),("X-Kalillac-Registration-Nonce","f"*32)]):
            assert client.get("/api/account/registration-contract",headers=headers).status_code==400
        assert client.get("/api/account/registration-contract?x=1",headers={"X-Kalillac-Registration-Nonce":"0"*32}).status_code==400

@pytest.mark.parametrize("failure",["missing","construct","encrypt","decrypt","ciphertext","plaintext","tag"])
def test_empty_database_rejects_unusable_aes_before_readiness(world,monkeypatch,capsys,failure):
    import builtins
    import kalillac_accounts.registration_startup as startup
    import cryptography.hazmat.primitives.ciphers.aead as aead
    with world.factory() as session:
        assert session.scalar(select(func.count()).select_from(Intent))==0
    monkeypatch.setattr(startup,"read_protected",lambda *a:pytest.fail("primitive failure reached protected files"))
    real=aead.AESGCM;calls=[]
    if failure=="missing":
        original=builtins.__import__
        def importing(name,*args,**kwargs):
            if name=="cryptography.hazmat.primitives.ciphers.aead":
                calls.append("import");raise ImportError("synthetic missing dependency")
            return original(name,*args,**kwargs)
        monkeypatch.setattr(builtins,"__import__",importing)
    else:
        class Broken:
            def __init__(self,key):
                calls.append("construct")
                if failure=="construct":raise RuntimeError("synthetic construction failure")
                self.real=real(key)
            def encrypt(self,*args):
                calls.append("encrypt")
                if failure=="encrypt":raise RuntimeError("synthetic encrypt failure")
                return b"bad" if failure=="ciphertext" else self.real.encrypt(*args)
            def decrypt(self,*args):
                calls.append("decrypt")
                if failure=="decrypt":raise RuntimeError("synthetic decrypt failure")
                if failure=="plaintext":return b"bad"
                if failure=="tag":return bytes(16)
                return self.real.decrypt(*args)
        monkeypatch.setattr(aead,"AESGCM",Broken)
    with pytest.raises(RuntimeError,match="^REGISTRATION_CONFIG_INVALID$"):
        startup.bootstrap(world.factory,None,{"KALILLAC_ACCOUNT_REGISTRATION_V2_ENABLED":"true"})
    assert calls
    assert "REGISTRATION_V2_READY" not in capsys.readouterr().out

def test_aes_known_answer_and_disabled_mode_without_crypto(world,monkeypatch,capsys):
    import builtins
    import kalillac_accounts.registration_startup as startup
    startup._validate_aes_gcm()
    original=builtins.__import__
    def importing(name,*args,**kwargs):
        if name.startswith("cryptography"):pytest.fail("disabled crypto import")
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,"__import__",importing)
    monkeypatch.setattr(startup,"read_protected",lambda *a:pytest.fail("disabled protected file"))
    assert startup.bootstrap(world.factory,None,{}) is None
    assert "REGISTRATION_V2_READY" not in capsys.readouterr().out

class RecordedFatal:
    def __init__(self):
        self.tripped=threading.Event();self.started=False;self.closed=False
    def start(self):self.started=True
    def trip(self):assert self.started;self.tripped.set()
    def close(self):self.closed=True

@pytest.mark.parametrize("fault",["construct","start","tick","io"])
def test_actual_maintenance_failure_closes_intake_and_preserves_ownership(world,monkeypatch,fault):
    import kalillac_accounts.registration_dispatch as module
    real=threading.Thread;fatal=RecordedFatal()
    entry=__import__("kalillac_accounts.registration_markers",fromlist=["IssuanceEntry"]).IssuanceEntry(
        "a"*64,uuid.uuid4(),uuid.uuid4(),1,None,None,(),0.)
    world.markers.add(entry);world.markers.uncertain(entry)
    cancelled=threading.Event()
    monkeypatch.setattr(world.authority,"cancel_all",cancelled.set)
    def failure(*a,**k):raise RuntimeError("synthetic supervisor failure")
    if fault=="io":monkeypatch.setattr(world.authority,"reconcile",failure)
    def factory(*a,**k):
        if k.get("name")=="registration-owned-io" and fault=="construct":failure()
        thread=real(*a,**k)
        if k.get("name")=="registration-owned-io" and fault=="start":thread.start=failure
        return thread
    monkeypatch.setattr(module.threading,"Thread",factory)
    dispatcher=RegistrationDispatcher(world.authority,workers=0,capacity=2,fatal=fatal)
    if fault=="tick":monkeypatch.setattr(dispatcher,"_tick",failure)
    dispatcher.submit("queued@example.test")
    try:
        dispatcher.start()
        assert fatal.tripped.wait(5) and cancelled.wait(5)
        assert dispatcher._failed and dispatcher._stop.is_set() and world.authority._stopping.is_set()
        dispatcher.submit("refused@example.test")
        assert dispatcher._queue.empty() and dispatcher._queue.unfinished_tasks==0
        assert all(t.ident is not None for t in dispatcher._threads)
        if fault in ("construct","start"):assert dispatcher._io_thread is None
        assert dispatcher._slots.acquire(False) and dispatcher._slots.acquire(False)
        assert not dispatcher._slots.acquire(False)
        dispatcher._slots.release();dispatcher._slots.release()
        # Uncertain outcome metadata is retained, never falsely acknowledged.
        assert len(world.markers.snapshot())==1 and not world.gateway.links
        with pytest.raises(RegistrationUnavailable):world.authority.ensure_accepting()
    finally:
        dispatcher.close();dispatcher.close()
    assert not any(t.is_alive() for t in dispatcher._threads)

@pytest.mark.parametrize("outcome",["unresolved","unavailable","database"])
def test_actual_supervisor_keeps_retryable_reconciliation_alive(world,monkeypatch,outcome):
    from sqlalchemy.exc import DBAPIError
    from kalillac_accounts.registration_markers import IssuanceEntry
    entry=IssuanceEntry("a"*64,uuid.uuid4(),uuid.uuid4(),1,None,None,(),0.)
    world.markers.add(entry);world.markers.uncertain(entry)
    twice=threading.Event();calls=[]
    def reconcile(*a,**k):
        calls.append(1)
        if len(calls)>=2:twice.set()
        if outcome=="unavailable":raise RegistrationUnavailable(retry=True)
        if outcome=="database":raise DBAPIError("synthetic",None,Exception(),False)
        return False
    monkeypatch.setattr(world.authority,"reconcile",reconcile)
    fatal=RecordedFatal();dispatcher=RegistrationDispatcher(world.authority,workers=0,fatal=fatal)
    try:
        dispatcher.start();assert twice.wait(5)
        assert not dispatcher._failed and not fatal.tripped.is_set()
        dispatcher.submit("queued@example.test")
        assert dispatcher._queue.qsize()==1 and len(world.markers.snapshot())==1
    finally:dispatcher.close();dispatcher.close()

def test_cleanup_safely_ignores_constructed_unstarted_threads(world):
    dispatcher=RegistrationDispatcher(world.authority,workers=0)
    dispatcher._threads.append(threading.Thread(target=lambda:None))
    dispatcher._io_thread=threading.Thread(target=lambda:None)
    dispatcher.submit("synthetic@example.test")
    dispatcher.close();dispatcher.close()
    assert dispatcher._queue.empty() and dispatcher._queue.unfinished_tasks==0
    assert dispatcher._slots.acquire(False)
    dispatcher._slots.release()

def test_actual_fatal_supervisor_forces_bounded_nonzero_exit_when_cancellation_blocks():
    import subprocess,sys
    from pathlib import Path
    code="""
import threading
from types import SimpleNamespace
from kalillac_accounts.registration_dispatch import RegistrationDispatcher
stop=threading.Event()
class Markers:
 def snapshot(self):raise RuntimeError('synthetic control failure')
 def accepting(self):return True
authority=SimpleNamespace(markers=Markers(),refuse_new_work=stop.set,
 cancel_all=lambda:threading.Event().wait(60))
d=RegistrationDispatcher(authority,workers=0)
d.start()
assert stop.wait(5)
d.close()
raise AssertionError('fatal exit did not retain process ownership')
"""
    child=subprocess.run([sys.executable,"-B","-c",code],cwd=Path(__file__).resolve().parents[1],
        capture_output=True,text=True,timeout=8)
    assert child.returncode==1
    assert "Traceback" not in child.stderr

def test_fatal_state_refuses_confirmation_hash_and_new_dispatch(world,monkeypatch):
    w=world;w.authority.issue_after_response("synthetic@example.test");token=w.token()
    import kalillac_accounts.registration_http as http
    monkeypatch.setattr(http,"hash_password",lambda *a:pytest.fail("fatal state started Argon2"))
    fatal=RecordedFatal();dispatcher=RegistrationDispatcher(w.authority,workers=0,fatal=fatal)
    def failure():raise RuntimeError("synthetic tick")
    monkeypatch.setattr(dispatcher,"_tick",failure)
    app,jobs=app_for(w)
    try:
        dispatcher.start();assert fatal.tripped.wait(5) and w.authority._stopping.wait(5)
        with TestClient(app) as client:
            headers={"Origin":"https://registration.invalid"}
            result=client.post("/api/account/register/confirm",json={"token":token,"password":"valid synthetic password"},headers=headers)
            assert result.status_code==503 and result.json()=={"error":"registration_unavailable"}
            assert client.post("/api/account/register",json={"email":"other@example.test"},headers=headers).status_code==202
        assert not jobs.jobs
    finally:dispatcher.close();dispatcher.close()

ADDRESS_CASES=[
 ("a@example.com",True),("A_b.c%+-@EXAMPLE.COM",True),
 ("a"*64+"@example.com",True),("a"*65+"@example.com",False),
 ("a@"+"b"*63+".com",True),("a@"+"b"*64+".com",False),
 ("a@example."+"c"*63,True),("a@example."+"c"*64,False),
 ("person@x.c",False),("\u043f\u043e\u0447\u0442\u0430@example.com",False),("a()@example.com",False),
 ("a'b@example.com",False),('"a"@example.com',False),
 ("a@-example.com",False),("a@example-.com",False),("a@ex_ample.com",False),
 ("a@example.12",False),("a@xn--example.com",True),
 ("a@x..com",False),("a@example.com,other@example.com",False),
 ("a"*64+"@"+".".join(["b"*63]*4),True),
 ("a"*64+"@"+".".join(["b"*63]*3+["b"*61,"cc"]),False),
]
@pytest.mark.parametrize("address,valid",ADDRESS_CASES,ids=["address-boundary-"+str(n) for n in range(len(ADDRESS_CASES))])
def test_v2_address_contract_precedes_all_work(world,monkeypatch,address,valid):
    from kalillac_accounts.registration_address import normalize_registration_email
    from kalillac_accounts.resend_gateway import ADDRESS
    assert (normalize_registration_email(address) is not None)==valid
    app,dispatch=app_for(world)
    monkeypatch.setattr(type(world.ring),"pairs",lambda *a:pytest.fail("unexpected address cryptography"))
    monkeypatch.setattr(type(world.ring),"encrypt",lambda *a:pytest.fail("unexpected encryption"))
    monkeypatch.setattr(world.authority,"_transaction",lambda *a,**k:pytest.fail("unexpected intent/database work"))
    monkeypatch.setattr(world.gateway,"send_registration_finish",lambda *a:pytest.fail("unexpected delivery"))
    with TestClient(app) as client:
        result=client.post("/api/account/register",json={"email":address},headers={"Origin":"https://registration.invalid"})
    assert result.status_code==(202 if valid else 422)
    assert result.json()==({"ok":True} if valid else {"error":"invalid_email"})
    assert result.headers["cache-control"]=="no-store" and "set-cookie" not in result.headers
    if valid:
        assert dispatch.jobs==[address.strip().lower()]
        assert ADDRESS.fullmatch(dispatch.jobs[0])
    else:
        assert not dispatch.jobs and not world.limits._request and not world.limits._buckets
        world.authority.issue_after_response(address)
    with world.factory() as session:assert session.scalar(select(func.count()).select_from(Intent))==0

@pytest.mark.parametrize("address",["person@x.c","a()@example.com"])
def test_v2_address_rule_does_not_change_legacy_registration_or_login(world,monkeypatch,address):
    import kalillac_accounts.router as router
    monkeypatch.setattr(router,"hash_password",lambda *a:"synthetic-hash")
    monkeypatch.setattr(router,"verify_password",lambda *a:True)
    monkeypatch.setattr(router,"needs_rehash",lambda *a:False)
    legacy=FastAPI();legacy.include_router(build_account_router(session_factory=lambda:world.factory))
    legacy.add_middleware(UnsafeRequestGuard,origin="https://registration.invalid")
    with TestClient(legacy) as client:
        response=client.post("/api/account/register",json={"email":address,"password":"valid synthetic password"},headers={"Origin":"https://registration.invalid"})
        assert response.status_code==201
    with world.factory.begin() as session:
        user=session.scalar(select(User));user.email_verified_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    app,_=app_for(world)
    with TestClient(app) as client:
        response=client.post("/api/account/login",json={"email":address,"password":"valid synthetic password"},headers={"Origin":"https://registration.invalid"})
        assert response.status_code==200

def test_fatal_intake_while_waiting_for_password_gate_refunds_unstarted_work(world,monkeypatch):
    import httpx
    import kalillac_accounts.registration_http as module
    w=world;w.authority.issue_after_response("synthetic@example.test");token=w.token()
    monkeypatch.setattr(module,"hash_password",lambda *a:pytest.fail("fatal intake began Argon2"))
    waiting=threading.Event()
    class ObservedGate(PasswordWorkGate):
        async def acquire(self):
            waiting.set()
            return await super().acquire()
    async def scenario():
        gate=ObservedGate()
        await gate.acquire();waiting.clear()
        app,_=app_for(w,gate=gate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="https://registration.invalid") as client:
            task=asyncio.create_task(client.post("/api/account/register/confirm",json={"token":token,"password":"valid synthetic password"},headers={"Origin":"https://registration.invalid"}))
            assert await asyncio.to_thread(waiting.wait,5)
            w.authority.refuse_new_work()
            gate.release()
            result=await task
            assert result.status_code==503 and result.json()=={"error":"registration_unavailable"}
            await gate.acquire();gate.release()
    asyncio.run(scenario())
    assert not next(iter(w.limits._buckets.values())).events["work"]
    assert not any(bucket.reservations for bucket in w.limits._buckets.values())
