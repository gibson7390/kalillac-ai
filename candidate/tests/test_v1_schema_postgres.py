"""V1-02A prepared account-security schema on real disposable PostgreSQL 16.

Uses the registration harness (loopback/disposable only; CI's REQUIRE_POSTGRES
switch turns an absent database into a failure). Owner-role writes below seed
negative fixtures only; no runtime principal gains any authority.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import os
import re
import threading
import time
import uuid

import pytest
from alembic import command
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

import kalillac_accounts.v1_schema as v1_schema
from kalillac_accounts.registration_schema import guard_functions
from kalillac_accounts.registration_startup import effective_privileges, schema_probe
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_accounts.tokens import hash_session_token
from kalillac_accounts.v1_security import (FLAG, V1SecurityConfigError, V1SecurityUnavailable,
                                         load_v1_security, load_v1_startup)
from kalillac_db.models import AccountEmailVerificationToken, AccountRecoveryToken, AccountSession
from registration_test_support import postgres_world
from test_account_migration import _config

HEAD = "0012_v1_account_security"
LEGACY_HEAD = "0011_registration_intents"
ENV = {FLAG: "true", "KALILLAC_ACCOUNTS_ENABLED": "true", "KALILLAC_DB_ENABLED": "true"}
NEW_USER_COLUMNS = ("deletion_requested_at", "password_reset_required")
TOKEN = "synthetic-v1-02a-postgres-session"
DIGEST = hash_session_token(TOKEN)
LOGGER = "kalillac.accounts.v1"
ACTIVATE = ("UPDATE kalillac.account_security_activation SET state='active', authority_version=1,"
            " activation_id=gen_random_uuid(), deployment_id='disposable-v1', release_sha=repeat('a',40),"
            " activated_at=clock_timestamp()")


def _hex(seed):
    return hashlib.sha256(seed.encode()).hexdigest()


@contextmanager
def _world(monkeypatch, active):
    monkeypatch.delenv(FLAG, raising=False)
    with postgres_world(monkeypatch, active=active) as w:
        # Sessions that start as the NOLOGIN owner, the only reader of the latch.
        w.owner_engine = create_engine(w.migration.url, hide_parameters=True,
                                       connect_args={"options": "-c role=" + w.names["owner"]})
        w.owner_factory = sessionmaker(bind=w.owner_engine)
        try:
            yield w
        finally:
            w.owner_engine.dispose()


@pytest.fixture
def world(monkeypatch):
    with _world(monkeypatch, active=False) as w:
        yield w


@pytest.fixture
def active_world(monkeypatch):
    with _world(monkeypatch, active=True) as w:
        yield w


def migrate(w, target, down=False):
    with w.migration.begin() as c:
        (command.downgrade if down else command.upgrade)(_config(connection=c), target)


@contextmanager
def owner(w):
    with w.migration.begin() as c:
        c.exec_driver_sql("SET ROLE " + w.names["owner"])
        yield c


def classify(factory):
    with factory() as s, s.begin():
        return v1_schema.classify(s)


def version(w):
    with owner(w) as c:
        return c.scalar(text("SELECT version_num FROM kalillac.alembic_version"))


def present(w):
    with owner(w) as c:
        tables = set(c.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='kalillac'"
                                    " AND tablename=ANY(:t)"), {"t": list(v1_schema.TABLES)}))
        columns = set(c.scalars(text("SELECT column_name FROM information_schema.columns WHERE table_schema='kalillac'"
                                     " AND table_name='users' AND column_name=ANY(:c)"), {"c": list(NEW_USER_COLUMNS)}))
    return tables, columns


def snapshot(w, exclude=()):
    with owner(w) as c:
        tables = c.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname='kalillac' ORDER BY 1")).all()
        return {t: c.scalar(text(f"SELECT coalesce(json_agg(row_to_json(x) ORDER BY row_to_json(x)::text),'[]')"
                                 f" FROM kalillac.{t} x")) for t in tables if t not in exclude}


def latch(w):
    with owner(w) as c:
        return c.execute(text("SELECT row_to_json(a) FROM kalillac.account_security_activation a")).scalar_one()


def make_user(c, email, verified=True):
    uid = uuid.uuid4()
    c.execute(text("INSERT INTO kalillac.users(id,email,password_hash,is_active,email_verified_at)"
                   " VALUES (:id,:e,'synthetic-hash',true,CASE WHEN :v THEN clock_timestamp() END)"),
              {"id": uid, "e": email, "v": verified})
    return uid


def sqlstate(c, sql, params=None):
    try:
        with c.begin_nested():
            c.execute(text(sql), params or {})
    except DBAPIError as error:
        return error.orig.sqlstate
    return "accepted"


@contextmanager
def admin(w):
    """The disposable cluster's superuser, only for tampering no owner could perform."""
    url = make_url(os.environ["KALILLAC_REGISTRATION_TEST_POSTGRES_URL"]).set(database=w.migration.url.database)
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.begin() as c:
            yield c
    finally:
        engine.dispose()


@pytest.fixture
def roles(world):
    """Extra disposable roles (intermediaries), dropped before the world is."""
    made = {}

    def make(label):
        made[label] = world.names["owner"].replace("_owner_", "_" + label + "_")
        with admin(world) as c:
            c.exec_driver_sql(f"CREATE ROLE {made[label]} NOLOGIN NOINHERIT")
        return made[label]
    yield make
    with admin(world) as c:
        for name in made.values():
            # CASCADE: a trigger on another owner's table may depend on what the role owns.
            c.exec_driver_sql(f"DROP OWNED BY {name} CASCADE")
            c.exec_driver_sql(f"DROP ROLE {name}")


def run(w, statements, **names):
    """Run tamper statements; 'owner:' runs as the owner, anything else as the superuser."""
    values = {**w.names, **names}
    with admin(w) as c:
        for statement in statements:
            as_owner = statement.startswith("owner:")
            if as_owner:
                c.exec_driver_sql("SET LOCAL ROLE " + w.names["owner"])
            c.exec_driver_sql(statement.removeprefix("owner:").format(**values))
            if as_owner:
                c.exec_driver_sql("RESET ROLE")


def refused(w):
    for factory in (w.owner_factory, w.runtime_factory):
        with pytest.raises(v1_schema.V1SchemaRefused, match="inconsistent_schema"):
            classify(factory)
    with pytest.raises(V1SecurityConfigError, match=r"\(inconsistent_schema\)"):
        load_v1_startup(lambda: w.runtime_factory, ENV)


def write_attempts(engine):
    """Every V1 write a principal could attempt directly; all must be refused."""
    with engine.connect() as c:
        return {table: (sqlstate(c, f"INSERT INTO kalillac.{table} DEFAULT VALUES"),
                        sqlstate(c, f"UPDATE kalillac.{table} SET {next(iter(v1_schema.COLUMNS[table]))}=NULL"),
                        sqlstate(c, f"DELETE FROM kalillac.{table}"))
                for table in v1_schema.TABLES}


DENIED = {table: ("42501",) * 3 for table in v1_schema.TABLES}


# --- upgrade compatibility ------------------------------------------------------------------

