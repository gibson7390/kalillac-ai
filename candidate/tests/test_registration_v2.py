"""Focused C/R7/AR8 registration evidence; all identities/keys/delivery are synthetic."""
from datetime import timedelta
import asyncio,base64,json,uuid
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kalillac_db.models import AccountRegistrationIntent as Intent,User,AccountEntitlement,AccountSession
from kalillac_accounts.registration_crypto import *
from kalillac_accounts.registration_limits import RegistrationLimits
from kalillac_accounts.registration_markers import IssuanceMap,IssuanceEntry
from kalillac_accounts.registration_http import RegistrationHTTP
from kalillac_accounts.router import AccountSettings,build_account_router
from kalillac_accounts.request_guard import UnsafeRequestGuard
from registration_test_support import sqlite_world,key_bytes

@pytest.fixture
def world():
    w=sqlite_world()
    yield w
    w.engine.dispose()

def count(w,model):
    with w.factory() as s:return s.scalar(select(func.count()).select_from(model))

def test_neutral_new_existing_and_atomic_creation(world):
    w=world;w.authority.issue_after_response("new@example.test")
    assert len(w.gateway.links)==1 and count(w,User)==0
    proof=w.authority.prevalidate(w.token())
    w.authority.confirm_after_hash(proof,"synthetic-hash")
    assert count(w,User)==count(w,AccountEntitlement)==1 and count(w,AccountSession)==0
    with w.factory() as s:
        u=s.scalar(select(User));assert bool(u.email_verified_at) and bool(u.is_active)
        assert u.email=="new@example.test"
    w.authority.issue_after_response("new@example.test")
    assert w.gateway.notices==1 and len(w.gateway.links)==1
    with pytest.raises(RegistrationInvalid):w.authority.prevalidate(w.token())

@pytest.mark.parametrize("change",["id","generation","address_lookup_digest","email_nonce","email_ciphertext","email_key_version","purpose","format_version"])
def test_ciphertext_cannot_be_transplanted(world,change):
    w=world;w.authority.issue_after_response("new@example.test")
    with w.factory() as s:
        row=s.scalar(select(Intent));s.expunge(row)
    assert w.ring.decrypt(row)=="new@example.test"
    value=getattr(row,change)
    replacement={"id":uuid.uuid4(),"generation":2,"address_lookup_digest":"f"*64,"email_nonce":b"x"*12,"email_ciphertext":b"x"*32,"email_key_version":"reg-enc-v9","purpose":"recovery","format_version":2}[change]
    setattr(row,change,replacement)
    with pytest.raises(RegistrationUnavailable):w.ring.decrypt(row)

def test_encrypted_identity_hmac_mismatch_fails(world):
    w=world;w.authority.issue_after_response("new@example.test")
    with w.factory() as s:row=s.scalar(select(Intent));s.expunge(row)
    w.ring.encrypt(row,"other@example.test")
    with pytest.raises(RegistrationUnavailable):w.ring.decrypt(row)

def test_expiry_supersession_and_single_use_context(world):
    w=world;w.authority.issue_after_response("new@example.test");old=w.token()
    proof=w.authority.prevalidate(old)
    w.authority.issue_after_response("new@example.test")
    with pytest.raises(RegistrationInvalid):w.authority.prevalidate(old)
    with pytest.raises(RegistrationInvalid):w.authority.confirm_after_hash(proof,"hash")
    with pytest.raises(RegistrationInvalid):w.authority.confirm_after_hash(proof,"hash")
    w.time[0]+=timedelta(hours=24)
    with pytest.raises(RegistrationInvalid):w.authority.prevalidate(w.token())
    assert count(w,User)==0

def test_delivery_failure_rolls_back_old_generation(world):
    w=world;w.authority.issue_after_response("new@example.test");old=w.token()
    def fail():raise RuntimeError("synthetic delivery refusal")
    w.gateway.before_send=fail
    w.authority.issue_after_response("new@example.test")
    assert count(w,Intent)==1
    assert w.authority.prevalidate(old)
    for entry in w.markers.snapshot():assert w.authority.reconcile(entry)
    assert len(w.markers.snapshot())==0

def test_exact_client_windows_and_no_refund():
    now=[0.];limits=RegistrationLimits(clock=lambda:now[0],max_keys=2)
    for _ in range(5):assert limits.client("client").allowed
    assert not limits.client("client").allowed
    for _ in range(10):assert limits.client("client",True).allowed
    assert not limits.client("client",True).allowed
    now[0]=900
    assert limits.client("client").allowed and limits.client("client",True).allowed

