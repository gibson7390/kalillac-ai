"""PG01-PG28: real disposable PostgreSQL, actual independent R/A/M connections.

Set KALILLAC_REGISTRATION_TEST_POSTGRES_URL to a loopback-only disposable operator
database. Mandatory CI sets KALILLAC_REGISTRATION_REQUIRE_POSTGRES=true.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
import threading,uuid
import pytest
from sqlalchemy import select,text,event,func
from sqlalchemy.exc import DBAPIError,IntegrityError
from alembic import command
from kalillac_db.models import User,AccountEntitlement,AccountRegistrationIntent as Intent
from kalillac_accounts.registration_crypto import *
from kalillac_accounts.registration_startup import effective_privileges,schema_probe
from registration_test_support import postgres_world
from test_account_migration import _config

@pytest.fixture
def world(monkeypatch):
    with postgres_world(monkeypatch) as w:yield w

def scalar(w,sql,values=None,runtime=False):
    with (w.runtime_engine if runtime else w.engine).connect() as c:return c.execute(text(sql),values or {}).scalar()

def denied(w,sql,values=None):
    with w.runtime_engine.begin() as c:
        with pytest.raises(DBAPIError):c.execute(text(sql),values or {})

def issue(w,email="synthetic@example.test"):
    w.authority.issue_after_response(email)
    assert bool(w.gateway.links),"synthetic issuance failed"
    return w.token()

def confirm(w,token):
    w.authority.confirm_after_hash(w.authority.prevalidate(token),"synthetic-hash")

def corrupt(w,sql,values=None,table="account_registration_intents"):
    # Explicit operator-only negative-fixture corruption, never used to make a
    # legitimate runtime positive pass. Always restore ALWAYS guards atomically.
    with w.migration.begin() as c:
        c.exec_driver_sql("SET ROLE "+w.names["owner"])
        trigger="trg_registration_intent_lifecycle" if table=="account_registration_intents" else "trg_users_registration_identity"
        c.exec_driver_sql("ALTER TABLE kalillac."+table+" DISABLE TRIGGER "+trigger)
        c.execute(text(sql),values or {})
        c.exec_driver_sql("ALTER TABLE kalillac."+table+" ENABLE ALWAYS TRIGGER "+trigger)

def test_pg01_upgrade_downgrade_upgrade_preserves_rows(monkeypatch):
    with postgres_world(monkeypatch,active=False) as w:
        with w.runtime_factory.begin() as s:
            user=User(email="synthetic@example.test",password_hash="hash");s.add(user);s.flush()
            s.add(AccountEntitlement(user_id=user.id,tier="free",source="registration"))
        with w.migration.begin() as c:
            command.downgrade(_config(connection=c),"0010_spaces")
            assert c.exec_driver_sql("SELECT count(*) FROM kalillac.users").scalar()==1
            assert c.exec_driver_sql("SELECT count(*) FROM kalillac.account_entitlements").scalar()==1
            command.upgrade(_config(connection=c),"head")
        assert scalar(w,"SELECT count(*) FROM kalillac.users")==1

def test_pg02_runtime_lifecycle_and_catalog_denials(world):
    w=world;token=issue(w)
    for sql in [
        "UPDATE kalillac.account_registration_intents SET state='consumed'",
        "DELETE FROM kalillac.account_registration_intents",
        "UPDATE kalillac.account_registration_intents SET consumed_xid=pg_current_xact_id()",
        "UPDATE kalillac.account_registration_intents SET provenance_claimed=true",
        "TRUNCATE kalillac.account_registration_intents",
        "UPDATE kalillac.account_registration_activation SET state='unactivated'",
        "SET session_replication_role=replica",
        "ALTER TABLE kalillac.account_registration_intents DISABLE TRIGGER ALL",
        "CREATE TABLE kalillac.attacker(id integer)",
        "SELECT * FROM kalillac.alembic_version",
    ]:denied(w,sql)
    assert w.authority.prevalidate(token)

def test_pg03_same_token_two_confirmations(world):
    w=world;t=issue(w);a=w.authority.prevalidate(t);b=w.authority.prevalidate(t)
    barrier=threading.Barrier(2)
    def finish(proof):
        barrier.wait()
        try:w.authority.confirm_after_hash(proof,"hash");return "ok"
        except RegistrationConflict:return "conflict"
    with ThreadPoolExecutor(2) as pool:
        left=pool.submit(finish,a);right=pool.submit(finish,b)
        assert sorted([left.result(15),right.result(15)])==["conflict","ok"]
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==1

def test_pg04_different_generation_recheck(world):
    w=world;t=issue(w);proof=w.authority.prevalidate(t)
    issue(w)
    with pytest.raises(RegistrationInvalid):w.authority.confirm_after_hash(proof,"hash")
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==0

def test_pg05_expiry_after_hash_rechecked_at_database_time(world):
    w=world;t=issue(w);proof=w.authority.prevalidate(t)
    corrupt(w,"UPDATE kalillac.account_registration_intents SET created_at=clock_timestamp()-interval '25 hours',expires_at=clock_timestamp()-interval '1 second'")
    with pytest.raises(RegistrationInvalid):w.authority.confirm_after_hash(proof,"hash")
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==0

def test_pg06_delivery_rollback_restores_prior(world):
    w=world;t=issue(w)
    def fail():raise RuntimeError("SYNTHETIC_DELIVERY_FAILURE")
    w.gateway.before_send=fail
    w.authority.issue_after_response("synthetic@example.test")
    assert w.authority.prevalidate(t)
    assert scalar(w,"SELECT count(*) FROM kalillac.account_registration_intents")==1
    for entry in w.markers.snapshot():assert w.authority.reconcile(entry)

def test_pg07_cleanup_expired_rows(world):
    w=world;issue(w)
    corrupt(w,"UPDATE kalillac.account_registration_intents SET created_at=clock_timestamp()-interval '25 hours',expires_at=clock_timestamp()-interval '1 second'")
    w.authority.cleanup_expired()
    assert scalar(w,"SELECT count(*) FROM kalillac.account_registration_intents")==0

def test_pg08_retained_key_rotation_reads_prior_intent(world):
    w=world;t=issue(w)
    from types import MappingProxyType
    ring=w.ring
    new=Keyring("reg-enc-v2","reg-lookup-v2",MappingProxyType(dict(ring.encryption_keys,**{"reg-enc-v2":__import__("os").urandom(32)})),MappingProxyType(dict(ring.lookup_keys,**{"reg-lookup-v2":__import__("os").urandom(32)})))
    from kalillac_accounts.registration_authority import RegistrationAuthority
    replacement=RegistrationAuthority(w.factory,new,w.gateway,w.authority.url,role=w.names["registration"])
    assert replacement.prevalidate(t)
    incomplete=Keyring("reg-enc-v2","reg-lookup-v2",MappingProxyType({"reg-enc-v2":new.encryption_keys["reg-enc-v2"]}),MappingProxyType({"reg-lookup-v2":new.lookup_keys["reg-lookup-v2"]}))
    replacement.ring=incomplete
    with pytest.raises(RegistrationUnavailable):replacement.prevalidate(t)

def test_pg09_exclusive_control_barrier_waits_old_backend(world):
    w=world;acquired=threading.Event();release=threading.Event();waiting=threading.Event()
    def old():
        with w.authority._transaction() as s:
            w.authority._control(s);acquired.set();assert release.wait(10)
    def new():
        waiting.set()
        with w.authority._transaction() as s:w.authority._control(s,True)
        return True
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(old);assert acquired.wait(5)
        b=pool.submit(new);assert waiting.wait(5)
        # Prove a real ungranted lock from pg_locks rather than sleeping.
        for _ in range(1000):
            if scalar(w,"SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype IN ('tuple','transactionid')")>0:break
        else:pytest.fail("exclusive backend did not wait")
        assert not b.done()
        release.set();a.result(10);assert b.result(10)

def test_pg10_active_downgrade_refused(world):
    w=world
    with w.migration.begin() as c:
        with pytest.raises(RuntimeError,match="REGISTRATION_ACTIVE_DOWNGRADE_REFUSED"):
            command.downgrade(_config(connection=c),"0010_spaces")
    assert scalar(w,"SELECT count(*) FROM kalillac.account_registration_activation")==1

def test_pg11_cross_version_locks_share_exact_signed_keys(world):
    from kalillac_accounts.registration_crypto import advisory_keys
    w=world;keys=advisory_keys([("reg-lookup-v1",bytes(range(32)).hex()),("reg-lookup-v2",bytes(range(32)).hex())])
    assert keys==(-7500557648018874575,8028672564955918753)
    with w.engine.begin() as c:
        for k in keys:assert c.execute(text("SELECT CAST(:k AS bigint)"),{"k":k}).scalar()==k
        c.execute(text("SELECT pg_advisory_xact_lock(CAST(:k AS bigint))"),{"k":keys[0]})
        with w.engine.begin() as other:
            assert not other.execute(text("SELECT pg_try_advisory_xact_lock(CAST(:k AS bigint))"),{"k":keys[0]}).scalar()

def test_pg12_runtime_schema_probe_rejects_flag_off_active(world):
    with pytest.raises(RuntimeError,match="REGISTRATION_STATE_UNREADABLE"):
        schema_probe(world.runtime_factory,False)
    assert schema_probe(world.runtime_factory,True)

def test_pg13_old_binary_current_repository_rejected(world):
    from kalillac_db.repositories.accounts import create_user,EmailAlreadyRegistered
    w=world
    with w.runtime_factory.begin() as s:
        with pytest.raises(EmailAlreadyRegistered) as refused:create_user(s,"synthetic@example.test","hash")
        assert refused.value.__cause__.orig.sqlstate=="23514"
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==0

def test_pg14_no_direct_function_or_role_authority(world):
    w=world
    for name in ["registration_intent_lifecycle_guard","registration_user_insert_guard","registration_user_identity_guard"]:
        assert not scalar(w,"SELECT has_function_privilege(current_user,:f,'EXECUTE')",{"f":"kalillac."+name+"()"},True)
        denied(w,"SELECT kalillac."+name+"()")
    for n in ("registration","owner","migrate"):denied(w,"SET ROLE "+w.names[n])
    denied(w,"SET SESSION AUTHORIZATION "+w.names["registration"])
    with w.runtime_engine.begin() as c:
        c.execute(text("SELECT kalillac.registration_control_lock(false)"))
        c.execute(text("SELECT kalillac.registration_control_lock(true)"))
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==0

def test_pg15_expired_parent_fk_null_preserves_account(world):
    w=world;t=issue(w);confirm(w,t)
    corrupt(w,"UPDATE kalillac.account_registration_intents SET created_at=clock_timestamp()-interval '25 hours',consumed_at=clock_timestamp()-interval '2 hours',expires_at=clock_timestamp()-interval '1 second'")
    w.authority.cleanup_expired()
    assert scalar(w,"SELECT count(*) FROM kalillac.account_registration_intents")==0
    assert scalar(w,"SELECT count(*) FROM kalillac.users WHERE registration_intent_id IS NULL AND email_verified_at IS NOT NULL")==1
    assert scalar(w,"SELECT count(*) FROM kalillac.account_entitlements")==1

def test_pg16_old_repeatable_read_cannot_see_unactivated_receipt(monkeypatch):
    with postgres_world(monkeypatch,active=False) as w:
        r=w.runtime_engine.connect().execution_options(isolation_level="REPEATABLE READ")
        tx=r.begin();r.exec_driver_sql("SELECT state FROM kalillac.account_registration_activation")
        with w.migration.begin() as c:
            c.exec_driver_sql("SET ROLE "+w.names["owner"])
            c.exec_driver_sql("UPDATE kalillac.account_registration_activation SET state='active_zero_legacy',activation_id=gen_random_uuid(),cutover_id=gen_random_uuid(),deployment_id='dev-test',evidence_sha256=repeat('0',64),inventory_at=clock_timestamp(),activated_at=clock_timestamp(),legacy_unverified_count=0")
        with pytest.raises(DBAPIError):
            r.execute(text("INSERT INTO kalillac.users(id,email,password_hash) VALUES (:i,'synthetic@example.test','hash')"),{"i":uuid.uuid4()})
        tx.rollback();r.close()

@contextmanager
def lost_ack(w):
    committed=threading.Event();release=threading.Event();original=w.engine.dialect.do_commit;armed=[True]
    def commit(connection):
        original(connection)
        if armed[0]:
            armed[0]=False;committed.set()
            assert release.wait(15)
            raise OSError("SYNTHETIC_LOST_COMMIT_ACK")
    w.engine.dialect.do_commit=commit
    with ThreadPoolExecutor(1) as pool:
        job=pool.submit(w.authority.issue_after_response,"synthetic@example.test")
        try:
            assert committed.wait(10)
            yield w.token()
        finally:
            release.set();job.result(15);w.engine.dialect.do_commit=original

def test_pg17_visible_pending_wins_lost_commit_ack(world):
    w=world
    with lost_ack(w) as t:
        assert w.markers.snapshot()
        assert w.authority.prevalidate(t)
        confirm(w,t)
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==1

def test_pg18_unknown_miss_latch_and_reconciliation(world):
    w=world
    with lost_ack(w) as t:pass
    assert w.markers.snapshot()
    e=w.markers.snapshot()[0];assert w.authority.reconcile(e)
    assert not w.markers.snapshot() and w.authority.prevalidate(t)
    w.markers._latched=True
    assert w.authority.prevalidate(t)
    with pytest.raises(RegistrationUnavailable):w.authority.prevalidate(new_token())

def test_pg19_catalog_guard_ownership_and_modes(world):
    w=world
    with w.factory.begin() as s:effective_privileges(s,w.names["registration"],w.names["app"],w.names["owner"])
    assert scalar(w,"SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='kalillac' AND t.tgname LIKE 'trg_%' AND t.tgenabled='A'")==3

def test_pg20_exact_effective_privileges_and_separate_auth(world):
    w=world
    with w.engine.connect() as c:
        result=c.exec_driver_sql("SELECT session_user,current_user,system_user").one()
        assert result[0]==result[1]==w.names["registration"] and result[2]=="scram-sha-256:"+w.names["registration"]
    assert not scalar(w,"SELECT has_column_privilege(current_user,'kalillac.account_registration_intents','state','UPDATE')",runtime=True)
    assert not scalar(w,"SELECT has_column_privilege(current_user,'kalillac.users','email','UPDATE')",runtime=True)

def test_pg21_full_fabrication_chain_and_genuine_reference(world):
    w=world;t=issue(w)
    intent=scalar(w,"SELECT id FROM kalillac.account_registration_intents")
    denied(w,"INSERT INTO kalillac.account_registration_intents SELECT * FROM kalillac.account_registration_intents")
    denied(w,"INSERT INTO kalillac.account_registration_intents(id,state) VALUES (:i,'pending')",{"i":uuid.uuid4()})
    denied(w,"UPDATE kalillac.account_registration_intents SET state='consumed',is_current=false,email_ciphertext=NULL,email_nonce=NULL,email_key_version=NULL,consumed_at=clock_timestamp()")
    denied(w,"INSERT INTO kalillac.users(id,email,password_hash,email_verified_at,is_active,registration_intent_id) VALUES (:i,'other@example.test','hash',clock_timestamp(),true,:p)",{"i":uuid.uuid4(),"p":intent})
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==0
    assert w.authority.prevalidate(t)

def test_pg22_identity_hmac_and_cross_email_attachment(world):
    w=world;t=issue(w);proof=w.authority.prevalidate(t)
    with pytest.raises(RegistrationUnavailable):w.authority.confirm_after_hash(replace(proof,email="other@example.test"),"hash")
    denied(w,"UPDATE kalillac.users SET email='other@example.test'")
    # Corrupt a cryptographic row while retaining DB shape, with operator-only
    # fixture DDL; the actual component/DB read must reject the mismatch.
    corrupt(w,"UPDATE kalillac.account_registration_intents SET address_lookup_digest=:d",{"d":"f"*64})
    with pytest.raises(RegistrationUnavailable):w.authority.prevalidate(t)
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==0

def test_pg23_replay_across_users_transactions_and_pooled_connection(world):
    w=world;t=issue(w);proof=w.authority.prevalidate(t);confirm(w,t)
    with pytest.raises(RegistrationInvalid):w.authority.prevalidate(t)
    with pytest.raises(RegistrationConflict):w.authority.confirm_after_hash(proof,"hash")
    with pytest.raises(RegistrationInvalid):w.authority.confirm_after_hash(proof,"hash")
    intent=scalar(w,"SELECT id FROM kalillac.account_registration_intents")
    with w.engine.begin() as c:
        with pytest.raises(DBAPIError):
            c.execute(text("INSERT INTO kalillac.users(id,email,password_hash,is_active,email_verified_at,registration_intent_id) VALUES (:i,'other@example.test','hash',true,clock_timestamp(),:p)"),{"i":uuid.uuid4(),"p":intent})
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==1

def test_pg24_rollback_at_each_final_dml_boundary(world):
    w=world;t=issue(w)
    for needle in ["UPDATE kalillac.account_registration_intents","INSERT INTO kalillac.users","INSERT INTO kalillac.account_entitlements"]:
        def fail(conn,cursor,statement,parameters,context,executemany):
            if needle in statement:raise RuntimeError("SYNTHETIC_DML_FAILURE")
        event.listen(w.engine,"before_cursor_execute",fail)
        try:
            with pytest.raises(RuntimeError,match="SYNTHETIC_DML_FAILURE"):
                w.authority.confirm_after_hash(w.authority.prevalidate(t),"hash")
        finally:event.remove(w.engine,"before_cursor_execute",fail)
        assert scalar(w,"SELECT count(*) FROM kalillac.users")==0
        assert scalar(w,"SELECT count(*) FROM kalillac.account_registration_intents WHERE state='pending' AND NOT provenance_claimed")==1
    confirm(w,t)

def test_pg25_unexpired_cleanup_and_identity_updates_denied(world):
    w=world;t=issue(w);confirm(w,t)
    with w.engine.begin() as c:
        with pytest.raises(DBAPIError):c.execute(text("DELETE FROM kalillac.account_registration_intents"))
    for column,value in [("email","'other@example.test'"),("id","gen_random_uuid()"),("registration_intent_id","NULL"),("email_verified_at","NULL")]:
        denied(w,"UPDATE kalillac.users SET "+column+"="+value)
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==1

def test_pg26_wrong_pool_role_and_unsafe_graph_rejected(world):
    w=world
    from kalillac_accounts.registration_authority import RegistrationAuthority
    wrong=RegistrationAuthority(w.runtime_factory,w.ring,w.gateway,w.authority.url,role=w.names["registration"])
    with pytest.raises(RegistrationUnavailable):wrong.prevalidate(new_token())
    with w.factory.begin() as s:effective_privileges(s,w.names["registration"],w.names["app"],w.names["owner"])
    assert schema_probe(w.runtime_factory,True)

def test_pg27_visible_terminal_wins_blocked_marker_reconciliation(world):
    w=world
    with lost_ack(w) as old:
        proof=w.authority.prevalidate(old)
        issue(w)
        with pytest.raises(RegistrationInvalid):w.authority.prevalidate(old)
        current=w.token();confirm(w,current)
        w.markers._latched=True
        with pytest.raises(RegistrationInvalid):w.authority.prevalidate(current)
        assert w.markers.snapshot()
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==1

def test_pg28_real_commit_marker_epoch_race(world):
    w=world;delivering=threading.Event();release=threading.Event();done=threading.Event()
    def delivery():delivering.set();assert release.wait(10)
    w.gateway.before_send=delivery
    with ThreadPoolExecutor(1) as pool:
        job=pool.submit(w.authority.issue_after_response,"synthetic@example.test")
        assert delivering.wait(5)
        digest=w.markers.snapshot()[0].digest
        with pytest.raises(RegistrationPending):w.markers.lookup(digest,w.authority._read)
        first=[True]
        def read(d):
            result=w.authority._read(d)
            if first[0]:
                first[0]=False;release.set();job.result(10)
            return result
        assert w.markers.lookup(digest,read) is not None
    assert len(w.gateway.links)==1

def owner_definition(w,sql,values=None):
    with w.migration.begin() as connection:
        connection.exec_driver_sql("SET ROLE "+w.names["owner"])
        result=connection.execute(text(sql),values or {})
        return result.scalar() if result.returns_rows else None

@pytest.mark.parametrize("access",[(),("schema",),("database",),("schema","database")])
def test_unactivated_downgrade_revokes_only_migration_owned_access(monkeypatch,access):
    import json
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError
    with postgres_world(monkeypatch,active=False,preexisting_access=access) as w:
        def effective():
            with w.migration.connect() as c:
                return tuple(c.execute(text("SELECT has_schema_privilege(:a,'kalillac','USAGE'),has_database_privilege(:a,current_database(),'CONNECT')"),{"a":w.names["registration"]}).one())
        assert effective()==(True,True)
        receipt=json.loads(owner_definition(w,"SELECT obj_description('kalillac.account_registration_activation'::regclass,'pg_class')"))
        assert receipt["schema_usage_added"]==("schema" not in access)
        assert receipt["database_connect_added"]==("database" not in access)
        url=w.engine.url
        w.engine.dispose()
        with w.migration.begin() as c:command.downgrade(_config(connection=c),"0010_spaces")
        assert effective()==("schema" in access,"database" in access)
        if "database" not in access:
            fresh=create_engine(url,hide_parameters=True)
            try:
                with pytest.raises(OperationalError):fresh.connect()
            finally:fresh.dispose()
        with w.migration.begin() as c:command.upgrade(_config(connection=c),"head")
        assert effective()==(True,True)
        with w.factory.begin() as s:effective_privileges(s,w.names["registration"],w.names["app"],w.names["owner"])

@pytest.mark.parametrize("object_kind",["function","check","partial_index"])
def test_same_named_weakened_security_definition_is_rejected_and_restored(world,object_kind):
    w=world
    with w.factory.begin() as s:effective_privileges(s,w.names["registration"],w.names["app"],w.names["owner"])
    if object_kind=="function":
        original=owner_definition(w,"SELECT pg_get_functiondef('kalillac.registration_user_insert_guard()'::regprocedure)")
        weak="CREATE OR REPLACE FUNCTION kalillac.registration_user_insert_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $weak$ BEGIN -- session_user <> '"+w.names["registration"]+"'\n RETURN NEW; END $weak$"
        owner_definition(w,weak)
        restore=lambda:owner_definition(w,original)
    elif object_kind=="check":
        name="ck_intents_provenance_state"
        original=owner_definition(w,"SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='kalillac.account_registration_intents'::regclass AND conname=:n",{"n":name})
        owner_definition(w,"ALTER TABLE kalillac.account_registration_intents DROP CONSTRAINT "+name)
        owner_definition(w,"ALTER TABLE kalillac.account_registration_intents ADD CONSTRAINT "+name+" CHECK (("+original[7:-1]+") OR state IS NOT NULL)")
        def restore():
            owner_definition(w,"ALTER TABLE kalillac.account_registration_intents DROP CONSTRAINT "+name)
            owner_definition(w,"ALTER TABLE kalillac.account_registration_intents ADD CONSTRAINT "+name+" "+original)
    else:
        name="uq_registration_current_lookup"
        original=owner_definition(w,"SELECT pg_get_indexdef('kalillac.uq_registration_current_lookup'::regclass)")
        owner_definition(w,"DROP INDEX kalillac."+name)
        owner_definition(w,"CREATE UNIQUE INDEX "+name+" ON kalillac.account_registration_intents(address_lookup_key_version,address_lookup_digest) WHERE is_current AND generation<0")
        def restore():
            owner_definition(w,"DROP INDEX kalillac."+name)
            owner_definition(w,original)
    try:
        # All names, columns, broad trigger modes and uniqueness metadata still
        # satisfy the old structural probe; the semantic check must reject them.
        assert schema_probe(w.runtime_factory,True)
        with w.factory.begin() as s:
            with pytest.raises(RegistrationUnavailable):effective_privileges(s,w.names["registration"],w.names["app"],w.names["owner"])
    finally:restore()
    with w.factory.begin() as s:effective_privileges(s,w.names["registration"],w.names["app"],w.names["owner"])