def test_upgrade_from_exact_0011_preserves_every_row_and_sets_exact_defaults(active_world):
    w = active_world
    w.authority.issue_after_response("v102a-registered@example.test")
    w.authority.confirm_after_hash(w.authority.prevalidate(w.token()), "synthetic-hash")
    migrate(w, LEGACY_HEAD, down=True)
    assert version(w) == LEGACY_HEAD and present(w) == (set(), set())
    w.authority.issue_after_response("v102a-pending@example.test")
    with w.runtime_factory.begin() as s:
        uid = s.scalar(text("SELECT id FROM kalillac.users"))
        now = s.scalar(text("SELECT clock_timestamp()"))
        s.add_all([AccountSession(user_id=uid, token_hash=DIGEST, created_at=now, expires_at=now + timedelta(days=1)),
                   AccountRecoveryToken(user_id=uid, purpose="password_reset", token_hash=_hex("recovery"),
                                        expires_at=now + timedelta(hours=1)),
                   AccountEmailVerificationToken(user_id=uid, email_hash=_hex("email"), token_hash=_hex("verify"),
                                                 expires_at=now + timedelta(hours=1))])
    before = snapshot(w, exclude=("alembic_version",))
    assert all(before[t] != [] for t in ("users", "account_sessions", "account_recovery_tokens",
                                         "account_email_verification_tokens", "account_registration_intents"))
    migrate(w, HEAD)
    after = snapshot(w, exclude=("alembic_version", *v1_schema.TABLES))
    for row in after["users"]:
        assert (row.pop("deletion_requested_at"), row.pop("password_reset_required")) == (None, False)
    assert after == before
    with owner(w) as c:
        instance, state = c.execute(text("SELECT database_instance_id::text,state FROM kalillac.account_registration_activation")).one()
    assert (latch(w)["database_instance_id"], latch(w)["state"], state) == (instance, "prepared", "active_zero_legacy")
    assert version(w) == HEAD and classify(w.owner_factory) == v1_schema.PREPARED


def test_states_are_distinguishable_and_the_runtime_cannot_read_the_latch(world):
    w = world
    assert classify(w.owner_factory) == v1_schema.PREPARED
    with pytest.raises(v1_schema.V1SchemaRefused, match="authority_state_unreadable"):
        classify(w.runtime_factory)
    migrate(w, LEGACY_HEAD, down=True)
    assert classify(w.owner_factory) == classify(w.runtime_factory) == v1_schema.LEGACY
    migrate(w, HEAD)
    with owner(w) as c:
        c.exec_driver_sql(ACTIVATE)
    assert classify(w.owner_factory) == v1_schema.ACTIVE


WRITER = "CREATE FUNCTION kalillac.v1_writer() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$"


@pytest.mark.parametrize("tamper", [
    ("DROP TABLE kalillac.account_outbox",),
    ("ALTER TABLE kalillac.account_security_events ADD COLUMN payload text",),
    ("GRANT SELECT ON kalillac.account_security_events TO {app}",),
    ("GRANT INSERT ON kalillac.account_outbox TO PUBLIC",),
    ("GRANT UPDATE (password_reset_required) ON kalillac.users TO {app}",),
    ("GRANT UPDATE ON kalillac.users TO {app}",),
    ("ALTER TABLE kalillac.account_security_admission DROP CONSTRAINT ck_account_security_admission_slot",),
    ("ALTER TABLE kalillac.account_security_admission ALTER COLUMN last_seen_at SET DEFAULT clock_timestamp()",),
    ("CREATE INDEX ix_extra ON kalillac.account_outbox (user_id)",),
    (WRITER,),
    (WRITER, "CREATE TRIGGER trg_v1_writer AFTER INSERT ON kalillac.account_sessions"
             " FOR EACH ROW EXECUTE FUNCTION kalillac.v1_writer()"),
    ("CREATE RULE v1_writer AS ON INSERT TO kalillac.account_sessions DO ALSO INSERT INTO kalillac.account_security_events"
     " SELECT gen_random_uuid(), NEW.user_id, 'session_revoke_all', clock_timestamp()",),
    ("DELETE FROM kalillac.account_security_activation",),
])
def test_classifier_refuses_partial_privileged_or_writable_schema(world, tamper):
    w = world
    with owner(w) as c:
        for statement in tamper:
            c.exec_driver_sql(statement.format(app=w.names["app"]))
    # The runtime cannot read the latch, so a missing latch row is unreadable to it.
    runtime = "authority_state_unreadable" if tamper[0].startswith("DELETE") else "inconsistent_schema"
    for factory, reason in ((w.owner_factory, "inconsistent_schema"), (w.runtime_factory, runtime)):
        with pytest.raises(v1_schema.V1SchemaRefused, match=reason):
            classify(factory)
    with pytest.raises(V1SecurityConfigError, match=r"\(" + runtime + r"\)"):
        load_v1_startup(lambda: w.runtime_factory, ENV)


EVENTS_USER_INDEX = ("DROP INDEX kalillac.ix_account_security_events_user",
                     "CREATE INDEX ix_account_security_events_user ON kalillac.account_security_events ")
EVENT_TYPE_CHECK = ("ALTER TABLE kalillac.account_security_events DROP CONSTRAINT ck_account_security_events_type,"
                    " ADD CONSTRAINT ck_account_security_events_type CHECK (event_type IN"
                    " ('session_revoke_others','session_revoke_all','password_reset'")
NOOP_GUARD = (" RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp"
              " AS $g$BEGIN RETURN NEW; END $g$")


# Same names, different PostgreSQL definitions: every one must be refused.
@pytest.mark.parametrize("tamper", [
    pytest.param(["owner:CREATE OR REPLACE FUNCTION kalillac.registration_control_lock(exclusive boolean) RETURNS void"
                  " LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $w$BEGIN"
                  " INSERT INTO kalillac.account_security_admission_aggregate"
                  " VALUES ('manual',ARRAY[]::timestamptz[],clock_timestamp()); END $w$"], id="definer-writer-same-name"),
    pytest.param(["owner:DROP FUNCTION kalillac.registration_control_lock(boolean)",
                  "owner:CREATE FUNCTION kalillac.registration_control_lock{lock}",
                  "owner:REVOKE ALL ON FUNCTION kalillac.registration_control_lock(integer) FROM PUBLIC",
                  "owner:GRANT EXECUTE ON FUNCTION kalillac.registration_control_lock(integer) TO {app},{registration}"],
                 id="function-signature"),
    pytest.param(["owner:CREATE OR REPLACE FUNCTION kalillac.registration_user_identity_guard()" + NOOP_GUARD],
                 id="function-body"),
    pytest.param(["ALTER FUNCTION kalillac.registration_user_insert_guard() OWNER TO {migrate}"], id="function-owner"),
    pytest.param(["owner:ALTER FUNCTION kalillac.registration_user_insert_guard() SECURITY INVOKER"],
                 id="function-security-invoker"),
    pytest.param(["owner:ALTER FUNCTION kalillac.registration_control_lock(boolean) STABLE"], id="function-volatility"),
    pytest.param(["owner:" + EVENTS_USER_INDEX[0], "owner:" + EVENTS_USER_INDEX[1] + "(occurred_at)"],
                 id="index-wrong-column"),
    pytest.param(["owner:ALTER TABLE kalillac.account_outbox DROP CONSTRAINT pk_account_outbox,"
                  " ADD CONSTRAINT pk_account_outbox PRIMARY KEY (task_type, operation_id)"], id="index-reordered"),
    pytest.param(["owner:" + EVENTS_USER_INDEX[0],
                  "owner:" + EVENTS_USER_INDEX[1] + "(user_id) WHERE event_type <> 'password_reset'"], id="index-partial"),
    pytest.param(["owner:" + EVENTS_USER_INDEX[0], "owner:" + EVENTS_USER_INDEX[1] + "((user_id::text))"],
                 id="index-expression"),
    pytest.param(["owner:" + EVENTS_USER_INDEX[0], "owner:" + EVENTS_USER_INDEX[1] + "USING hash (user_id)"],
                 id="index-method"),
    pytest.param(["owner:" + EVENTS_USER_INDEX[0], "owner:" + EVENTS_USER_INDEX[1] + "(user_id DESC)"],
                 id="index-ordering"),
    pytest.param(["UPDATE pg_catalog.pg_index SET indisvalid=false"
                  " WHERE indexrelid='kalillac.ix_account_security_events_user'::regclass"], id="index-invalid"),
    pytest.param(["UPDATE pg_catalog.pg_index SET indisready=false"
                  " WHERE indexrelid='kalillac.ix_account_security_events_user'::regclass"], id="index-not-ready"),
    pytest.param(["owner:ALTER TABLE kalillac.account_security_admission_aggregate SET UNLOGGED"], id="table-unlogged"),
    pytest.param(["owner:ALTER TABLE kalillac.account_outbox DROP COLUMN created_at",
                  "owner:ALTER TABLE kalillac.account_outbox ADD COLUMN created_at timestamptz NOT NULL"],
                 id="column-dropped-and-readded"),
    pytest.param(["owner:ALTER TABLE kalillac.account_security_events ALTER COLUMN event_type"
                  " TYPE varchar(32) COLLATE \"C\""], id="column-collation"),
    pytest.param(["owner:" + EVENT_TYPE_CHECK + ",'login'))"], id="check-widened"),
    pytest.param(["owner:" + EVENT_TYPE_CHECK + ")) NOT VALID"], id="check-not-valid"),
    pytest.param(["owner:ALTER TABLE kalillac.account_outbox ALTER CONSTRAINT fk_account_outbox_operation_account"
                  " DEFERRABLE INITIALLY DEFERRED"], id="foreign-key-deferred"),
    pytest.param(["owner:ALTER TABLE kalillac.account_security_events DROP CONSTRAINT fk_account_security_events_user,"
                  " ADD CONSTRAINT fk_account_security_events_user FOREIGN KEY (user_id) REFERENCES kalillac.users(id)"
                  " ON DELETE CASCADE"], id="foreign-key-cascading-delete"),
    pytest.param(["owner:ALTER TABLE kalillac.users DISABLE TRIGGER trg_users_registration_identity"],
                 id="trigger-disabled"),
    pytest.param(["owner:ALTER TABLE kalillac.users ENABLE TRIGGER trg_users_registration_identity"],
                 id="trigger-origin-only"),
    pytest.param(["owner:DROP TRIGGER trg_users_registration_identity ON kalillac.users",
                  "owner:CREATE TRIGGER trg_users_registration_identity BEFORE UPDATE OF email ON kalillac.users"
                  " FOR EACH ROW EXECUTE FUNCTION kalillac.registration_user_identity_guard()",
                  "owner:ALTER TABLE kalillac.users ENABLE ALWAYS TRIGGER trg_users_registration_identity"],
                 id="trigger-events"),
    pytest.param(["CREATE FUNCTION public.v1_ddl_hook() RETURNS event_trigger LANGUAGE plpgsql AS $e$BEGIN END $e$",
                  "CREATE EVENT TRIGGER v1_ddl_hook ON ddl_command_end EXECUTE FUNCTION public.v1_ddl_hook()"],
                 id="event-trigger"),
])
def test_classifier_validates_canonical_definitions_not_names(world, tamper):
    w = world
    assert classify(w.owner_factory) == v1_schema.PREPARED
    lock = guard_functions(w.names["registration"], w.names["migrate"])["registration_control_lock"]
    run(w, tamper, lock=lock.replace("(exclusive boolean)", "(exclusive integer)"))
    refused(w)


