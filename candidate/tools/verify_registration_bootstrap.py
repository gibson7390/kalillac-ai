"""Exercise production bootstrap with root-owned files and disposable SCRAM roles.

Run as root only in an isolated test environment. Every receipt, identity, key
and password is generated for a new loopback-only fixture database. This never
reads deployment files, performs D1 inventory or activates an application.
"""
from __future__ import annotations
import base64,hashlib,json,os,pathlib,shutil,sys,uuid,io,contextlib
from datetime import datetime,timezone
sys.path[:0]=[str(pathlib.Path(__file__).resolve().parents[1]),str(pathlib.Path(__file__).resolve().parents[1]/"tests")]
import pytest
from sqlalchemy import text
from registration_test_support import postgres_world

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()

def main():
    assert sys.platform=="linux" and os.geteuid()==0,"isolated root fixture required"
    base=pathlib.Path("/run")/("kalillac-registration-bootstrap-test-"+uuid.uuid4().hex)
    assert base.parent==pathlib.Path("/run") and not base.exists()
    base.mkdir(mode=0o750);os.chown(base,0,65534)
    monkeypatch=pytest.MonkeyPatch();results=[]
    def protected(name,raw):
        path=base/name;temporary=base/(name+".new")
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o440)
        try:
            os.fchown(fd,0,65534);os.write(fd,raw);os.fsync(fd)
        finally:os.close(fd)
        os.replace(temporary,path)
        directory=os.open(base,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        return str(path)
    try:
        with postgres_world(monkeypatch,active=False) as w:
            with w.migration.begin() as connection:
                connection.exec_driver_sql("SET ROLE "+w.names["owner"])
                instance=str(connection.execute(text("SELECT database_instance_id FROM kalillac.account_registration_activation")).scalar_one())
            now=datetime.now(timezone.utc).isoformat()
            activation={"format_version":1,"policy_version":1,"database_instance_id":instance,"activation_id":str(uuid.uuid4()),
                "cutover_id":str(uuid.uuid4()),"deployment_id":"disposable-bootstrap","schema_revision":"0011_registration_intents",
                "evidence_sha256":"","inventory_at":now,"activated_at":now,"legacy_unverified_count":0}
            query="SELECT count(*) FROM kalillac.users WHERE email_verified_at IS NULL"
            ordinary=w.runtime_engine.url
            evidence={"query_text":query,"query_sha256":hashlib.sha256(query.encode()).hexdigest(),
                "aggregates":{"total_users":0,"legacy_unverified_count":0},"database_identity":{"database_instance_id":instance,
                "database_name":ordinary.database,"endpoint_identity":(ordinary.host or "")+":"+str(ordinary.port or 5432)},
                "schema_revision":activation["schema_revision"],"inventory_at":now,"cutover_id":activation["cutover_id"],
                "deployment_id":activation["deployment_id"],"writer_exclusion":{"stopped_release":"a935f62929b8ab072e6578d8a189c5284f4523a0",
                "lease_id":str(uuid.uuid4()),"cutover_id":activation["cutover_id"]}}
            activation["evidence_sha256"]=hashlib.sha256(canonical(evidence)).hexdigest()
            with w.migration.begin() as connection:
                connection.exec_driver_sql("SET ROLE "+w.names["owner"])
                connection.execute(text("UPDATE kalillac.account_registration_activation SET state='active_zero_legacy',activation_id=:activation_id,cutover_id=:cutover_id,deployment_id=:deployment_id,evidence_sha256=:evidence_sha256,inventory_at=:inventory_at,activated_at=:activated_at,legacy_unverified_count=0"),{k:activation[k] for k in ("activation_id","cutover_id","deployment_id","evidence_sha256","inventory_at","activated_at")})
            ring={"format_version":1,"active_encryption":w.ring.active_encryption,"active_lookup":w.ring.active_lookup,
                "encryption_keys":[{"version":k,"key_b64":base64.b64encode(v).decode()} for k,v in w.ring.encryption_keys.items()],
                "lookup_keys":[{"version":k,"key_b64":base64.b64encode(v).decode()} for k,v in w.ring.lookup_keys.items()]}
            credential={"format_version":1,"role":w.names["registration"],"database_instance_id":instance,
                "deployment_id":activation["deployment_id"],"password":w.engine.url.password}
            protected("keyring",canonical(ring));protected("activation",canonical(activation))
            protected("activation.evidence.json",canonical(evidence));protected("credential",canonical(credential))
            env={"KALILLAC_ACCOUNT_REGISTRATION_V2_ENABLED":"true","KALILLAC_SITE_ORIGIN":"https://registration.invalid",
                "KALILLAC_ACCOUNT_REGISTRATION_URL":"https://registration.invalid/account/create/confirm",
                "KALILLAC_DEPLOYMENT_ID":activation["deployment_id"],
                "KALILLAC_ACCOUNT_REGISTRATION_KEYRING_FILE":str(base/"keyring"),
                "KALILLAC_ACCOUNT_REGISTRATION_ACTIVATION_FILE":str(base/"activation"),
                "KALILLAC_ACCOUNT_REGISTRATION_DATABASE_CREDENTIAL_FILE":str(base/"credential"),
                "KALILLAC_MIGRATION_APP_ROLE":w.names["app"],"KALILLAC_MIGRATION_OWNER_ROLE":w.names["owner"],
                "KALILLAC_MIGRATION_REGISTRATION_ROLE":w.names["registration"]}
            def child(label,expected=True,overrides=None,crypto_failure=None):
                pid=os.fork()
                if pid==0:
                    try:
                        os.setgroups([]);os.setgid(65534);os.setuid(65534)
                        # Never reuse a DBAPI socket inherited across fork.
                        for engine in (w.engine,w.runtime_engine,w.migration):engine.dispose(close=False)
                        from kalillac_accounts.registration_startup import bootstrap
                        if crypto_failure:
                            import builtins
                            import cryptography.hazmat.primitives.ciphers.aead as aead
                            if crypto_failure=="missing":
                                original=builtins.__import__
                                def importing(name,*args,**kwargs):
                                    if name=="cryptography.hazmat.primitives.ciphers.aead":
                                        raise ImportError("synthetic missing primitive")
                                    return original(name,*args,**kwargs)
                                builtins.__import__=importing
                            else:
                                def broken(key):raise RuntimeError("synthetic broken primitive")
                                aead.AESGCM=broken
                        settings=dict(env);settings.update(overrides or {})
                        os.environ.update(settings)
                        output=io.StringIO()
                        with contextlib.redirect_stdout(output):
                            try:
                                authority=bootstrap(w.runtime_factory,w.gateway,settings)
                                if authority is not None:authority._dispose()
                                good=expected and authority is not None
                            except RuntimeError as error:
                                good=not expected and str(error) in ("REGISTRATION_CONFIG_INVALID","REGISTRATION_STATE_UNREADABLE")
                        good=good and (("REGISTRATION_V2_READY" in output.getvalue())==expected)
                        os._exit(0 if good else 1)
                    except BaseException:os._exit(2)
                _,status=os.waitpid(pid,0)
                results.append({"case":label,"passed":os.waitstatus_to_exitcode(status)==0})
            with w.migration.connect() as connection:
                assert connection.execute(text("SELECT count(*) FROM kalillac.account_registration_intents")).scalar_one()==0
            child("empty database validates usable AES-GCM")
            child("empty database refuses missing AES-GCM before readiness",False,crypto_failure="missing")
            child("empty database refuses broken AES-GCM before readiness",False,crypto_failure="construct")
            w.authority.issue_after_response("synthetic@example.test")
            assert len(w.gateway.links)==1
            w.engine.dispose()  # Old fixture issuer has exited before new admission.
            child("complete protected bootstrap and live-intent decrypt")
            child("active receipt refuses flag off",False,{"KALILLAC_ACCOUNT_REGISTRATION_V2_ENABLED":"false"})
            child("nonproduction setting refuses production bootstrap",False,{"KALILLAC_ACCOUNT_REGISTRATION_NONPRODUCTION":"true"})
            child("role name cannot rebind stored authority",False,{"KALILLAC_MIGRATION_REGISTRATION_ROLE":w.names["app"]})
            bad=dict(credential,password=base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="))
            protected("credential",canonical(bad));child("partial file rotation refuses SCRAM",False)
            protected("credential",canonical(credential))
            reused=dict(credential,password=ordinary.password)
            protected("credential",canonical(reused));child("ordinary credential reuse refused",False)
            protected("credential",canonical(credential))
            wrong=dict(credential,database_instance_id=str(uuid.uuid4()))
            protected("credential",canonical(wrong));child("credential database binding refused",False)
            protected("credential",canonical(credential))
            with w.migration.connect() as connection:
                assert connection.execute(text("SELECT count(*) FROM pg_stat_activity WHERE usename=:role"),{"role":w.names["registration"]}).scalar_one()==0,"old authority backend survived drain"
            original=credential["password"]
            credential["password"]=base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
            # Synthetic canonical URL-safe material cannot contain a SQL delimiter.
            from sqlalchemy import create_engine,make_url
            admin=create_engine(make_url(os.environ["KALILLAC_REGISTRATION_TEST_POSTGRES_URL"]).set(database=ordinary.database),isolation_level="AUTOCOMMIT",hide_parameters=True)
            try:
                with admin.connect() as connection:
                    connection.exec_driver_sql("ALTER ROLE "+w.names["registration"]+" PASSWORD '"+credential["password"]+"'")
                child("partial database rotation refuses old file",False)
                protected("credential",canonical(credential))
                child("matched restart rotation accepts fresh credential")
                with admin.connect() as connection:connection.exec_driver_sql("GRANT SELECT ON pg_catalog.pg_authid TO "+w.names["app"])
                child("explicit credential catalog grant refuses",False)
                with admin.connect() as connection:connection.exec_driver_sql("REVOKE SELECT ON pg_catalog.pg_authid FROM "+w.names["app"])
                with admin.connect() as connection:connection.exec_driver_sql("GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) TO "+w.names["app"])
                child("explicit server-file function grant refuses",False)
                with admin.connect() as connection:connection.exec_driver_sql("REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) FROM "+w.names["app"])
                old=dict(credential,password=original)
                protected("credential",canonical(old));child("old credential is not a fallback",False)
                protected("credential",canonical(credential))
            finally:admin.dispose()
            retained=json.loads(canonical(ring))
            retained["active_encryption"]="reg-enc-v2";retained["active_lookup"]="reg-lookup-v2"
            retained["encryption_keys"].append({"version":"reg-enc-v2","key_b64":base64.b64encode(os.urandom(32)).decode()})
            retained["lookup_keys"].append({"version":"reg-lookup-v2","key_b64":base64.b64encode(os.urandom(32)).decode()})
            protected("keyring",canonical(retained));child("rotation retains live old key versions")
            removed=json.loads(canonical(retained));removed["encryption_keys"]=removed["encryption_keys"][1:]
            protected("keyring",canonical(removed));child("missing live encryption version refuses",False)
            protected("keyring",canonical(retained))
            def owner_sql(sql):
                with w.migration.begin() as connection:
                    connection.exec_driver_sql("SET ROLE "+w.names["owner"]);connection.execute(text(sql))
            owner_sql("GRANT SELECT(id) ON kalillac.spaces TO "+w.names["registration"])
            child("unrelated column grant refuses",False)
            owner_sql("REVOKE SELECT(id) ON kalillac.spaces FROM "+w.names["registration"])
            owner_sql("CREATE FUNCTION kalillac.synthetic_unsafe_bridge() RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS 'SELECT NULL::void'")
            child("PUBLIC executable definer bridge refuses",False)
            owner_sql("DROP FUNCTION kalillac.synthetic_unsafe_bridge()")
            owner_sql("ALTER TABLE kalillac.users DISABLE TRIGGER trg_users_registration_provenance")
            child("disabled provenance guard refuses",False)
            owner_sql("ALTER TABLE kalillac.users ENABLE ALWAYS TRIGGER trg_users_registration_provenance")
            child("restored contract accepts fresh startup")
            # Same names and superficial metadata must never authorize readiness.
            def definition(sql,parameters=None):
                with w.migration.begin() as connection:
                    connection.exec_driver_sql("SET ROLE "+w.names["owner"])
                    return connection.execute(text(sql),parameters or {}).scalar()
            original=definition("SELECT pg_get_functiondef('kalillac.registration_user_insert_guard()'::regprocedure)")
            owner_sql("CREATE OR REPLACE FUNCTION kalillac.registration_user_insert_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $weak$ BEGIN -- session_user <> '"+w.names["registration"]+"'\n RETURN NEW; END $weak$")
            child("same-named no-op guard with role comment refuses readiness",False)
            owner_sql(original)
            child("canonical user-insert guard restoration accepts readiness")
            name="ck_intents_provenance_state"
            original=definition("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='kalillac.account_registration_intents'::regclass AND conname=:n",{"n":name})
            owner_sql("ALTER TABLE kalillac.account_registration_intents DROP CONSTRAINT "+name)
            owner_sql("ALTER TABLE kalillac.account_registration_intents ADD CONSTRAINT "+name+" CHECK (("+original[7:-1]+") OR state IS NOT NULL)")
            child("same-named weakened provenance CHECK refuses readiness",False)
            owner_sql("ALTER TABLE kalillac.account_registration_intents DROP CONSTRAINT "+name)
            owner_sql("ALTER TABLE kalillac.account_registration_intents ADD CONSTRAINT "+name+" "+original)
            child("canonical provenance CHECK restoration accepts readiness")
            name="uq_registration_current_lookup"
            original=definition("SELECT pg_get_indexdef('kalillac.uq_registration_current_lookup'::regclass)")
            owner_sql("DROP INDEX kalillac."+name)
            owner_sql("CREATE UNIQUE INDEX "+name+" ON kalillac.account_registration_intents(address_lookup_key_version,address_lookup_digest) WHERE is_current AND generation<0")
            child("same-named weakened partial unique predicate refuses readiness",False)
            owner_sql("DROP INDEX kalillac."+name)
            owner_sql(original)
            child("canonical partial unique predicate restoration accepts readiness")

    finally:
        monkeypatch.undo()
        assert base.parent==pathlib.Path("/run") and base.name.startswith("kalillac-registration-bootstrap-test-")
        shutil.rmtree(base)
    print(json.dumps({"synthetic_only":True,"tests":len(results),"failures":sum(not r["passed"] for r in results),"results":results},sort_keys=True))
    return 0 if all(r["passed"] for r in results) else 1

if __name__=="__main__":raise SystemExit(main())