def test_send_notice_and_reserved_work_accounting():
    now=[0.];l=RegistrationLimits(clock=lambda:now[0],max_keys=2)
    pairs=(("reg-lookup-v1","a"*64),)
    for _ in range(3):assert l.address(pairs,"link")[0].allowed
    assert not l.address(pairs,"link")[0].allowed
    assert l.address(pairs,"notice")[0].allowed and not l.address(pairs,"notice")[0].allowed
    attempts=[l.address(pairs,"work")[1] for _ in range(5)]
    assert not l.address(pairs,"work")[0].allowed
    attempts[0].release();extra=l.address(pairs,"work")[1]
    for a in attempts[1:]+[extra]:a.charge()
    assert not l.address(pairs,"work")[0].allowed
    now[0]=900
    assert l.address(pairs,"work")[0].allowed
    assert not l.address(pairs,"link")[0].allowed

def test_alias_capacity_and_reclamation():
    now=[0.];l=RegistrationLimits(clock=lambda:now[0],max_keys=1)
    one=(("reg-lookup-v1","a"*64),);both=one+(("reg-lookup-v2","b"*64),)
    assert l.address(one,"link")[0].allowed
    assert l.address(both,"link")[0].allowed
    assert l.address((both[1],),"link")[0].allowed
    assert not l.address(one,"link")[0].allowed
    assert not l.address((("reg-lookup-v1","c"*64),),"link")[0].allowed
    now[0]=86400
    assert l.address((("reg-lookup-v1","c"*64),),"link")[0].allowed
    assert len(l._aliases)==1


    # Ten controlled immutable snapshots, with eligible retirement after the
    # minimum retention window. Each restart deliberately resets local stores;
    # changing one snapshot never alters its predecessor's charged ledger.
    from types import MappingProxyType
    from kalillac_accounts.registration_authority import RegistrationAuthority
    w=sqlite_world()
    materials={i:(os.urandom(32),os.urandom(32)) for i in range(1,11)}
    previous=None
    try:
        for version in range(1,11):
            if previous is not None:
                assert previous.limits._buckets
                w.time[0]+=timedelta(hours=26);w.mono[0]+=26*3600
            retained=range(max(1,version-7),version+1)
            encryption=MappingProxyType({"reg-enc-v"+str(i):materials[i][0] for i in retained})
            lookup=MappingProxyType({"reg-lookup-v"+str(i):materials[i][1] for i in retained})
            ring=Keyring("reg-enc-v"+str(version),"reg-lookup-v"+str(version),encryption,lookup)
            current=RegistrationAuthority(w.factory,ring,w.gateway,w.authority.url,production=False,
                limits=RegistrationLimits(clock=lambda:w.mono[0]),markers=IssuanceMap(clock=lambda:w.mono[0]),
                clock=lambda:w.time[0],monotonic=lambda:w.mono[0])
            assert not current.limits._buckets and not current.limits._aliases
            current.issue_after_response("synthetic@example.test")
            assert len(w.gateway.links)==version
            assert len(current.limits._aliases)<=8
            assert current.prevalidate(w.token())
            if previous is not None:assert previous.ring.active_lookup=="reg-lookup-v"+str(version-1)
            previous=current
    finally:w.engine.dispose()

def entry(digest="a"*64):
    return IssuanceEntry(digest,uuid.uuid4(),uuid.uuid4(),1,None,None,(),0.)

@pytest.mark.parametrize("state",["pending","consumed","superseded","expired"])
def test_visible_state_wins_over_marker_and_latch(state):
    m=IssuanceMap(clock=lambda:0.);e=entry();m.add(e);m._latched=True
    row=SimpleNamespace(state=state)
    assert m.lookup(e.digest,lambda _:row) is row

def test_marker_only_on_miss_and_bounded_epoch_race():
    m=IssuanceMap(clock=lambda:0.);e=entry();m.add(e);calls=[]
    def read(d):calls.append(d);return None
    with pytest.raises(RegistrationPending):m.lookup(e.digest,read)
    assert len(calls)==1
    m.resolve(e);calls.clear()
    with pytest.raises(RegistrationInvalid):m.lookup(e.digest,read)
    assert len(calls)==2
    def churn(d):
        e2=entry();m.add(e2);m.resolve(e2);calls.append(d);return None
    calls.clear()
    with pytest.raises(RegistrationPending):m.lookup(e.digest,churn)
    assert len(calls)==3

def test_commit_removal_between_first_miss_and_epoch_capture():
    m=IssuanceMap(clock=lambda:0.);e=entry();m.add(e);row=object();calls=[]
    def read(d):
        calls.append(d)
        if len(calls)==1:m.resolve(e);return None
        return row
    assert m.lookup(e.digest,read) is row and len(calls)==2