def test_classifier_refuses_a_temporary_replacement_even_in_its_own_session(world):
    w = world
    with w.owner_engine.connect() as c:
        replacement = c.begin()
        try:
            c.exec_driver_sql("DROP TABLE kalillac.account_security_admission_aggregate")
            c.exec_driver_sql("CREATE TEMPORARY TABLE account_security_admission_aggregate (admission_class varchar(16)"
                              " PRIMARY KEY, charges timestamptz[] NOT NULL, last_seen_at timestamptz NOT NULL)")
            # The replacement is visible here, first on a pg_temp search path.
            c.exec_driver_sql("SET LOCAL search_path=pg_temp,kalillac")
            assert c.scalar(text("SELECT count(*) FROM account_security_admission_aggregate")) == 0
            with Session(bind=c) as s, pytest.raises(v1_schema.V1SchemaRefused, match="inconsistent_schema"):
                v1_schema.classify(s)
        finally:
            replacement.rollback()
    assert classify(w.owner_factory) == v1_schema.PREPARED


@pytest.mark.parametrize("state", ["legacy", "prepared", "active"])
def test_v1_on_startup_refuses_every_state_read_only(world, state):
    w = world
    if state == "legacy":
        migrate(w, LEGACY_HEAD, down=True)
    if state == "active":
        with owner(w) as c:
            c.exec_driver_sql(ACTIVATE)
    before, statements = snapshot(w), []
    record = lambda conn, cursor, sql, *a: statements.append(sql.split()[0].upper())
    expected = {"legacy": ("schema_not_prepared", "schema_not_prepared"),
                "prepared": ("authority_state_unreadable", "prepared_inactive"),
                "active": ("authority_state_unreadable", "authority_catalog_unqualified")}[state]
    for factory, reason in zip((w.runtime_factory, w.owner_factory), expected):
        engine = factory.kw["bind"]
        event.listen(engine, "before_cursor_execute", record)
        try:
            with pytest.raises(V1SecurityConfigError) as error:
                load_v1_startup(lambda: factory, ENV)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert str(error.value).endswith("(" + reason + ").")
        assert engine.pool.checkedout() == 0
    assert statements and set(statements) <= {"SET", "SELECT"}
    assert snapshot(w) == before


# --- authority boundary --------------------------------------------------------------------

def test_runtime_registration_and_public_hold_no_security_authority(world):
    w = world
    principals = (w.names["app"], w.names["registration"], "public")
    with owner(w) as c:
        for principal in principals:
            for table in v1_schema.TABLES:
                for right in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                    assert not c.scalar(text("SELECT has_table_privilege(:p,:t,:r)"),
                                        {"p": principal, "t": "kalillac." + table, "r": right}), (principal, table, right)
            for column in NEW_USER_COLUMNS:
                for right in ("INSERT", "UPDATE"):
                    assert not c.scalar(text("SELECT has_column_privilege(:p,'kalillac.users',:c,:r)"),
                                        {"p": principal, "c": column, "r": right}), (principal, column, right)
    for engine in (w.runtime_engine, w.engine):
        with engine.connect() as c:
            for table in v1_schema.TABLES:
                assert sqlstate(c, f"INSERT INTO kalillac.{table} DEFAULT VALUES") == "42501", table
                assert sqlstate(c, f"DELETE FROM kalillac.{table}") == "42501", table
            for column, value in (("password_reset_required", "true"), ("deletion_requested_at", "clock_timestamp()")):
                assert sqlstate(c, f"UPDATE kalillac.users SET {column}={value}") == "42501", column


PRINCIPALS = {"app": "runtime_engine", "registration": "engine"}
# Owner authority by membership; {p} is R or A.
OWNER_ROUTES = {
    "direct": ["GRANT {owner} TO {p}"],
    "inherited": ["GRANT {owner} TO {p} WITH INHERIT TRUE"],
    "set-role-without-inherit": ["GRANT {owner} TO {p} WITH INHERIT FALSE, SET TRUE"],
    "multi-hop": ["GRANT {owner} TO {hop2} WITH INHERIT TRUE", "GRANT {hop2} TO {hop1} WITH INHERIT TRUE",
                  "GRANT {hop1} TO {p} WITH INHERIT TRUE"],
}


@pytest.mark.parametrize("route", list(OWNER_ROUTES))
@pytest.mark.parametrize("principal", list(PRINCIPALS))
def test_owner_authority_reachable_by_runtime_or_registration_is_refused(world, roles, principal, route):
    w = world
    p, engine = w.names[principal], getattr(w, PRINCIPALS[principal])
    names = {"p": p, "hop1": roles("hop1"), "hop2": roles("hop2")}
    run(w, OWNER_ROUTES[route], **names)
    refused(w)
    if route == "set-role-without-inherit":
        # Invisible to a write probe: nothing is inherited, yet SET ROLE reaches the owner.
        assert write_attempts(engine) == DENIED
    run(w, ["REVOKE {hop1} FROM {p}" if route == "multi-hop" else "REVOKE {owner} FROM {p}"], **names)
    assert write_attempts(engine) == DENIED
    assert classify(w.owner_factory) == v1_schema.PREPARED


