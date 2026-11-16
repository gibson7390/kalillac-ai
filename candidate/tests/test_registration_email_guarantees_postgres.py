"""Owner decision 14: what proves email ownership, checked on real disposable PostgreSQL 16.

Dropping a V1 email-verification authority is safe only if, once email-first registration is active,
no account can exist unverified, be created without proof of its address, or change its address or
verification state. These tests check each guarantee in the database itself, including migration 0021,
which makes the activation transition count unverified accounts instead of trusting the recorded 0.
Every address and hash is synthetic.
"""
from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import kalillac_accounts.v1_schema as v1_schema
from test_v1_revocation_postgres import admin_sql
from test_v1_schema_postgres import classify, migrate, refused, sqlstate
from test_v1_session_end_postgres import head_world

HEAD = "0021_registration_activation"
PREVIOUS = "0020_v1_password_change"
SIGNATURE = "kalillac.registration_activation_guard()"
HASH = "$argon2id$v=19$m=65536,t=3,p=4$" + "s" * 22 + "$" + "h" * 43
ACTIVATE = ("UPDATE kalillac.account_registration_activation SET state='active_zero_legacy',activation_id=:a,"
            "cutover_id=:c,deployment_id='dev-test',evidence_sha256=:e,inventory_at=clock_timestamp(),"
            "activated_at=clock_timestamp(),legacy_unverified_count=0")


@pytest.fixture
def world(monkeypatch):
    with head_world(monkeypatch) as w:                 # Registration not yet active.
        yield w


def activate(w):
    with w.migration.begin() as c:
        c.exec_driver_sql("SET ROLE " + w.names["owner"])
        c.execute(text(ACTIVATE), {"a": uuid.uuid4(), "c": uuid.uuid4(), "e": "0" * 64})


def state(w):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT state FROM kalillac.account_registration_activation"))


def legacy_account(w, verified):
    """An account created the legacy way, by the runtime role, before activation."""
    user = uuid.uuid4()
    with w.runtime_engine.begin() as c:
        c.execute(text("INSERT INTO kalillac.users(id,email,password_hash,is_active,email_verified_at)"
                       " VALUES (:i,:e,:h,true,CASE WHEN :v THEN clock_timestamp() END)"),
                  {"i": user, "e": f"legacy-{user.hex[:12]}@example.test", "h": HASH, "v": verified})
    return user


def as_owner(w, statement, **params):
    with w.migration.begin() as c:
        c.exec_driver_sql("SET ROLE " + w.names["owner"])
        c.execute(text(statement), params)


def test_activation_is_refused_while_any_account_is_unverified(world):
    w = world
    legacy_account(w, verified=True)
    unverified = legacy_account(w, verified=False)
    with pytest.raises(DBAPIError, match="REGISTRATION_LEGACY_UNVERIFIED"):
        activate(w)
    assert state(w) == "unactivated"
    with w.runtime_engine.begin() as c:                   # The legacy route verifies it first.
        c.execute(text("UPDATE kalillac.users SET email_verified_at = clock_timestamp() WHERE id = :u"),
                  {"u": unverified})
    activate(w)
    assert state(w) == "active_zero_legacy"


def test_an_unverified_account_inserted_during_activation_cannot_slip_past_it(world):
    w = world
    insert = w.runtime_engine.connect()
    transaction = insert.begin()
    user = uuid.uuid4()
    insert.execute(text("INSERT INTO kalillac.users(id,email,password_hash,is_active) VALUES (:i,:e,:h,true)"),
                   {"i": user, "e": f"race-{user.hex[:12]}@example.test", "h": HASH})
    outcome = []

    def run():
        try:
            activate(w)
            outcome.append("activated")
        except DBAPIError as error:
            outcome.append("REGISTRATION_LEGACY_UNVERIFIED" in str(error.orig))
    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:            # The activation waits on the insert's row share lock.
            with w.admin_engine.connect() as c:
                if c.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                                 " AND wait_event_type = 'Lock'")):
                    break
            time.sleep(0.05)
        else:
            pytest.fail("the activation never waited for the in-flight insert")
        transaction.commit()
    finally:
        insert.close()
        thread.join(30)
    assert outcome == [True] and state(w) == "unactivated"


