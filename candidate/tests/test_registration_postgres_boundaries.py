"""Additional real-role rollback, savepoint, issuance and committed-state probes."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading,uuid
import pytest
from sqlalchemy import text,event,select
from sqlalchemy.exc import DBAPIError
from kalillac_accounts.registration_crypto import RegistrationInvalid,RegistrationUnavailable
from kalillac_accounts.registration_authority import clear
from kalillac_db.models import User,AccountEntitlement,AccountRegistrationIntent as Intent
from test_registration_postgres import world,issue,confirm,scalar,denied,corrupt,lost_ack

@pytest.mark.parametrize("boundary",["supersede","insert","marker","commit"])
def test_issuance_failure_restores_previous_generation_without_redelivery(world,monkeypatch,boundary):
    w=world;old=issue(w);delivered=len(w.gateway.links);connections=[]
    def checkout(connection,record,proxy):connections.append(connection)
    def fail_sql(conn,cursor,statement,parameters,context,executemany):
        needle="UPDATE kalillac.account_registration_intents" if boundary=="supersede" else "INSERT INTO kalillac.account_registration_intents"
        if needle in statement:raise RuntimeError("SYNTHETIC_ISSUANCE_BOUNDARY")
    def fail_commit(connection):raise RuntimeError("SYNTHETIC_COMMIT_BOUNDARY")
    def fail_marker(entry):raise RegistrationUnavailable()
    event.listen(w.engine,"checkout",checkout)
    if boundary in ("supersede","insert"):event.listen(w.engine,"before_cursor_execute",fail_sql)
    if boundary=="commit":event.listen(w.engine,"commit",fail_commit)
    if boundary=="marker":monkeypatch.setattr(w.markers,"add",fail_marker)
    try:w.authority.issue_after_response("synthetic@example.test")
    finally:
        event.remove(w.engine,"checkout",checkout)
        if boundary in ("supersede","insert"):event.remove(w.engine,"before_cursor_execute",fail_sql)
        if boundary=="commit":event.remove(w.engine,"commit",fail_commit)
    assert len(w.gateway.links)==delivered+(1 if boundary=="commit" else 0)
    assert scalar(w,"SELECT count(*) FROM kalillac.account_registration_intents")==1
    assert w.authority.prevalidate(old)
    for entry in w.markers.snapshot():assert w.authority.reconcile(entry)
    if boundary!="marker":assert bool(connections) and all(c.closed for c in connections),"failed physical connection was recycled"
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==0

def test_nested_savepoint_rollback_cannot_export_a_stamp_or_claim(world):
    w=world;token=issue(w)
    with w.authority._transaction() as session:
        nested=session.begin_nested()
        row=session.scalar(select(Intent).with_for_update());intent_id=row.id
        now=w.authority._time(session);clear(row,"consumed",now);session.flush()
        user=User(email="synthetic@example.test",password_hash="hash",email_verified_at=now,is_active=True,registration_intent_id=intent_id)
        session.add(user);session.flush()
        session.add(AccountEntitlement(user_id=user.id,tier="free",source="registration"));session.flush()
        assert session.scalar(select(Intent.provenance_claimed))
        nested.rollback()
        session.expire_all()
        assert session.scalar(select(Intent.state))=="pending"
        assert not session.scalar(select(Intent.provenance_claimed))
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==0
    # A later top-level transaction cannot attach the rolled-back authority.
    with w.engine.begin() as connection:
        with pytest.raises(DBAPIError):
            connection.execute(text("INSERT INTO kalillac.users(id,email,password_hash,is_active,email_verified_at,registration_intent_id) VALUES (:id,'other@example.test','hash',true,clock_timestamp(),:intent)"),{"id":uuid.uuid4(),"intent":intent_id})
    confirm(w,token)
    assert scalar(w,"SELECT count(*) FROM kalillac.account_entitlements")==1

def test_real_same_address_writers_serialize_while_other_address_progresses(world):
    w=world;first_delivery=threading.Event();release=threading.Event();second_entered=threading.Event()
    count=[0];lock=threading.Lock()
    def deliver():
        with lock:count[0]+=1;n=count[0]
        if n==1:first_delivery.set();assert release.wait(15)
    w.gateway.before_send=deliver
    def same_address():
        second_entered.set();w.authority.issue_after_response("synthetic@example.test")
    with ThreadPoolExecutor(3) as pool:
        first=pool.submit(w.authority.issue_after_response,"synthetic@example.test")
        assert first_delivery.wait(5)
        second=pool.submit(same_address);assert second_entered.wait(5)
        third=pool.submit(w.authority.issue_after_response,"other@example.test")
        try:
            third.result(10)
            for _ in range(1000):
                if scalar(w,"SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype='advisory'"):break
            else:pytest.fail("same address did not contend")
            assert not second.done()
        finally:release.set()
        first.result(10);second.result(10)
    assert scalar(w,"SELECT count(*) FROM kalillac.account_registration_intents WHERE is_current")==2
    assert scalar(w,"SELECT max(generation) FROM kalillac.account_registration_intents")==2

@pytest.mark.parametrize("state",["expired","wrong_purpose"])
def test_visible_invalid_row_overrides_retained_marker_and_latch(world,state):
    w=world
    with lost_ack(w) as token:
        if state=="expired":
            corrupt(w,"UPDATE kalillac.account_registration_intents SET created_at=clock_timestamp()-interval '25 hours',expires_at=clock_timestamp()-interval '1 second'")
        else:
            # Owner-only fixture corruption bypasses CHECK as well as trigger,
            # to exercise service purpose refusal on a visible committed row.
            with w.migration.begin() as c:
                c.exec_driver_sql("SET ROLE "+w.names["owner"])
                c.exec_driver_sql("ALTER TABLE kalillac.account_registration_intents DROP CONSTRAINT ck_account_registration_intents_purpose_format")
            corrupt(w,"UPDATE kalillac.account_registration_intents SET purpose='wrong_purpose'")
        w.markers._latched=True
        assert w.markers.snapshot()
        with pytest.raises(RegistrationInvalid):w.authority.prevalidate(token)
        assert w.markers.snapshot()
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==0

def test_runtime_copy_upsert_row_assignments_and_server_file_authority_denied(world):
    w=world;issue(w)
    for statement in [
        "INSERT INTO kalillac.account_registration_intents SELECT * FROM kalillac.account_registration_intents ON CONFLICT(id) DO UPDATE SET state=excluded.state",
        "UPDATE kalillac.account_registration_intents SET (state,is_current)=('consumed',false)",
        "SELECT pg_read_file('/synthetic-registration-credential')",
        "SELECT * FROM pg_authid",
        "CREATE FUNCTION kalillac.registration_attacker() RETURNS void LANGUAGE sql AS 'SELECT NULL::void'",
    ]:denied(w,statement)
    with w.runtime_engine.begin() as connection:
        try:
            with connection.begin_nested():connection.exec_driver_sql("GRANT ALL ON kalillac.account_registration_intents TO PUBLIC")
        except DBAPIError:pass
    assert scalar(w,"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE n.nspname=\'kalillac\' AND c.relname=\'account_registration_intents\' AND a.grantee=0")==0
    import psycopg
    with w.runtime_engine.begin() as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.connection.driver_connection.cursor().copy("COPY kalillac.account_registration_intents FROM STDIN") as copy:
                pass
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==0

def test_active_migration_repair_cannot_reassign_existing_identity(world):
    w=world;confirm(w,issue(w))
    for column,value in [("email","'other@example.test'"),("id","gen_random_uuid()"),("registration_intent_id","NULL")]:
        with w.migration.begin() as connection:
            connection.exec_driver_sql("SET ROLE "+w.names["owner"])
            with pytest.raises(DBAPIError):
                connection.exec_driver_sql("UPDATE kalillac.users SET "+column+"="+value)
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==1

def test_cleanup_skips_locked_confirmation_parent_and_keeps_ledgers(world):
    w=world;token=issue(w);confirm(w,token)
    corrupt(w,"UPDATE kalillac.account_registration_intents SET created_at=clock_timestamp()-interval '25 hours',expires_at=clock_timestamp()-interval '1 second'")
    with w.engine.begin() as connection:
        connection.exec_driver_sql("SELECT id FROM kalillac.account_registration_intents FOR UPDATE")
        with ThreadPoolExecutor(1) as pool:pool.submit(w.authority.cleanup_expired).result(10)
        assert scalar(w,"SELECT count(*) FROM kalillac.account_registration_intents")==1
    before=len(w.limits._buckets)
    w.authority.cleanup_expired()
    assert scalar(w,"SELECT count(*) FROM kalillac.account_registration_intents")==0
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==1
    assert scalar(w,"SELECT count(*) FROM kalillac.account_entitlements")==1
    assert len(w.limits._buckets)==before

def test_actual_three_read_epoch_race_observes_new_committed_row(world,monkeypatch):
    from kalillac_accounts.registration_crypto import new_token,token_digest
    import kalillac_accounts.registration_authority as authority
    w=world;token=new_token();monkeypatch.setattr(authority,"new_token",lambda:token)
    reads=[]
    def read(digest):
        row=w.authority._read(digest);reads.append(row is not None)
        if len(reads)==2:issue(w)
        return row
    row=w.markers.lookup(token_digest(token),read)
    assert reads==[False,False,True] and row.state=="pending"
    assert len(w.gateway.links)==1 and w.authority.prevalidate(token)

def test_backend_death_before_commit_resolves_under_fresh_control_barrier(world):
    import os
    from sqlalchemy import create_engine,make_url
    w=world;delivering=threading.Event();release=threading.Event()
    def delivery():delivering.set();assert release.wait(15)
    w.gateway.before_send=delivery
    admin=create_engine(make_url(os.environ["KALILLAC_REGISTRATION_TEST_POSTGRES_URL"]).set(database=w.engine.url.database),hide_parameters=True)
    try:
        with ThreadPoolExecutor(1) as pool:
            job=pool.submit(w.authority.issue_after_response,"synthetic@example.test")
            try:
                assert delivering.wait(5);entry=w.markers.snapshot()[0]
                with w.authority._connections_lock:
                    backend=w.authority._connections[entry.intent_id].info.backend_pid
                with admin.begin() as connection:
                    assert connection.execute(text("SELECT pg_terminate_backend(:pid)"),{"pid":backend}).scalar_one()
            finally:release.set()
            job.result(10)
        assert len(w.gateway.links)==1 and w.markers.snapshot()
        # Observe the resolved database transaction under the actual exclusive
        # startup barrier before treating committed absence as authoritative.
        with w.authority._transaction() as session:w.authority._control(session,True)
        assert w.authority.reconcile(entry)
        with pytest.raises(RegistrationInvalid):w.authority.prevalidate(w.token())
        assert scalar(w,"SELECT count(*) FROM kalillac.account_registration_intents")==0
    finally:admin.dispose()

def test_expiry_sampled_after_actual_address_lock_wait(world):
    from kalillac_accounts.registration_crypto import advisory_keys
    w=world;proof=w.authority.prevalidate(issue(w))
    held=w.engine.connect();transaction=held.begin()
    for key in advisory_keys(w.ring.pairs(proof.email)):
        held.execute(text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"),{"key":key})
    with ThreadPoolExecutor(1) as pool:
        job=pool.submit(w.authority.confirm_after_hash,proof,"hash")
        try:
            for _ in range(1000):
                if scalar(w,"SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype='advisory'"):break
            else:pytest.fail("confirmation did not wait on the address lock")
            corrupt(w,"UPDATE kalillac.account_registration_intents SET created_at=clock_timestamp()-interval '25 hours',expires_at=clock_timestamp()-interval '1 second'")
        finally:transaction.rollback();held.close()
        with pytest.raises(RegistrationInvalid):job.result(10)
    assert scalar(w,"SELECT count(*) FROM kalillac.users")==0