# Effective rights without owner membership: PUBLIC, intermediary roles, column,
# schema, sequence and function privileges, and latent owner default privileges.
RIGHT_ROUTES = {
    "public-table-write": ["GRANT INSERT ON kalillac.account_security_events TO PUBLIC"],
    "intermediary-table-write": ["GRANT UPDATE ON kalillac.account_security_activation TO {hop1}",
                                 "GRANT {hop1} TO {p} WITH INHERIT TRUE"],
    "intermediary-column-write": ["GRANT UPDATE (password_reset_required) ON kalillac.users TO {hop1}",
                                  "GRANT {hop1} TO {p} WITH INHERIT TRUE"],
    "schema-create": ["GRANT CREATE ON SCHEMA kalillac TO {p}"],
    "sequence-write": ["GRANT USAGE, UPDATE ON SEQUENCE kalillac.account_registration_activation_singleton_id_seq TO {p}"],
    "definer-function": ["owner:CREATE FUNCTION public.v1_bridge() RETURNS void LANGUAGE sql SECURITY DEFINER"
                         " AS 'SELECT 1'"],
    "owner-default-privileges": ["owner:ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA kalillac"
                                 " GRANT INSERT ON TABLES TO {p}"],
}


@pytest.mark.parametrize("route", list(RIGHT_ROUTES))
@pytest.mark.parametrize("principal", list(PRINCIPALS))
def test_effective_rights_reachable_by_runtime_or_registration_are_refused(world, roles, principal, route):
    w = world
    run(w, RIGHT_ROUTES[route], p=w.names[principal], hop1=roles("hop1"))
    refused(w)


BRIDGE_VIEW = "owner:CREATE VIEW {view} AS SELECT * FROM kalillac.account_security_activation"
BRIDGE_GRANT = "GRANT SELECT, UPDATE ON {view} TO {grantee}"
SHIFT = "UPDATE {view} SET prepared_at = prepared_at - interval '1 day'"
# A view runs with its owner's rights and a rule action with its table owner's:
# (setup, its objects, the principal that writes through it, that write).
BRIDGES = {
    "view-granted-to-runtime": ([BRIDGE_VIEW, "GRANT USAGE ON SCHEMA public TO PUBLIC", BRIDGE_GRANT],
                                {"view": "public.v1_bridge", "grantee": "app"}, "app", SHIFT),
    "view-granted-to-registration": ([BRIDGE_VIEW, "GRANT USAGE ON SCHEMA public TO PUBLIC", BRIDGE_GRANT],
                                     {"view": "public.v1_bridge", "grantee": "registration"}, "registration", SHIFT),
    "view-granted-to-public": ([BRIDGE_VIEW, "GRANT USAGE ON SCHEMA public TO PUBLIC", BRIDGE_GRANT],
                               {"view": "public.v1_bridge", "grantee": "PUBLIC"}, "app", SHIFT),
    "view-granted-to-intermediary": ([BRIDGE_VIEW, "GRANT USAGE ON SCHEMA public TO PUBLIC", BRIDGE_GRANT,
                                      "GRANT {hop1} TO {app} WITH INHERIT TRUE"],
                                     {"view": "public.v1_bridge", "grantee": "hop1"}, "app", SHIFT),
    "nested-view": (["owner:CREATE VIEW public.v1_inner AS SELECT * FROM kalillac.account_security_activation",
                     "owner:CREATE VIEW {view} AS SELECT * FROM public.v1_inner", BRIDGE_GRANT],
                    {"view": "public.v1_outer", "grantee": "registration"}, "registration", SHIFT),
    "view-in-another-schema": (["owner:CREATE SCHEMA v1_elsewhere", BRIDGE_VIEW,
                                "GRANT USAGE ON SCHEMA v1_elsewhere TO {app}", BRIDGE_GRANT],
                               {"view": "v1_elsewhere.v1_bridge", "grantee": "app"}, "app", SHIFT),
    "rule-relay": (["owner:CREATE TABLE public.v1_inbox (shift interval)",
                    "owner:CREATE RULE v1_relay AS ON INSERT TO public.v1_inbox DO INSTEAD"
                    " UPDATE kalillac.account_security_activation SET prepared_at = prepared_at - NEW.shift",
                    "GRANT INSERT ON public.v1_inbox TO {app}"],
                   {}, "app", "INSERT INTO public.v1_inbox VALUES (interval '1 day')"),
}


def bridge_state(w):
    with admin(w) as c:
        return (c.execute(text("SELECT n.nspname,c.relname,c.relkind,c.relacl::text FROM pg_class c"
                               " JOIN pg_namespace n ON n.oid=c.relnamespace"
                               " WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') ORDER BY 1,2")).all(),
                c.execute(text("SELECT ev_class::regclass::text,rulename FROM pg_rewrite WHERE ev_class>=16384"
                               " ORDER BY 1,2")).all(),
                c.execute(text("SELECT nspname,nspacl::text FROM pg_namespace ORDER BY 1")).all(),
                c.execute(text("SELECT roleid::regrole::text,member::regrole::text FROM pg_auth_members"
                               " ORDER BY 1,2")).all())


@pytest.mark.parametrize("bridge", list(BRIDGES))
def test_view_and_rule_bridges_into_protected_state_are_refused_read_only(world, roles, bridge):
    w = world
    setup, objects, principal, write = BRIDGES[bridge]
    hop1 = roles("hop1")
    roles_by_key = {**w.names, "hop1": hop1}
    objects = {key: roles_by_key.get(value, value) for key, value in objects.items()}
    run(w, setup, hop1=hop1, **objects)
    # The bridge is real: the principal itself changes the protected latch.
    before = latch(w)
    with getattr(w, PRINCIPALS[principal]).begin() as c:
        c.exec_driver_sql(write.format(**objects))
    changed = latch(w)
    assert changed["prepared_at"] != before["prepared_at"]
    # Classification refuses it, reading only: bridge, grants and latch stay as they are.
    state, statements = bridge_state(w), []
    record = lambda conn, cursor, sql, *a: statements.append(sql.split()[0].upper())
    engines = (w.owner_engine, w.runtime_engine)
    for engine in engines:
        event.listen(engine, "before_cursor_execute", record)
    try:
        refused(w)
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", record)
    assert statements and set(statements) <= {"SET", "SELECT"}
    assert bridge_state(w) == state and latch(w) == changed
    # Without the bridge (and the intermediary membership) the same database qualifies again.
    run(w, ["DROP VIEW IF EXISTS public.v1_outer, public.v1_bridge, public.v1_inner",
            "DROP SCHEMA IF EXISTS v1_elsewhere CASCADE", "DROP TABLE IF EXISTS public.v1_inbox",
            "REVOKE {hop1} FROM {app}"], hop1=hop1)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def definer_state(w):
    with admin(w) as c:
        return (bridge_state(w),
                c.execute(text("SELECT p.oid::regprocedure::text,pg_get_userbyid(p.proowner),p.prosecdef,p.proacl::text"
                               " FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace"
                               " WHERE n.nspname NOT IN ('pg_catalog','information_schema') ORDER BY 1")).all(),
                c.execute(text("SELECT tgname,tgrelid::regclass::text,tgenabled FROM pg_trigger"
                               " WHERE NOT tgisinternal ORDER BY 1,2")).all(),
                memberships(w))


def install_hook(w, body, principal, owner_role, function_schema="public", table_schema="public"):
    """An unreviewed SECURITY DEFINER trigger function owned by owner_role, in
    function_schema, fired by inserts the principal may make into a table in
    table_schema; nobody holds EXECUTE on it, not even PUBLIC."""
    run(w, [f"CREATE SCHEMA IF NOT EXISTS {function_schema}", f"CREATE SCHEMA IF NOT EXISTS {table_schema}",
            f"CREATE FUNCTION {function_schema}.v1_hook() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER"
            " SET search_path=pg_catalog,pg_temp AS $h$BEGIN " + body + "; RETURN NEW; END $h$",
            f"ALTER FUNCTION {function_schema}.v1_hook() OWNER TO {{hook_owner}}",
            f"REVOKE ALL ON FUNCTION {function_schema}.v1_hook() FROM PUBLIC",
            f"CREATE TABLE {table_schema}.v1_inbox (note text)",
            f"CREATE TRIGGER v1_hook AFTER INSERT ON {table_schema}.v1_inbox"
            f" FOR EACH ROW EXECUTE FUNCTION {function_schema}.v1_hook()",
            f"GRANT USAGE ON SCHEMA {table_schema} TO {{p}}", f"GRANT INSERT ON {table_schema}.v1_inbox TO {{p}}"],
        hook_owner=owner_role, p=w.names[principal])
    with admin(w) as c:
        assert not c.scalar(text("SELECT has_function_privilege(:p,:f,'EXECUTE')"),
                            {"p": w.names[principal], "f": function_schema + ".v1_hook()"})
    return f"INSERT INTO {table_schema}.v1_inbox VALUES ('x')"