def test_after_activation_no_account_is_created_without_proof_of_its_address(world):
    w = world
    activate(w)
    for engine in (w.runtime_engine, w.engine):
        user = uuid.uuid4()
        with engine.connect() as c:
            with pytest.raises(DBAPIError) as error:
                with c.begin():
                    c.execute(text("INSERT INTO kalillac.users(id,email,password_hash,is_active,email_verified_at)"
                                   " VALUES (:i,:e,:h,true,clock_timestamp())"),
                              {"i": user, "e": f"x-{user.hex[:12]}@example.test", "h": HASH})
            assert error.value.orig.sqlstate in ("23514", "42501")
    with pytest.raises(DBAPIError, match="23514|REGISTRATION"):
        as_owner(w, "INSERT INTO kalillac.users(id,email,password_hash,is_active) VALUES (:i,'o@example.test',:h,true)",
                 i=uuid.uuid4(), h=HASH)


def test_after_activation_address_and_verification_state_never_change(world):
    w = world
    user = legacy_account(w, verified=True)
    activate(w)
    with pytest.raises(DBAPIError, match="REGISTRATION_IDENTITY_IMMUTABLE"):
        as_owner(w, "UPDATE kalillac.users SET email = 'changed@example.test' WHERE id = :u", u=user)
    for assignment in ("email_verified_at = NULL", "email_verified_at = clock_timestamp()"):
        with pytest.raises(DBAPIError, match="REGISTRATION_AUTHORITY_REQUIRED"):
            as_owner(w, f"UPDATE kalillac.users SET {assignment} WHERE id = :u", u=user)
        with w.runtime_engine.connect() as c:
            assert sqlstate(c, f"UPDATE kalillac.users SET {assignment} WHERE id = :u", {"u": user}) == "23514"
    with w.runtime_engine.connect() as c:                  # The runtime cannot touch the address at all.
        assert sqlstate(c, "UPDATE kalillac.users SET email = 'x@example.test' WHERE id = :u", {"u": user}) == "42501"
    with w.admin_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM kalillac.users WHERE email_verified_at IS NULL")) == 0


def test_the_guard_is_classified_exactly_and_executable_by_nobody_but_its_owner(world):
    w = world
    assert classify(w.owner_factory) == v1_schema.PREPARED
    for engine in (w.runtime_engine, w.engine):
        with engine.connect() as c:
            assert c.scalar(text("SELECT has_function_privilege(current_user, :f, 'EXECUTE')"),
                            {"f": SIGNATURE}) is False


@pytest.mark.parametrize("tamper", [
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('" + SIGNATURE + "')",
    "GRANT EXECUTE ON FUNCTION " + SIGNATURE + " TO {app}",
    "ALTER TABLE kalillac.account_registration_activation DISABLE TRIGGER trg_registration_activation_zero_legacy",
    "ALTER FUNCTION " + SIGNATURE + " SECURITY DEFINER",
])
def test_a_tampered_guard_is_refused_by_startup_and_by_the_downgrade(world, tamper):
    w = world
    migrate(w, HEAD, down=True)                  # 0021's own downgrade; 0022's is tested with 0022.
    admin_sql(w, tamper.format(**w.names))
    refused(w)
    with pytest.raises(DBAPIError, match="REGISTRATION_ACTIVATION_GUARD_POSTCONDITION_INVALID"):
        migrate(w, PREVIOUS, down=True)


def test_downgrade_and_upgrade_restore_the_exact_catalogs(world):
    w = world
    migrate(w, PREVIOUS, down=True)
    assert classify(w.owner_factory) == v1_schema.PREPARED
    with w.admin_engine.connect() as c:
        assert c.scalar(text("SELECT to_regprocedure(:f) IS NULL"), {"f": SIGNATURE})
    migrate(w, HEAD)
    assert classify(w.owner_factory) == v1_schema.PREPARED