def test_marker_capacity_deadline_and_exact_removal():
    now=[0.];m=IssuanceMap(clock=lambda:now[0],capacity=1);e=entry();m.add(e)
    with pytest.raises(RegistrationUnavailable):m.add(entry("b"*64))
    m.resolve(IssuanceEntry(e.digest,uuid.uuid4(),e.address_id,2,None,None,(),0))
    assert len(m.snapshot())==1
    now[0]=30;assert m.snapshot()[0].state=="uncertain"
    now[0]=60;assert not m.snapshot() and not m.accepting()
    with pytest.raises(RegistrationUnavailable):m.lookup(e.digest,lambda _:None)

@pytest.mark.parametrize("value",["","a","a"*43,"+"*43,True,42,None])
def test_canonical_tokens(value):
    if value=="a"*43:assert token_valid(value) is False
    else:assert not token_valid(value)

def test_dedicated_token_and_advisory_golden_vectors():
    token=new_token();assert token_valid(token) and len(token_digest(token))==64
    assert token_digest(token)!=__import__("hashlib").sha256(token.encode()).hexdigest()
    pairs=[("reg-lookup-v1",bytes(range(32)).hex()),("reg-lookup-v2",bytes(range(32)).hex())]
    assert advisory_keys(pairs)==(-7500557648018874575,8028672564955918753)

    import hashlib
    literal=[
        ("reg-lookup-v1","6b616c696c6c61632d726567697374726174696f6e2d6c6f636b2d7631007265672d6c6f6f6b75702d763100000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f","6f6b9328fc1f31a12cad5decd0a7619e2c6ea6a53a03d24c552549ebdf74e470","6f6b9328fc1f31a1",8028672564955918753),
        ("reg-lookup-v2","6b616c696c6c61632d726567697374726174696f6e2d6c6f636b2d7631007265672d6c6f6f6b75702d763200000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f","97e8aa8f0c7edf3153596179b672c099ca0c2662b173726a754f6d60e4e5e7c6","97e8aa8f0c7edf31",-7500557648018874575)]
    for version,material,full,first,signed in literal:
        raw=b"kalillac-registration-lock-v1\0"+version.encode()+b"\0"+bytes(range(32))
        assert raw.hex()==material
        digest=hashlib.sha256(raw).digest()
        assert digest.hex()==full and digest[:8].hex()==first
        assert int.from_bytes(digest[:8],"big",signed=True)==signed
        assert advisory_keys([(version,bytes(range(32)).hex())])==(signed,)

def test_keyring_duplicate_and_reuse_refused():
    raw=key_bytes();obj=json.loads(raw)
    obj["lookup_keys"][0]["key_b64"]=obj["encryption_keys"][0]["key_b64"]
    with pytest.raises(RegistrationUnavailable):Keyring.parse(json.dumps(obj).encode())
    with pytest.raises(RegistrationUnavailable):Keyring.parse(raw[:-1]+b',"format_version":1}')
    assert "key_b64" not in repr(Keyring.parse(raw))

def http_world(w):
    class Dispatch:
        def submit(self,email):self.submitted=email
    dispatcher=Dispatch()
    http=RegistrationHTTP(w.authority,dispatcher,AccountSettings(cookie_secure=False))
    app=FastAPI();app.include_router(build_account_router(session_factory=lambda:w.factory,registration_http=http))
    app.add_middleware(UnsafeRequestGuard,origin="https://registration.invalid")
    return app,dispatcher

def test_http_post_response_neutral_and_no_preproof_hash(world):
    w=world;app,dispatch=http_world(w)
    with TestClient(app) as client:
        r=client.post("/api/account/register",json={"email":"new@example.test"},headers={"Origin":"https://registration.invalid"})
        assert r.status_code==202 and r.json()=={"ok":True} and "set-cookie" not in r.headers
        assert count(w,Intent)==0 and dispatch.submitted=="new@example.test"
        r=client.post("/api/account/register/confirm",json={"token":"bad","password":"valid synthetic password"},headers={"Origin":"https://registration.invalid"})
        assert r.status_code==400 and count(w,User)==0

@pytest.mark.parametrize("body,status,code",[
    ({"email":"bad"},422,"invalid_email"),({"email":"new@example.test","password":"extra"},400,"invalid_body"),
])
def test_exact_request_body(world,body,status,code):
    app,_=http_world(world)
    with TestClient(app) as client:
        r=client.post("/api/account/register",json=body,headers={"Origin":"https://registration.invalid"})
        assert r.status_code==status and r.json()=={"error":code}

def test_protected_loader_windows_refuses_without_open(monkeypatch):
    import kalillac_accounts.registration_files as files
    monkeypatch.setattr(files.sys,"platform","win32")
    monkeypatch.setattr(files.os,"open",lambda *a,**k:pytest.fail("must not open"))
    with pytest.raises(RegistrationUnavailable):files.read_protected("/synthetic",8192)
