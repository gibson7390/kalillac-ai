"""Accounts-enabled schema fence and production-only protected registration bootstrap.

No path here performs D1 inventory, writes activation, or repairs grants.
"""
from __future__ import annotations
import base64
import hashlib
import os
import sys
import uuid
from sqlalchemy import create_engine,event,inspect,select,text,func
from sqlalchemy.orm import sessionmaker
from kalillac_db.models import AccountRegistrationActivation as Activation,AccountRegistrationIntent as Intent,User
from kalillac_db.migration_roles import migration_app_role,migration_owner_role,validate_role_name
from .registration_crypto import Keyring,RegistrationUnavailable,strict_json
from .registration_files import read_protected

TRUE={"1","true","yes","on"}
def enabled(env=None):
    env=os.environ if env is None else env
    return env.get("KALILLAC_ACCOUNT_REGISTRATION_V2_ENABLED","").strip().lower() in TRUE

def _uuid(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:raise RegistrationUnavailable()
    return value

def _receipt(session):
    row=session.get(Activation,1)
    if row is None or row.policy_version!=1 or row.state not in ("unactivated","active_zero_legacy"):raise RegistrationUnavailable()
    return row


def _schema_shape(session):
    inspector=inspect(session.connection())
    dialect=session.get_bind().dialect
    for model in (Intent,Activation):
        table=model.__table__
        columns=inspector.get_columns(table.name,schema="kalillac")
        if {c["name"] for c in columns}!=set(table.columns.keys()):raise RegistrationUnavailable()
        for col in columns:
            expected=table.columns[col["name"]]
            if col["nullable"]!=expected.nullable or str(col["type"].compile(dialect=dialect)).lower()!=str(expected.type.compile(dialect=dialect)).lower():raise RegistrationUnavailable()
        checks={c["name"] for c in inspector.get_check_constraints(table.name,schema="kalillac")}
        from sqlalchemy import CheckConstraint,UniqueConstraint
        if checks!={str(c.name) for c in table.constraints if isinstance(c,CheckConstraint)}:raise RegistrationUnavailable()
        actual_unique={tuple(c["column_names"]) for c in inspector.get_unique_constraints(table.name,schema="kalillac")}
        if actual_unique!={tuple(c.columns.keys()) for c in table.constraints if isinstance(c,UniqueConstraint)}:raise RegistrationUnavailable()
        actual={(i["name"],tuple(i["column_names"]),bool(i["unique"])) for i in inspector.get_indexes(table.name,schema="kalillac") if not i.get("duplicates_constraint")}
        expected={(i.name,tuple(c.name for c in i.columns),bool(i.unique)) for i in table.indexes}
        if actual!=expected:raise RegistrationUnavailable()
        if inspector.get_pk_constraint(table.name,schema="kalillac")["constrained_columns"]!=[list(table.primary_key.columns)[0].name]:raise RegistrationUnavailable()
    references=inspector.get_foreign_keys("users",schema="kalillac")
    if not any(f["constrained_columns"]==["registration_intent_id"] and f["referred_table"]==Intent.__tablename__ and f["referred_columns"]==["id"] and f.get("options",{}).get("ondelete")=="SET NULL" for f in references):raise RegistrationUnavailable()
    if not any(i["name"]=="uq_users_registration_intent" and i["column_names"]==["registration_intent_id"] and i["unique"] for i in inspector.get_indexes("users",schema="kalillac")):raise RegistrationUnavailable()
    if dialect.name=="postgresql":
        rows=session.execute(text("SELECT t.tgname,c.relname,t.tgtype,t.tgenabled,p.proname FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace JOIN pg_catalog.pg_proc p ON p.oid=t.tgfoid WHERE n.nspname='kalillac' AND c.relname IN ('users','account_registration_intents') AND NOT t.tgisinternal")).all()
        expected={
            ("trg_registration_intent_lifecycle","account_registration_intents",31,"A","registration_intent_lifecycle_guard"),
            ("trg_users_registration_provenance","users",7,"A","registration_user_insert_guard"),
            ("trg_users_registration_identity","users",19,"A","registration_user_identity_guard")}
        if set(map(tuple,rows))!=expected:raise RegistrationUnavailable()

def _utc_timestamp(value):
    import re
    from datetime import datetime,timedelta
    if not isinstance(value,str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)",value):raise RegistrationUnavailable()
    parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
    if parsed.utcoffset()!=timedelta(0):raise RegistrationUnavailable()
    return parsed

def _evidence_shape(activation,evidence,ordinary):
    import re
    for key in ("database_instance_id","activation_id","cutover_id"):_uuid(activation[key])
    if not isinstance(activation["deployment_id"],str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",activation["deployment_id"]):raise RegistrationUnavailable()
    for key in ("evidence_sha256",):
        if not isinstance(activation[key],str) or not re.fullmatch("[0-9a-f]{64}",activation[key]):raise RegistrationUnavailable()
    if _utc_timestamp(activation["inventory_at"])>_utc_timestamp(activation["activated_at"]):raise RegistrationUnavailable()
    if not isinstance(evidence["query_text"],str) or not evidence["query_text"].strip() or len(evidence["query_text"])>32768:raise RegistrationUnavailable()
    if not isinstance(evidence["query_sha256"],str) or not re.fullmatch("[0-9a-f]{64}",evidence["query_sha256"]):raise RegistrationUnavailable()
    def aggregates(value):
        if isinstance(value,dict) and value:
            return all(isinstance(k,str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}",k) and aggregates(v) for k,v in value.items())
        return type(value) is bool or (type(value) is int and 0<=value<=2**63-1)
    if not isinstance(evidence["aggregates"],dict) or not aggregates(evidence["aggregates"]):raise RegistrationUnavailable()
    identity=evidence["database_identity"]
    if not isinstance(identity,dict) or set(identity)!={"database_instance_id","database_name","endpoint_identity"}:raise RegistrationUnavailable()
    if identity["database_instance_id"]!=activation["database_instance_id"] or identity["database_name"]!=ordinary.database or identity["endpoint_identity"]!=(ordinary.host or "")+":"+str(ordinary.port or 5432):raise RegistrationUnavailable()
    writer=evidence["writer_exclusion"]
    if not isinstance(writer,dict) or set(writer)!={"stopped_release","lease_id","cutover_id"}:raise RegistrationUnavailable()
    if not isinstance(writer["stopped_release"],str) or not re.fullmatch("[0-9a-f]{40}",writer["stopped_release"]) or _uuid(writer["lease_id"])!=writer["lease_id"] or writer["cutover_id"]!=activation["cutover_id"]:raise RegistrationUnavailable()

def _ordinary_auth(factory,ordinary):
    with factory() as session:
        row=session.execute(text("SELECT session_user,current_user,system_user")).one()
    if ordinary.username!=migration_app_role() or row[0]!=ordinary.username or row[1]!=ordinary.username:raise RegistrationUnavailable()
    if not isinstance(row[2],str):raise RegistrationUnavailable()
    method,separator,identity=row[2].partition(":")
    if not separator or not identity:raise RegistrationUnavailable()
    if method in ("scram-sha-256","md5","password"):
        if identity!=ordinary.username or ordinary.password is None:raise RegistrationUnavailable()
        return ordinary.password
    if method not in ("peer","cert","gss","sspi"):raise RegistrationUnavailable()
    print("REGISTRATION_ORDINARY_AUTH_NONPASSWORD",method)
    return None


def schema_probe(factory,v2):
    """Must precede mounting either registration route, even with every path absent."""
    try:
        if factory is None:raise RegistrationUnavailable()
        with factory() as session:
            with session.begin():
                names=set(inspect(session.connection()).get_table_names(schema="kalillac"))
                before={"users","account_sessions","account_entitlements","account_recovery_tokens","account_email_verification_tokens","spaces","conversations","messages"}
                new={"account_registration_intents","account_registration_activation"}
                if not before<=names:raise RegistrationUnavailable()
                user_columns={c["name"] for c in inspect(session.connection()).get_columns("users",schema="kalillac")}
                if not names&new:
                    if v2 or user_columns & {"registration_intent_id","quarantined_at","sessions_valid_after"}:raise RegistrationUnavailable()
                    return False
                if not new<=names:raise RegistrationUnavailable()
                required={"registration_intent_id","quarantined_at","sessions_valid_after"}
                if not required<={c["name"] for c in inspect(session.connection()).get_columns("users",schema="kalillac")}:raise RegistrationUnavailable()
                _schema_shape(session)
                row=_receipt(session)
                if session.get_bind().dialect.name=="postgresql":
                    functions=set(session.execute(text("SELECT p.proname FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='kalillac'")).scalars())
                    if not {"registration_control_lock","registration_intent_lifecycle_guard","registration_user_insert_guard","registration_user_identity_guard"}<=functions:raise RegistrationUnavailable()
                    triggers=set(session.execute(text("SELECT t.tgname FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='kalillac' AND t.tgenabled='A'")).scalars())
                    if not {"trg_registration_intent_lifecycle","trg_users_registration_provenance","trg_users_registration_identity"}<=triggers:raise RegistrationUnavailable()
                if not v2 and row.state!="unactivated":raise RegistrationUnavailable()
                return True
    except Exception:raise RuntimeError("REGISTRATION_STATE_UNREADABLE") from None

def _check_credential(raw,role,instance,deployment,ordinary_password,ring):
    c=strict_json(raw)
    if not isinstance(c,dict) or set(c)!={"format_version","role","database_instance_id","deployment_id","password"} or type(c["format_version"]) is not int or c["format_version"]!=1:
        raise RegistrationUnavailable()
    if c["role"]!=role or _uuid(c["database_instance_id"])!=instance or c["deployment_id"]!=deployment:raise RegistrationUnavailable()
    password=c["password"]
    if not isinstance(password,str) or len(password)!=43:raise RegistrationUnavailable()
    try:decoded=base64.urlsafe_b64decode(password+"=")
    except ValueError:raise RegistrationUnavailable() from None
    if len(decoded)!=32 or base64.urlsafe_b64encode(decoded).decode().rstrip("=")!=password:raise RegistrationUnavailable()
    forbidden=set(ring.encryption_keys.values())|set(ring.lookup_keys.values())
    if ordinary_password is not None:
        if password==ordinary_password:raise RegistrationUnavailable()
        for decode in [lambda s:base64.urlsafe_b64decode(s+"="*(-len(s)%4)),lambda s:base64.b64decode(s,validate=True)]:
            try:
                other=decode(ordinary_password)
                if len(other)==32:forbidden.add(other)
            except (ValueError,TypeError):pass
    if decoded in forbidden:raise RegistrationUnavailable()
    return password

def effective_privileges(session,role,runtime,owner):
    """Inspect effective grants, memberships, exposed definer functions and guards."""
    names=[role,runtime,owner]
    rows=session.execute(text("SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolcanlogin,rolinherit FROM pg_catalog.pg_roles WHERE rolname=ANY(:names)"),{"names":names}).all()
    by={r[0]:r for r in rows}
    if set(by)!=set(names):raise RegistrationUnavailable()
    if any(any(by[n][1:6]) for n in (role,runtime)) or not by[role][6] or by[role][7] or by[owner][6]:raise RegistrationUnavailable()
    for principal in [runtime,role]:
        memberships=session.execute(text("SELECT rolname FROM pg_catalog.pg_roles WHERE oid<>(SELECT oid FROM pg_catalog.pg_roles WHERE rolname=:p) AND pg_catalog.pg_has_role(:p,oid,'MEMBER')"),{"p":principal}).scalars().all()
        if memberships:raise RegistrationUnavailable()
        if session.execute(text("SELECT pg_catalog.has_schema_privilege(:p,'kalillac','CREATE')"),{"p":principal}).scalar_one():raise RegistrationUnavailable()
        functions=session.execute(text("SELECT p.oid,n.nspname,p.proname,p.prosecdef,p.proowner,p.proconfig FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND pg_catalog.has_function_privilege(:p,p.oid,'EXECUTE')"),{"p":principal}).all()
        if any(f[3] and (f[1],f[2])!=("kalillac","registration_control_lock") for f in functions):raise RegistrationUnavailable()
        for table in ["account_registration_activation","account_registration_intents","users"]:
            for right in ["TRUNCATE","TRIGGER","REFERENCES"]:
                if session.execute(text("SELECT pg_catalog.has_table_privilege(:p,:t,:r)"),{"p":principal,"t":"kalillac."+table,"r":right}).scalar_one():raise RegistrationUnavailable()
    # Exact privilege lists, independent of SQL text in migrations.
    intent_insert=set("id,address_id,generation,purpose,format_version,address_lookup_key_version,address_lookup_digest,token_digest,email_ciphertext,email_nonce,email_key_version,is_current,state,created_at,expires_at,consumed_at,superseded_at,expired_at".split(","))
    intent_update=set("email_ciphertext,email_nonce,email_key_version,is_current,state,consumed_at,superseded_at,expired_at".split(","))
    user_insert=set("id,email,password_hash,created_at,updated_at,is_active,email_verified_at,registration_intent_id,quarantined_at,sessions_valid_after".split(","))
    for principal in [runtime,role]:
        for table in ["account_registration_intents","account_registration_activation","users","account_entitlements"]:
            columns=list(session.execute(text("SELECT a.attname FROM pg_catalog.pg_attribute a WHERE a.attrelid=CAST(:t AS regclass) AND a.attnum>0 AND NOT a.attisdropped"),{"t":"kalillac."+table}).scalars())
            for right in ["INSERT","UPDATE"]:
                expected=(intent_insert if right=="INSERT" else intent_update) if table=="account_registration_intents" and principal==role else set()
                if table=="users":expected=user_insert if right=="INSERT" else ({"password_hash","updated_at","is_active","email_verified_at"} if principal==runtime else set())
                if table=="account_entitlements":expected=set(columns) if principal==runtime or right=="INSERT" else set()
                for col in columns:
                    has=session.execute(text("SELECT pg_catalog.has_column_privilege(:p,:t,:c,:r)"),{"p":principal,"t":"kalillac."+table,"c":col,"r":right}).scalar_one()
                    if has!=(col in expected):raise RegistrationUnavailable()
    guards=session.execute(text("SELECT p.proname,p.prosecdef,pg_catalog.pg_get_userbyid(p.proowner),p.proconfig,pg_catalog.has_function_privilege(:r,p.oid,'EXECUTE'),pg_catalog.has_function_privilege(:a,p.oid,'EXECUTE') FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='kalillac' AND p.proname IN ('registration_intent_lifecycle_guard','registration_user_insert_guard','registration_user_identity_guard','registration_control_lock')"),{"r":runtime,"a":role}).all()
    if len(guards)!=4:raise RegistrationUnavailable()
    for name,definer,function_owner,config,r_exec,a_exec in guards:
        if not definer or function_owner!=owner or "search_path=pg_catalog, pg_temp" not in (config or []):raise RegistrationUnavailable()
        if bool(r_exec)!=(name=="registration_control_lock") or bool(a_exec)!=(name=="registration_control_lock"):raise RegistrationUnavailable()


    # SELECT/DELETE and all unaffected-table privileges are part of the boundary,
    # including PUBLIC/inherited rights. A receives no unrelated application DML.
    tables=list(session.execute(text("SELECT c.relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='kalillac' AND c.relkind IN ('r','p','v','m','f')")).scalars())
    for table in tables:
        for principal in [role,runtime]:
            if principal==runtime and table not in ("users","account_registration_intents","account_registration_activation"):continue
            for right in ["SELECT","DELETE"]:
                expected = table in ("users","account_registration_intents","account_registration_activation","account_entitlements") if right=="SELECT" else (table=="account_registration_intents" if principal==role else table=="users")
                actual=session.execute(text("SELECT pg_catalog.has_table_privilege(:p,:t,:r)"),{"p":principal,"t":"kalillac."+table,"r":right}).scalar_one()
                if bool(actual)!=expected:raise RegistrationUnavailable()
            if principal==role and table not in ("users","account_registration_intents","account_registration_activation","account_entitlements"):
                for right in ["INSERT","UPDATE","TRUNCATE","REFERENCES","TRIGGER"]:
                    if session.execute(text("SELECT pg_catalog.has_table_privilege(:p,:t,:r)"),{"p":principal,"t":"kalillac."+table,"r":right}).scalar_one():raise RegistrationUnavailable()
                for right in ["SELECT","INSERT","UPDATE","REFERENCES"]:
                    if session.execute(text("SELECT pg_catalog.has_any_column_privilege(:p,:t,:r)"),{"p":principal,"t":"kalillac."+table,"r":right}).scalar_one():raise RegistrationUnavailable()
    for principal in [role,runtime]:
        if session.execute(text("SELECT count(*) FROM pg_catalog.pg_foreign_server WHERE pg_catalog.has_server_privilege(:p,oid,'USAGE')"),{"p":principal}).scalar_one():raise RegistrationUnavailable()
    # Explicit grants can bypass role-membership checks; inspect credential
    # catalogs, server-file functions and effective grant options as well.
    for principal in (role,runtime):
        for catalog in ("pg_catalog.pg_authid","pg_catalog.pg_shadow"):
            if session.execute(text("SELECT pg_catalog.has_table_privilege(:p,:t,'SELECT') OR pg_catalog.has_any_column_privilege(:p,:t,'SELECT')"),{"p":principal,"t":catalog}).scalar_one():raise RegistrationUnavailable()
        if session.execute(text("SELECT count(*) FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='pg_catalog' AND p.proname IN ('pg_read_file','pg_read_binary_file','pg_ls_dir','pg_stat_file','lo_import','lo_export') AND pg_catalog.has_function_privilege(:p,p.oid,'EXECUTE')"),{"p":principal}).scalar_one():raise RegistrationUnavailable()
        if session.execute(text("SELECT count(*) FROM pg_catalog.pg_proc WHERE pg_catalog.has_function_privilege(:p,oid,'EXECUTE WITH GRANT OPTION')"),{"p":principal}).scalar_one():raise RegistrationUnavailable()
        for table in tables:
            for right in ("SELECT","INSERT","UPDATE","DELETE","TRUNCATE","REFERENCES","TRIGGER"):
                if session.execute(text("SELECT pg_catalog.has_table_privilege(:p,:t,:r)"),{"p":principal,"t":"kalillac."+table,"r":right+" WITH GRANT OPTION"}).scalar_one():raise RegistrationUnavailable()
                if right in ("SELECT","INSERT","UPDATE","REFERENCES") and session.execute(text("SELECT pg_catalog.has_any_column_privilege(:p,:t,:r)"),{"p":principal,"t":"kalillac."+table,"r":right+" WITH GRANT OPTION"}).scalar_one():raise RegistrationUnavailable()
    from .registration_schema import validate_security_definitions
    validate_security_definitions(session,role,runtime,owner)


def _validate_aes_gcm():
    """Bounded AES-256-GCM known-answer check with public, non-secret inputs."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
    primitive = AESGCM(bytes(32))
    nonce, plaintext = bytes(12), bytes(16)
    expected = bytes.fromhex("cea7403d4d606b6e074ec5d3baf39d18d0d1c8a799996bf0265b98b5d48ab919")
    if primitive.encrypt(nonce, plaintext, None) != expected:
        raise RegistrationUnavailable()
    if primitive.decrypt(nonce, expected, None) != plaintext:
        raise RegistrationUnavailable()
    try:
        primitive.decrypt(nonce, expected[:-1] + bytes([expected[-1] ^ 1]), None)
    except InvalidTag:
        return
    raise RegistrationUnavailable()


def bootstrap(factory,gateway,env=None):
    env=os.environ if env is None else env
    v2=enabled(env);schema_probe(factory,v2)
    if not v2:return None
    engine=None
    try:
        _validate_aes_gcm()
        if sys.platform!="linux" or env.get("KALILLAC_ACCOUNT_REGISTRATION_NONPRODUCTION","").lower() in TRUE:raise RegistrationUnavailable()
        from .request_guard import load_guard_origin
        origin=load_guard_origin(env)
        url=env.get("KALILLAC_ACCOUNT_REGISTRATION_URL","")
        if not origin or url!=origin+"/account/create/confirm":raise RegistrationUnavailable()
        deployment=env.get("KALILLAC_DEPLOYMENT_ID","")
        with factory() as s:
            with s.begin():
                receipt=_receipt(s)
                if receipt.state!="active_zero_legacy":raise RegistrationUnavailable()
                instance=str(receipt.database_instance_id)
                ordinary=s.get_bind().url
        ring=Keyring.parse(read_protected(env.get("KALILLAC_ACCOUNT_REGISTRATION_KEYRING_FILE",""),16384))
        activation_path=env.get("KALILLAC_ACCOUNT_REGISTRATION_ACTIVATION_FILE","")
        activation=strict_json(read_protected(activation_path,8192))
        evidence=strict_json(read_protected(activation_path+".evidence.json",65536))
        expected={"format_version","policy_version","database_instance_id","activation_id","cutover_id","deployment_id","schema_revision","evidence_sha256","inventory_at","activated_at","legacy_unverified_count"}
        if not isinstance(activation,dict) or set(activation)!=expected or type(activation["format_version"]) is not int or activation["format_version"]!=1 or type(activation["policy_version"]) is not int or activation["policy_version"]!=1 or type(activation["legacy_unverified_count"]) is not int or activation["legacy_unverified_count"]!=0 or activation["schema_revision"]!="0011_registration_intents":raise RegistrationUnavailable()
        evidence_fields={"query_text","query_sha256","aggregates","database_identity","schema_revision","inventory_at","cutover_id","deployment_id","writer_exclusion"}
        if not isinstance(evidence,dict) or set(evidence)!=evidence_fields:raise RegistrationUnavailable()
        _evidence_shape(activation,evidence,ordinary)
        canonical=__import__("json").dumps(evidence,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()
        if hashlib.sha256(canonical).hexdigest()!=activation["evidence_sha256"]:raise RegistrationUnavailable()
        if hashlib.sha256(evidence["query_text"].encode("utf-8")).hexdigest()!=evidence["query_sha256"]:raise RegistrationUnavailable()
        if evidence["schema_revision"]!=activation["schema_revision"] or any(evidence[k]!=activation[k] for k in ["inventory_at","cutover_id","deployment_id"]):raise RegistrationUnavailable()
        if evidence["database_identity"]["database_instance_id"]!=instance:raise RegistrationUnavailable()
        role=validate_role_name(env.get("KALILLAC_MIGRATION_REGISTRATION_ROLE","kalillac_registration"),"registration role")
        if len({role,migration_app_role(),migration_owner_role()})!=3:raise RegistrationUnavailable()
        password=_check_credential(read_protected(env.get("KALILLAC_ACCOUNT_REGISTRATION_DATABASE_CREDENTIAL_FILE",""),8192),role,instance,deployment,_ordinary_auth(factory,ordinary),ring)
        engine=create_engine(ordinary.set(username=role,password=password),pool_size=4,max_overflow=0,pool_timeout=5,pool_pre_ping=True,hide_parameters=True,
            connect_args={"connect_timeout":5,"application_name":"kalillac-registration","options":"-c statement_timeout=5000 -c lock_timeout=2000"},isolation_level="READ COMMITTED")
        @event.listens_for(engine,"checkout")
        def authenticate(connection,record,proxy):
            with connection.cursor() as cur:
                cur.execute("SELECT session_user,current_user,system_user")
                if cur.fetchone()!=(role,role,"scram-sha-256:"+role):raise RegistrationUnavailable()
            connection.rollback()
        private=sessionmaker(bind=engine,expire_on_commit=False)
        from .registration_authority import RegistrationAuthority
        authority=RegistrationAuthority(private,ring,gateway,url,role=role)
        with authority._transaction() as s:
            authority._control(s,True)
            row=_receipt(s)
            for k in ["database_instance_id","activation_id","cutover_id","deployment_id","evidence_sha256","legacy_unverified_count"]:
                value=getattr(row,k);value=str(value) if isinstance(value,uuid.UUID) else value
                if activation[k]!=value:raise RegistrationUnavailable()
            for k in ["inventory_at","activated_at"]:
                from datetime import datetime
                if datetime.fromisoformat(activation[k].replace("Z","+00:00"))!=getattr(row,k):raise RegistrationUnavailable()
            if activation["deployment_id"]!=deployment or _uuid(activation["database_instance_id"])!=instance:raise RegistrationUnavailable()
            effective_privileges(s,role,migration_app_role(),migration_owner_role())
            if s.scalar(select(func.count()).select_from(User).where(User.email_verified_at.is_(None)))!=0:raise RegistrationUnavailable()
            last=None
            while True:
                query=select(Intent).where(Intent.is_current,Intent.expires_at>func.clock_timestamp()).order_by(Intent.id).limit(250)
                if last is not None:query=query.where(Intent.id>last)
                rows=list(s.scalars(query))
                if not rows:break
                for intent in rows:ring.decrypt(intent)
                last=rows[-1].id
        authority._dispose=engine.dispose
        print("REGISTRATION_V2_READY",ring.active_encryption,ring.active_lookup)
        return authority
    except Exception:
        if engine is not None:engine.dispose()
        raise RuntimeError("REGISTRATION_CONFIG_INVALID") from None
