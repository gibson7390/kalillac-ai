"""Synthetic registration worlds; production connections and providers are forbidden."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime,timezone
import base64,json,os,uuid
from types import SimpleNamespace
from sqlalchemy import create_engine,text
from sqlalchemy.orm import sessionmaker
from kalillac_db.models import Base,AccountRegistrationActivation as Activation
from kalillac_accounts.registration_crypto import Keyring
from kalillac_accounts.registration_authority import RegistrationAuthority
from kalillac_accounts.registration_limits import RegistrationLimits
from kalillac_accounts.registration_markers import IssuanceMap
from account_test_db import make_sqlite_engine

def key_bytes():
    return json.dumps({"format_version":1,"active_encryption":"reg-enc-v1","active_lookup":"reg-lookup-v1",
        "encryption_keys":[{"version":"reg-enc-v1","key_b64":base64.b64encode(os.urandom(32)).decode()}],
        "lookup_keys":[{"version":"reg-lookup-v1","key_b64":base64.b64encode(os.urandom(32)).decode()}]}).encode()

class FakeEmail:
    def __init__(self):self.links=[];self.notices=0;self.before_send=lambda:None
    def send_registration_finish(self,email,link):
        self.before_send();self.links.append(link)
    def send_registration_notice(self,email):
        self.before_send();self.notices+=1

class World:
    def __repr__(self):return "RegistrationTestWorld()"
    def token(self):return self.gateway.links[-1].split("#token=",1)[1]

def sqlite_world(clock=None,monotonic=None):
    w=World();w.engine=make_sqlite_engine();Base.metadata.create_all(w.engine)
    w.factory=sessionmaker(bind=w.engine,expire_on_commit=False)
    with w.factory.begin() as s:s.add(Activation(singleton_id=1,database_instance_id=uuid.uuid4(),state="unactivated",policy_version=1))
    w.ring=Keyring.parse(key_bytes());w.gateway=FakeEmail()
    w.time=[datetime.now(timezone.utc)];w.mono=[0.]
    w.limits=RegistrationLimits(clock=monotonic or (lambda:w.mono[0]))
    w.markers=IssuanceMap(clock=monotonic or (lambda:w.mono[0]))
    w.authority=RegistrationAuthority(w.factory,w.ring,w.gateway,"https://registration.invalid/account/create/confirm",production=False,
        limits=w.limits,markers=w.markers,clock=clock or (lambda:w.time[0]),monotonic=monotonic or (lambda:w.mono[0]))
    return w

@contextmanager
def postgres_world(monkeypatch,*,active=True,preexisting_access=()):
    import pytest
    from sqlalchemy import make_url
    from alembic import command
    from test_account_migration import _config
    raw=os.getenv("KALILLAC_REGISTRATION_TEST_POSTGRES_URL")
    if not raw:
        if os.getenv("KALILLAC_REGISTRATION_REQUIRE_POSTGRES")=="true":pytest.fail("required disposable PostgreSQL is missing")
        pytest.skip("explicit disposable PostgreSQL required")
    url=make_url(raw)
    if url.host not in ("127.0.0.1","localhost") or url.database!="postgres":
        raise RuntimeError("NONLOCAL_POSTGRES_TEST_REFUSED")
    admin=create_engine(url,isolation_level="AUTOCOMMIT",hide_parameters=True)
    tag=uuid.uuid4().hex[:12]
    names={n:"kalillac_test_s3_"+n+"_"+tag for n in ("owner","migrate","app","registration","service","executor")}
    passwords={n:base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=") for n in ("migrate","app","registration")}
    database="kalillac_test_s3_"+tag;w=World();engines=[]
    with admin.connect() as c:
        for n,name in names.items():
            # All identifiers are fixed-prefix hex names, and generated URL-safe
            # synthetic passwords cannot contain SQL delimiters. Never print SQL.
            # The V1 service and executor roles are NOLOGIN while prepared.
            attrs="NOLOGIN" if n in ("owner","service","executor") else "LOGIN PASSWORD '"+passwords[n]+"'"
            c.exec_driver_sql(f"CREATE ROLE {name} {attrs} NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
        c.exec_driver_sql(f"GRANT {names['owner']} TO {names['migrate']}")
        # The operator's prerequisite for 0013: owner->executor, SET only.
        c.exec_driver_sql(f"GRANT {names['executor']} TO {names['owner']} WITH INHERIT FALSE, SET TRUE")
        c.exec_driver_sql(f"CREATE DATABASE {database} OWNER {names['owner']}")
        c.exec_driver_sql(f"REVOKE CONNECT ON DATABASE {database} FROM PUBLIC")
        c.exec_driver_sql(f"GRANT CONNECT ON DATABASE {database} TO {names['migrate']},{names['app']}")
        if "database" in preexisting_access:
            c.exec_driver_sql(f"GRANT CONNECT ON DATABASE {database} TO {names['registration']}")

    owner_url=url.set(database=database,username=names["migrate"],password=passwords["migrate"])
    migration=create_engine(owner_url,hide_parameters=True);engines.append(migration)
    with migration.begin() as c:
        c.exec_driver_sql("SET ROLE "+names["owner"])
        c.exec_driver_sql("CREATE SCHEMA kalillac AUTHORIZATION "+names["owner"])
        c.exec_driver_sql("GRANT USAGE ON SCHEMA kalillac TO "+names["app"])
        if "schema" in preexisting_access:
            c.exec_driver_sql("GRANT USAGE ON SCHEMA kalillac TO "+names["registration"])
        initial=tuple(c.execute(text("SELECT has_schema_privilege(:a,'kalillac','USAGE'),has_database_privilege(:a,current_database(),'CONNECT')"),{"a":names["registration"]}).one())
        assert initial==("schema" in preexisting_access,"database" in preexisting_access)

    monkeypatch.setenv("KALILLAC_MIGRATION_OWNER_ROLE",names["owner"])
    monkeypatch.setenv("KALILLAC_MIGRATION_APP_ROLE",names["app"])
    monkeypatch.setenv("KALILLAC_MIGRATION_REGISTRATION_ROLE",names["registration"])
    monkeypatch.setenv("KALILLAC_V1_SECURITY_SERVICE_ROLE",names["service"])
    monkeypatch.setenv("KALILLAC_V1_SECURITY_EXECUTOR_ROLE",names["executor"])
    try:
        with migration.begin() as c:command.upgrade(_config(connection=c),"head")
        if active:
            with migration.begin() as c:
                c.exec_driver_sql("SET ROLE "+names["owner"])
                c.execute(text("UPDATE kalillac.account_registration_activation SET state='active_zero_legacy',activation_id=:a,cutover_id=:c,deployment_id='dev-test',evidence_sha256=:e,inventory_at=clock_timestamp(),activated_at=clock_timestamp(),legacy_unverified_count=0"),{"a":uuid.uuid4(),"c":uuid.uuid4(),"e":"0"*64})
        w.engine=create_engine(url.set(database=database,username=names["registration"],password=passwords["registration"]),pool_size=4,max_overflow=0,pool_timeout=5,hide_parameters=True,isolation_level="READ COMMITTED")
        w.runtime_engine=create_engine(url.set(database=database,username=names["app"],password=passwords["app"]),hide_parameters=True)
        engines.extend([w.engine,w.runtime_engine])
        w.factory=sessionmaker(bind=w.engine,expire_on_commit=False)
        w.runtime_factory=sessionmaker(bind=w.runtime_engine,expire_on_commit=False)
        w.migration=migration;w.names=names;w.ring=Keyring.parse(key_bytes());w.gateway=FakeEmail();w.mono=[0.]
        w.limits=RegistrationLimits(clock=lambda:w.mono[0]);w.markers=IssuanceMap(clock=lambda:w.mono[0])
        w.authority=RegistrationAuthority(w.factory,w.ring,w.gateway,"https://registration.invalid/account/create/confirm",
            role=names["registration"],production=active,limits=w.limits,markers=w.markers,monotonic=lambda:w.mono[0])
        yield w
    finally:
        for engine in engines:engine.dispose()
        with admin.connect() as c:
            c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:d AND pid<>pg_backend_pid()"),{"d":database})
            c.exec_driver_sql(f"DROP DATABASE {database}")
            for n in ("migrate","registration","app","service","executor","owner"):c.exec_driver_sql("DROP ROLE "+names[n])
        admin.dispose()