LATCH_SET = "UPDATE kalillac.account_security_activation SET prepared_at = clock_timestamp() - interval '1 day'"
KALILLAC_USAGE = "GRANT USAGE ON SCHEMA kalillac TO {m}"
# How the function owner M holds protected authority: (grants, the function's
# write, what it changes, or None where the route is refused conservatively).
DEFINER_AUTHORITY = {
    "owner-inherited": (["GRANT {owner} TO {m} WITH INHERIT TRUE"], LATCH_SET, "latch"),
    "owner-multi-hop": (["GRANT {owner} TO {hop1} WITH INHERIT TRUE", "GRANT {hop1} TO {m} WITH INHERIT TRUE"],
                        LATCH_SET, "latch"),
    "activation-update-grant": ([KALILLAC_USAGE, "GRANT UPDATE ON kalillac.account_security_activation TO {m}"],
                                LATCH_SET, "latch"),
    "reset-column-update-grant": ([KALILLAC_USAGE, "GRANT UPDATE (password_reset_required) ON kalillac.users TO {m}"],
                                  "UPDATE kalillac.users SET password_reset_required = true", "users"),
    "deletion-column-update-grant": ([KALILLAC_USAGE, "GRANT UPDATE (deletion_requested_at) ON kalillac.users TO {m}"],
                                     "UPDATE kalillac.users SET deletion_requested_at = clock_timestamp()", "users"),
    # A definer function cannot SET ROLE, so this route cannot write; refused
    # conservatively all the same.
    "owner-set-only": (["GRANT {owner} TO {m} WITH INHERIT FALSE, SET TRUE"], LATCH_SET, None),
}


@pytest.mark.parametrize("authority,principal,schemas", [
    pytest.param("owner-inherited", "app", ("public", "public"), id="inherits-owner-runtime"),
    pytest.param("owner-inherited", "registration", ("public", "public"), id="inherits-owner-registration"),
    pytest.param("owner-multi-hop", "app", ("public", "public"), id="multi-hop-owner"),
    pytest.param("activation-update-grant", "registration", ("public", "public"), id="activation-update-grant"),
    pytest.param("reset-column-update-grant", "app", ("public", "public"), id="reset-column-update-grant"),
    pytest.param("deletion-column-update-grant", "registration", ("public", "public"),
                 id="deletion-column-update-grant"),
    pytest.param("owner-inherited", "app", ("v1_hooks", "v1_inboxes"), id="function-and-table-in-other-schemas"),
    pytest.param("owner-set-only", "app", ("public", "public"), id="set-only-owner-conservative"),
])
def test_definer_trigger_functions_with_protected_authority_are_refused(world, roles, authority, principal, schemas):
    w = world
    grants, write, changes = DEFINER_AUTHORITY[authority]
    m, hop1 = roles("hook"), roles("hop1")
    with owner(w) as c:
        make_user(c, "v102a-hooked@example.test")
    run(w, grants, m=m, hop1=hop1)
    insert = install_hook(w, write, principal, m, *schemas)
    state = lambda: (latch(w)["prepared_at"], snapshot(w, exclude=v1_schema.TABLES)["users"])
    before = state()
    with getattr(w, PRINCIPALS[principal]).connect() as c:
        outcome = sqlstate(c, insert)
        c.commit()
    if changes:
        # The route is real: R or A, with no EXECUTE, changes protected state.
        assert outcome == "accepted" and state() != before
        assert (state()[0] != before[0]) == (changes == "latch")
    else:
        assert outcome == "42501" and state() == before
    # Qualification refuses it, reading only and changing nothing.
    frozen, statements = definer_state(w), []
    record = lambda conn, cursor, sql, *a: statements.append(sql.split()[0].upper())
    for engine in (w.owner_engine, w.runtime_engine):
        event.listen(engine, "before_cursor_execute", record)
    try:
        refused(w)
    finally:
        for engine in (w.owner_engine, w.runtime_engine):
            event.remove(engine, "before_cursor_execute", record)
    assert statements and set(statements) <= {"SET", "SELECT"} and definer_state(w) == frozen
    # Without the unreviewed function (and M's direct grants, which the
    # owner-only ACL check refuses on their own) the canonical guards and
    # database qualify.
    run(w, [f"DROP FUNCTION {schemas[0]}.v1_hook() CASCADE",
            "REVOKE ALL ON kalillac.account_security_activation FROM {m}",
            "REVOKE ALL (password_reset_required, deletion_requested_at) ON kalillac.users FROM {m}",
            "REVOKE ALL ON SCHEMA kalillac FROM {m}"], m=m)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def test_definer_trigger_function_without_protected_authority_is_not_refused(world, roles):
    w = world
    insert = install_hook(w, "PERFORM 1", "app", roles("hook"))
    with w.runtime_engine.connect() as c:
        assert sqlstate(c, insert) == "accepted"
    assert classify(w.owner_factory) == v1_schema.PREPARED
    with pytest.raises(v1_schema.V1SchemaRefused, match="authority_state_unreadable"):
        classify(w.runtime_factory)


def test_no_writer_exists_and_v1_off_legacy_routes_leave_security_state_untouched(world):
    w = world
    with owner(w) as c:
        assert not c.scalar(text("SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid"
                                 " JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='kalillac'"
                                 " AND r.relname=ANY(:t) AND NOT t.tgisinternal"), {"t": list(v1_schema.TABLES)})
        assert set(c.scalars(text("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace"
                                  " WHERE n.nspname='kalillac'"))) == v1_schema.FUNCTIONS
        assert not c.scalar(text("SELECT count(*) FROM pg_rewrite r JOIN pg_class t ON t.oid=r.ev_class JOIN pg_namespace n"
                                 " ON n.oid=t.relnamespace WHERE n.nspname='kalillac' AND r.rulename<>'_RETURN'"))
    before, statements = latch(w), []
    record = lambda conn, cursor, sql, *a: statements.append(sql)
    api = FastAPI()
    api.include_router(build_account_router(lambda: w.runtime_factory, AccountSettings()))
    event.listen(w.runtime_engine, "before_cursor_execute", record)
    try:
        with TestClient(api, base_url="https://testserver") as client:
            body = {"email": "v102a-legacy@example.test", "password": "synthetic-password-for-legacy"}
            assert client.post("/api/account/register", json=body).status_code == 201
            assert client.post("/api/account/login", json=body).status_code == 200
            assert client.get("/api/account/me").json()["user"]["email"] == body["email"]
            assert client.get("/api/account/entitlements").json()["entitlements"]["tier"] == "free"
            assert client.post("/api/account/logout").json() == {"ok": True}
            assert client.get("/api/account/me").status_code == 401
    finally:
        event.remove(w.runtime_engine, "before_cursor_execute", record)
    writes = [s for s in statements if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert writes and not any(t in s for s in statements for t in v1_schema.TABLES)
    assert not any(column in s for s in writes for column in NEW_USER_COLUMNS)
    with owner(w) as c:
        assert all(c.scalar(text(f"SELECT count(*) FROM kalillac.{t}")) == 0 for t in v1_schema.TABLES
                   if t != "account_security_activation")
        assert c.execute(text("SELECT deletion_requested_at,password_reset_required FROM kalillac.users")).one() == (None, False)
    assert latch(w) == before and w.runtime_engine.pool.checkedout() == 0


def test_constraints_reject_invalid_oversized_and_cross_account_rows(world):
    w = world
    with owner(w) as c:
        first, second = make_user(c, "v102a-first@example.test"), make_user(c, "v102a-second@example.test")
        op = uuid.uuid4()
        stamps = lambda n: "ARRAY[" + ",".join(["clock_timestamp()"] * n) + "]::timestamptz[]"
        event_sql = "INSERT INTO kalillac.account_security_events VALUES (:op,:u,:t,clock_timestamp())"
        outbox_sql = "INSERT INTO kalillac.account_outbox VALUES (:op,:t,:u,clock_timestamp())"
        admit = lambda slot, manual, recovery="'{}'": (f"INSERT INTO kalillac.account_security_admission VALUES"
                                                       f" ({slot},:u,{manual},{recovery},clock_timestamp())")
        aggregate = "INSERT INTO kalillac.account_security_admission_aggregate VALUES (:k,{},clock_timestamp())"
        cases = [
            ("event ok", event_sql, {"op": op, "u": first, "t": "session_revoke_all"}, "accepted"),
            ("event duplicate operation", event_sql, {"op": op, "u": second, "t": "password_reset"}, "23505"),
            ("event unknown type", event_sql, {"op": uuid.uuid4(), "u": first, "t": "login"}, "23514"),
            ("event oversized type", event_sql, {"op": uuid.uuid4(), "u": first, "t": "x" * 33}, "22001"),
            ("event unknown account", event_sql, {"op": uuid.uuid4(), "u": uuid.uuid4(), "t": "password_reset"}, "23503"),
            ("event missing time", "INSERT INTO kalillac.account_security_events VALUES (:op,:u,'password_reset',NULL)",
             {"op": uuid.uuid4(), "u": first}, "23502"),
            ("outbox ok", outbox_sql, {"op": op, "u": first, "t": "notification"}, "accepted"),
            ("outbox duplicate task", outbox_sql, {"op": op, "u": first, "t": "notification"}, "23505"),
            ("outbox cross-account", outbox_sql, {"op": op, "u": second, "t": "reconciliation"}, "23503"),
            ("outbox without operation", outbox_sql, {"op": uuid.uuid4(), "u": first, "t": "notification"}, "23503"),
            ("outbox token delivery", outbox_sql, {"op": op, "u": first, "t": "recovery_email"}, "23514"),
            ("admission ok", admit(4095, stamps(6), stamps(6)), {"u": first}, "accepted"),
            ("admission duplicate account", admit(0, "'{}'"), {"u": first}, "23505"),
            ("admission slot above 4095", admit(4096, "'{}'"), {"u": second}, "23514"),
            ("admission negative slot", admit(-1, "'{}'"), {"u": second}, "23514"),
            ("admission seventh manual charge", admit(1, stamps(7)), {"u": second}, "23514"),
            ("admission seventh recovery charge", admit(1, "'{}'", stamps(7)), {"u": second}, "23514"),
            ("admission null charge", admit(1, "ARRAY[NULL]::timestamptz[]"), {"u": second}, "23514"),
            ("admission two-dimensional ring", admit(1, "ARRAY[[clock_timestamp()]]::timestamptz[]"), {"u": second}, "23514"),
            ("admission offset ring", admit(1, "'[0:0]={2026-01-01}'::timestamptz[]"), {"u": second}, "23514"),
            ("admission unknown account", admit(1, "'{}'"), {"u": uuid.uuid4()}, "23503"),
            ("aggregate ok", aggregate.format(stamps(60)), {"k": "manual"}, "accepted"),
            ("aggregate sixty-first charge", aggregate.format(stamps(61)), {"k": "recovery"}, "23514"),
            ("aggregate unknown class", aggregate.format("'{}'"), {"k": "billing"}, "23514"),
            ("latch second row", "INSERT INTO kalillac.account_security_activation(singleton_id,schema_version,"
             "database_instance_id,state,prepared_at) SELECT 2,1,gen_random_uuid(),'prepared',clock_timestamp()", {}, "23514"),
            ("latch future schema", "UPDATE kalillac.account_security_activation SET schema_version=2", {}, "23514"),
            ("latch active without binding", "UPDATE kalillac.account_security_activation SET state='active'", {}, "23514"),
            ("latch invalid release", ACTIVATE.replace("repeat('a',40)", "repeat('A',40)"), {}, "23514"),
            ("latch foreign instance", "UPDATE kalillac.account_security_activation SET database_instance_id=gen_random_uuid()",
             {}, "23503"),
            ("users null reset gate", "UPDATE kalillac.users SET password_reset_required=NULL", {}, "23502"),
            ("users delete with security history", "DELETE FROM kalillac.users WHERE id=:u", {"u": first}, "23503"),
        ]
        results = [(name, sqlstate(c, sql, params)) for name, sql, params, _ in cases]
    assert results == [(name, expected) for name, _, _, expected in cases]


ACTIVE_BINDING = {"authority_version": "1", "activation_id": "gen_random_uuid()",
                  "deployment_id": "'disposable-v1'", "release_sha": "repeat('a',40)",
                  "activated_at": "clock_timestamp()"}


def activate(**overrides):
    values = {**ACTIVE_BINDING, **overrides}
    return ("UPDATE kalillac.account_security_activation SET state='active',"
            + ",".join(f"{field}={value}" for field, value in values.items()))


@pytest.mark.parametrize("field", list(ACTIVE_BINDING))
def test_active_latch_rejects_each_missing_binding_field(world, field):
    w = world
    with owner(w) as c:
        # Every other field is valid, so only the explicit NOT NULL can reject it.
        assert sqlstate(c, activate(**{field: "NULL"})) == "23514"
    assert latch(w)["state"] == "prepared"


def test_complete_active_latch_is_accepted_and_incomplete_combinations_fail(world):
    w = world
    latch_sql = "UPDATE kalillac.account_security_activation SET "
    cases = [
        ("prepared with an authority version", latch_sql + "authority_version=1", "23514"),
        ("prepared with an activation id", latch_sql + "activation_id=gen_random_uuid()", "23514"),
        ("prepared with a deployment", latch_sql + "deployment_id='disposable-v1'", "23514"),
        ("prepared with a release", latch_sql + "release_sha=repeat('a',40)", "23514"),
        ("prepared with an activation time", latch_sql + "activated_at=clock_timestamp()", "23514"),
        ("active with nothing bound", latch_sql + "state='active'", "23514"),
        ("active with every field NULL", activate(**dict.fromkeys(ACTIVE_BINDING, "NULL")), "23514"),
        ("active with authority version 0", activate(authority_version="0"), "23514"),
        ("active with an invalid deployment", activate(deployment_id="'-disposable'"), "23514"),
        ("active with an uppercase release", activate(release_sha="repeat('A',40)"), "23514"),
        ("active before preparation", activate(activated_at="prepared_at - interval '1 second'"), "23514"),
        ("unknown state", latch_sql + "state='retired'", "23514"),
        ("complete active", activate(), "accepted"),
        ("back to prepared with the binding kept", latch_sql + "state='prepared'", "23514"),
    ]
    with owner(w) as c:
        results = [(name, sqlstate(c, sql)) for name, sql, _ in cases]
    assert results == [(name, expected) for name, _, expected in cases]
    assert latch(w)["state"] == "active" and classify(w.owner_factory) == v1_schema.ACTIVE


# --- downgrade, failure and cancellation ---------------------------------------------------

@pytest.mark.parametrize("target", [LEGACY_HEAD, "0010_spaces", "0008_account_recovery_tokens",
                                    "0007_anonymous_usage", "base"])
def test_required_downgrade_paths_and_reupgrade_succeed(world, target):
    w = world
    migrate(w, target, down=True)
    assert present(w) == (set(), set())
    with owner(w) as c:
        assert c.scalars(text("SELECT version_num FROM kalillac.alembic_version")).all() == ([] if target == "base" else [target])
    migrate(w, HEAD)
    assert version(w) == HEAD and classify(w.owner_factory) == v1_schema.PREPARED


@pytest.mark.parametrize("mutation", [
    "UPDATE kalillac.users SET password_reset_required=true",
    "UPDATE kalillac.users SET deletion_requested_at=clock_timestamp()",
    ACTIVATE,
    "INSERT INTO kalillac.account_security_events SELECT gen_random_uuid(),id,'session_revoke_all',clock_timestamp() FROM kalillac.users",
    "INSERT INTO kalillac.account_security_admission SELECT 0,id,'{}','{}',clock_timestamp() FROM kalillac.users",
    "INSERT INTO kalillac.account_security_admission_aggregate VALUES ('manual','{}',clock_timestamp())",
])
def test_downgrade_refuses_any_non_pristine_security_state(world, mutation):
    w = world
    with owner(w) as c:
        make_user(c, "v102a-state@example.test")
        c.exec_driver_sql(mutation)
    before = snapshot(w)
    with pytest.raises(RuntimeError, match="V1_SECURITY_DOWNGRADE_REFUSED"):
        migrate(w, LEGACY_HEAD, down=True)
    assert version(w) == HEAD and snapshot(w) == before


def test_failed_upgrade_rolls_back_every_0012_step(world):
    w = world
    migrate(w, LEGACY_HEAD, down=True)
    with owner(w) as c:
        # Conflicts with the last table 0012 creates, after every earlier step ran.
        c.exec_driver_sql("CREATE TABLE kalillac.account_security_admission_aggregate (conflict integer)")
    with pytest.raises(DBAPIError) as error:
        migrate(w, HEAD)
    assert error.value.orig.sqlstate == "42P07"
    assert version(w) == LEGACY_HEAD
    assert present(w) == ({"account_security_admission_aggregate"}, set())
    with owner(w) as c:
        c.exec_driver_sql("DROP TABLE kalillac.account_security_admission_aggregate")
    assert classify(w.owner_factory) == v1_schema.LEGACY
    migrate(w, HEAD)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def catalog_state(w):
    """Everything a failed 0012 could leave behind, and the defaults it must not touch."""
    with owner(w) as c:
        return (c.execute(text("SELECT version_num FROM kalillac.alembic_version")).all(),
                c.execute(text("SELECT c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace"
                               " WHERE n.nspname='kalillac' ORDER BY 1")).all(),
                c.execute(text("SELECT attnum,attname,attisdropped FROM pg_attribute"
                               " WHERE attrelid='kalillac.users'::regclass AND attnum>0 ORDER BY 1")).all(),
                c.execute(text("SELECT defaclrole::regrole::text,defaclnamespace,defaclobjtype,defaclacl::text"
                               " FROM pg_default_acl ORDER BY 1,2,3")).all())


# Owner default privileges (and one effective-only route) that would hand the
# new tables, or later functions, to R, A, PUBLIC or an intermediary of R.
UNSAFE_DEFAULTS = {
    "runtime-table-write": ["owner:ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA kalillac"
                            " GRANT INSERT, UPDATE ON TABLES TO {app}"],
    "registration-table-write": ["owner:ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA kalillac"
                                 " GRANT DELETE ON TABLES TO {registration}"],
    "public-table-write": ["owner:ALTER DEFAULT PRIVILEGES FOR ROLE {owner} GRANT TRUNCATE ON TABLES TO PUBLIC"],
    "intermediary-table-write": ["GRANT {hop1} TO {app} WITH INHERIT TRUE",
                                 "owner:ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA kalillac"
                                 " GRANT INSERT ON TABLES TO {hop1}"],
    "runtime-function-execute": ["owner:ALTER DEFAULT PRIVILEGES FOR ROLE {owner} GRANT EXECUTE ON FUNCTIONS TO {app}"],
    "registration-function-execute": ["owner:ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA kalillac"
                                      " GRANT EXECUTE ON FUNCTIONS TO {registration}"],
    "intermediary-function-execute": ["GRANT {hop1} TO {registration} WITH INHERIT TRUE",
                                      "owner:ALTER DEFAULT PRIVILEGES FOR ROLE {owner}"
                                      " GRANT EXECUTE ON FUNCTIONS TO {hop1}"],
    # No ACL or default ACL names R: only the effective check can see this.
    "effective-only-write-all-data": ["GRANT pg_write_all_data TO {app} WITH INHERIT TRUE"],
}


@pytest.mark.parametrize("route", list(UNSAFE_DEFAULTS))
def test_upgrade_refuses_unsafe_owner_default_privileges_atomically(world, roles, route):
    w = world
    migrate(w, LEGACY_HEAD, down=True)
    hop1 = roles("hop1")
    run(w, UNSAFE_DEFAULTS[route], hop1=hop1)
    before = catalog_state(w)
    with pytest.raises(DBAPIError) as error:
        migrate(w, HEAD)
    assert (error.value.orig.sqlstate, error.value.orig.diag.message_primary) == (
        "P0001", "V1_SECURITY_PRIVILEGES_UNSAFE")
    # Nothing survives: still 0011, no table, column, latch or admission state,
    # and the offending default privilege is neither repaired nor revoked.
    assert version(w) == LEGACY_HEAD and present(w) == (set(), set())
    assert catalog_state(w) == before
    # Only the unsafe privilege was the cause: without it 0012 applies.
    run(w, ["REVOKE pg_write_all_data FROM {app}", "REVOKE {hop1} FROM {app}", "REVOKE {hop1} FROM {registration}",
            "owner:ALTER DEFAULT PRIVILEGES FOR ROLE {owner} REVOKE ALL ON TABLES FROM PUBLIC,{app},{registration},{hop1}",
            "owner:ALTER DEFAULT PRIVILEGES FOR ROLE {owner} REVOKE ALL ON FUNCTIONS FROM {app},{registration},{hop1}",
            "owner:ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA kalillac"
            " REVOKE ALL ON TABLES FROM PUBLIC,{app},{registration},{hop1}",
            "owner:ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA kalillac"
            " REVOKE ALL ON FUNCTIONS FROM {app},{registration},{hop1}"], hop1=hop1)
    migrate(w, HEAD)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def memberships(w):
    with admin(w) as c:
        return c.execute(text("SELECT roleid::regrole::text,member::regrole::text,inherit_option,set_option,"
                              "admin_option FROM pg_auth_members ORDER BY 1,2")).all()


# R or A authority at 0011 that 0012 must refuse; {p} is R or A. (grants, the
# role reached, its edge's (inherit_option, set_option) where direct.)
UNSAFE_MEMBERSHIPS = {
    "owner-inherit": (["GRANT {owner} TO {p} WITH INHERIT TRUE"], "owner", (True, True)),
    "owner-set-only": (["GRANT {owner} TO {p} WITH INHERIT FALSE, SET TRUE"], "owner", (False, True)),
    "write-all-data-inherit": (["GRANT pg_write_all_data TO {p} WITH INHERIT TRUE"], "pg_write_all_data", (True, True)),
    "write-all-data-set-only": (["GRANT pg_write_all_data TO {p} WITH INHERIT FALSE, SET TRUE"],
                                "pg_write_all_data", (False, True)),
    "multi-hop-owner": (["GRANT {owner} TO {hop2} WITH INHERIT TRUE", "GRANT {hop2} TO {hop1} WITH INHERIT TRUE",
                         "GRANT {hop1} TO {p} WITH INHERIT TRUE"], "hop1", (True, True)),
    "multi-hop-write-all-data": (["GRANT pg_write_all_data TO {hop1} WITH INHERIT TRUE",
                                  "GRANT {hop1} TO {p} WITH INHERIT TRUE"], "hop1", (True, True)),
}


@pytest.mark.parametrize("route", list(UNSAFE_MEMBERSHIPS))
@pytest.mark.parametrize("principal", list(PRINCIPALS))
def test_upgrade_refuses_runtime_or_registration_membership_atomically(world, roles, principal, route):
    w = world
    migrate(w, LEGACY_HEAD, down=True)
    grants, reached, options = UNSAFE_MEMBERSHIPS[route]
    names = {"p": w.names[principal], "hop1": roles("hop1"), "hop2": roles("hop2")}
    target = {**w.names, **names}.get(reached, reached)
    run(w, grants, **names)
    edge = [m for m in memberships(w) if m[:2] == (target, names["p"])]
    assert [m[2:4] for m in edge] == [options]
    # The authority is real: an inherited write, or SET ROLE (no inherited
    # privilege, so invisible to effective-privilege checks) and then a write.
    # The write reads no column: pg_write_all_data confers no SELECT.
    forbidden_write = "UPDATE kalillac.account_registration_activation SET policy_version=1"
    with getattr(w, PRINCIPALS[principal]).connect() as c:
        if options == (False, True):
            assert sqlstate(c, forbidden_write) == "42501"
            c.exec_driver_sql("SET ROLE " + target)
        assert c.exec_driver_sql(forbidden_write).rowcount == 1
        c.rollback()
    before = (catalog_state(w), memberships(w))
    with pytest.raises(DBAPIError) as error:
        migrate(w, HEAD)
    assert (error.value.orig.sqlstate, error.value.orig.diag.message_primary) == (
        "P0001", "V1_SECURITY_PRIVILEGES_UNSAFE")
    # Still 0011, nothing of 0012 left, and the membership neither revoked nor repaired.
    assert version(w) == LEGACY_HEAD and present(w) == (set(), set())
    assert (catalog_state(w), memberships(w)) == before
    # The migration login's trusted owner membership is untouched and still
    # works: without the unsafe membership 0012 applies through it.
    assert (w.names["owner"], w.names["migrate"]) in [m[:2] for m in memberships(w)]
    # The intermediary's own write-all-data membership is unsafe for everyone,
    # R/A or not, so it goes too.
    run(w, ["REVOKE {target} FROM {p}", "REVOKE pg_write_all_data FROM {hop1}"], target=target, **names)
    migrate(w, HEAD)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def test_upgrade_resolves_configured_and_recorded_role_names_safely(world, monkeypatch):
    w = world
    # The harness's R, owner and recorded A are not the defaults, so every
    # successful 0012 here already resolved configured and recorded names.
    assert w.names["app"] != "kalillac_app" and w.names["owner"] != "kalillac_owner"
    migrate(w, LEGACY_HEAD, down=True)
    with owner(w) as c:
        receipt = c.scalar(text("SELECT obj_description('kalillac.account_registration_activation'::regclass,"
                                "'pg_class')"))
    assert json.loads(receipt)["registration_role"] == w.names["registration"]
    absent = w.names["app"].replace("_app_", "_absent_")
    hostile = json.dumps({**json.loads(receipt), "registration_role": 'x"; DROP SCHEMA kalillac CASCADE; --'})

    def comment(value):
        with admin(w) as c:
            c.exec_driver_sql(c.scalar(text("SELECT format('COMMENT ON TABLE kalillac.account_registration_activation"
                                            " IS %L', CAST(:v AS text))"), {"v": value}))
    # An R that does not exist, and an A name built to break out of quoting,
    # are each refused; nothing is executed from either.
    for setup, undo in ((lambda: monkeypatch.setenv("KALILLAC_MIGRATION_APP_ROLE", absent),
                         lambda: monkeypatch.setenv("KALILLAC_MIGRATION_APP_ROLE", w.names["app"])),
                        (lambda: comment(hostile), lambda: comment(receipt))):
        setup()
        with pytest.raises(DBAPIError) as error:
            migrate(w, HEAD)
        assert error.value.orig.diag.message_primary == "V1_SECURITY_PRIVILEGES_UNSAFE"
        assert version(w) == LEGACY_HEAD and present(w) == (set(), set())
        undo()
    migrate(w, HEAD)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def test_cancelled_upgrade_rolls_back_every_0012_step(world):
    w = world
    migrate(w, LEGACY_HEAD, down=True)
    outcome = {}
    with w.migration.connect() as blocker, w.migration.connect() as worker:
        pid = worker.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        worker.rollback()
        blocker.exec_driver_sql("SET ROLE " + w.names["owner"])
        # The latch FK needs SHARE ROW EXCLUSIVE here, after both users columns were added.
        blocker.exec_driver_sql("LOCK TABLE kalillac.account_registration_activation IN ROW EXCLUSIVE MODE")

        def upgrade():
            try:
                with worker.begin():
                    command.upgrade(_config(connection=worker), HEAD)
            except BaseException as error:
                outcome["error"] = error
        thread = threading.Thread(target=upgrade)
        thread.start()
        deadline = time.monotonic() + 10
        with w.migration.connect() as watcher:
            while not watcher.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:p AND NOT granted)"), {"p": pid}):
                assert time.monotonic() < deadline, "the upgrade never waited mid-transaction"
                watcher.rollback()
                time.sleep(0.01)
            waiting = watcher.scalar(text("SELECT query FROM pg_stat_activity WHERE pid=:p"), {"p": pid})
            assert "account_security_activation" in waiting
            assert watcher.scalar(text("SELECT pg_cancel_backend(:p)"), {"p": pid})
        thread.join(10)
        blocker.rollback()
    assert not thread.is_alive() and outcome["error"].orig.sqlstate == "57014"
    assert version(w) == LEGACY_HEAD and present(w) == (set(), set())
    migrate(w, HEAD)
    assert classify(w.owner_factory) == v1_schema.PREPARED


# --- protected registration and V1-01 compatibility ----------------------------------------

def test_registration_catalog_and_protected_authority_remain_intact(active_world):
    w = active_world
    with w.factory() as s, s.begin():
        effective_privileges(s, w.names["registration"], w.names["app"], w.names["owner"])
    assert schema_probe(w.runtime_factory, True) is True
    w.authority.issue_after_response("v102a-protected@example.test")
    w.authority.confirm_after_hash(w.authority.prevalidate(w.token()), "synthetic-hash")
    with w.runtime_factory() as s:
        assert s.execute(text("SELECT email,password_reset_required FROM kalillac.users")).one() == (
            "v102a-protected@example.test", False)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def test_authorization_failure_diagnostic_on_real_lock_wait_timeout_keeps_the_response(world, caplog, capfd):
    w = world
    with owner(w) as c:
        uid = make_user(c, "v102a-logged@example.test")
    with w.runtime_factory.begin() as s:
        now = s.scalar(text("SELECT clock_timestamp()"))
        s.add(AccountSession(user_id=uid, token_hash=DIGEST, created_at=now, expires_at=now + timedelta(days=1)))
    security = load_v1_security(lambda: w.runtime_factory, ENV)
    api = FastAPI()
    api.include_router(build_account_router(lambda: w.runtime_factory, v1_security=security))
    capfd.readouterr()
    with owner(w) as c:
        c.execute(text("SELECT id FROM kalillac.users WHERE id=:id FOR UPDATE"), {"id": uid})
        with pytest.raises(V1SecurityUnavailable) as raised:
            security.read(DIGEST, lambda s, a: a)
        with TestClient(api, base_url="https://testserver") as client:
            response = client.get("/api/account/me", headers={"Cookie": "__Host-kalillac_account=" + TOKEN})
    assert (response.status_code, response.json(), response.headers["cache-control"]) == (
        503, {"error": "account_unavailable"}, "no-store")
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    # The fixed diagnostic only, on the controlled stderr sink, never via logging.
    # V1-01 bounds both the lock wait and the statement at 3s; either timeout may fire first.
    captured = capfd.readouterr()
    lines = captured.err.splitlines()
    assert captured.out == "" and len(lines) == 2 and all(re.fullmatch(
        r"V1 account authorization unavailable: OperationalError sqlstate=(55P03|57014)", line) for line in lines)
    assert not [r for r in caplog.records if r.name == LOGGER]
    for secret in (TOKEN, DIGEST, "v102a-logged@example.test", str(uid), "SELECT", "kalillac."):
        assert secret not in captured.err + " ".join(r.getMessage() for r in caplog.records)
    assert w.runtime_engine.pool.checkedout() == 0
