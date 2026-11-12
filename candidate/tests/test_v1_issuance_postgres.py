"""V1-02C private session issuance and the session epoch on real disposable PostgreSQL 16.

Uses the registration harness (loopback/disposable only; CI's REQUIRE_POSTGRES
switch turns an absent database into a failure). S stays NOLOGIN: it is
exercised only through SET LOCAL ROLE from the disposable superuser, which
also seeds fixtures and injects faults. No application path calls it. Every
password hash here is synthetic.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import importlib.util
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

import kalillac_accounts.v1_schema as v1_schema
from kalillac_accounts.auth import RequestRejected
from kalillac_accounts.registration_schema import guard_functions, identity_guard_v2
from kalillac_accounts.v1_security import FLAG, V1SecurityConfigError, load_v1_startup
from kalillac_db import migration_roles
from registration_test_support import postgres_world
from test_account_migration import _config
from test_v1_revocation_postgres import (Call, admin_sql, authorize, holding, revoke, security_state, set_user,
                                         wait_blocked)
from test_v1_schema_postgres import ENV, catalog_state, classify, memberships, migrate, refused, run, sqlstate

REVISION = "0014_v1_session_issuance"
PREVIOUS = "0013_v1_session_revocation"
BASE = "0012_v1_account_security"
SIGNATURE = "kalillac.v1_issue_session(uuid,text,uuid,text,integer)"
DAYS = 14
MICRO = timedelta(microseconds=1)
DENIED = ("not_authenticated", None, None, False)
UNAVAILABLE = ("security_unavailable", None, None, False)
_spec = importlib.util.spec_from_file_location(
    "v1_02c_migration_pg", Path(__file__).resolve().parents[1] / "migrations" / "versions"
    / "0014_v1_session_issuance_epoch.py")
MIGRATION = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(MIGRATION)


@contextmanager
def _world(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    with postgres_world(monkeypatch, active=False) as w:
        url = make_url(os.environ["KALILLAC_REGISTRATION_TEST_POSTGRES_URL"]).set(database=w.migration.url.database)
        w.admin_engine = create_engine(url, hide_parameters=True, pool_size=12, max_overflow=0)
        w.owner_engine = create_engine(w.migration.url, hide_parameters=True,
                                       connect_args={"options": "-c role=" + w.names["owner"]})
        w.owner_factory = sessionmaker(bind=w.owner_engine)
        # These tests verify 0014 itself: stand exactly on it, below the
        # Financial Safeguards ledger (0015) that the shared world upgrades to.
        migrate(w, REVISION, down=True)
        try:
            yield w
        finally:
            w.admin_engine.dispose()
            w.owner_engine.dispose()


@pytest.fixture
def world(monkeypatch):
    with _world(monkeypatch) as w:
        yield w
        assert w.admin_engine.pool.checkedout() == 0 and w.runtime_engine.pool.checkedout() == 0


def account(w):
    """A verified active user with a synthetic stored hash; returns (user_id, stored_hash)."""
    user = uuid.uuid4()
    stored = "$argon2id$v=19$m=65536,t=3,p=4$" + user.hex[:22] + "$synthetic-v1-02c-verified-hash"
    admin_sql(w, "INSERT INTO kalillac.users(id,email,password_hash,is_active,email_verified_at)"
                 " VALUES (:i,:e,:h,true,clock_timestamp())", i=user, e=f"v102c-{user.hex[:16]}@example.test", h=stored)
    return user, stored


def key():
    """A caller-generated (session_id, token_hash) pair."""
    return uuid.uuid4(), secrets.token_hex(32)


def issue(w, session_id, token_hash, user, stored, days=DAYS, c=None, settings=()):
    """Call the function as the NOLOGIN service role S."""
    def call(c):
        for setting in settings:
            c.exec_driver_sql(setting)
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["service"])
        return tuple(c.execute(text("SELECT * FROM kalillac.v1_issue_session(:s,:t,:u,:h,:d)"),
                               {"s": session_id, "t": token_hash, "u": user, "h": stored, "d": days}).one())
    if c is not None:
        return call(c)
    with w.admin_engine.begin() as c:
        return call(c)


def sessions(w, user):
    with w.admin_engine.connect() as c:
        return [tuple(r) for r in c.execute(text(
            "SELECT id,token_hash,created_at,expires_at,revoked_at FROM kalillac.account_sessions WHERE user_id=:u"
            " ORDER BY created_at,id"), {"u": user})]


def epoch(w, user):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT sessions_valid_after FROM kalillac.users WHERE id=:u"), {"u": user})


def database_now(w):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT clock_timestamp()"))


def authorized(w, user, k):
    """V1-01's fresh original-session authorization, as R: True or the refusal status."""
    try:
        return authorize(w, user, k).id == user
    except RequestRejected as rejected:
        return rejected.status


def reported(error):
    """Everything an error report carries to the client (and the server log)."""
    diag = error.orig.diag
    return "\n".join(str(part) for part in (error, error.orig, diag.message_primary, diag.message_detail,
                                            diag.message_hint, diag.context, diag.internal_query) if part)


def locks_left(w):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid"
                             " WHERE a.datname=current_database() AND a.pid<>pg_backend_pid()"))


def literal(sql):
    """Function text for run(), which formats role names into each statement."""
    return sql.replace("{", "{{").replace("}", "}}")


def guard_sql(version):
    template = MIGRATION.GUARD_V2 if version == 2 else MIGRATION.GUARD_V1
    return ("CREATE OR REPLACE FUNCTION kalillac.registration_user_identity_guard "
            + literal(template).replace(MIGRATION.MARKER, "{registration}"))


def authority_state(w):
    """The private-authority catalog, by name: functions, ownership, grants and guard binding."""
    service, executor = w.names["service"], w.names["executor"]
    with w.admin_engine.connect() as c:
        return (c.execute(text("SELECT p.proname,encode(sha256(convert_to(p.prosrc,'UTF8')),'hex'),"
                               "pg_get_userbyid(p.proowner),p.proacl::text,p.proconfig,p.prosecdef FROM pg_proc p"
                               " JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='kalillac'"
                               " ORDER BY 1")).all(),
                c.execute(text("SELECT pg_get_userbyid(refobjid),classid::regclass::text,deptype,count(*)"
                               " FROM pg_shdepend WHERE refclassid='pg_authid'::regclass"
                               " AND pg_get_userbyid(refobjid) IN (:s,:e) GROUP BY 1,2,3 ORDER BY 1,2,3"),
                          {"s": service, "e": executor}).all(),
                c.execute(text("SELECT c.relname,a.attname,pg_get_userbyid(x.grantee),x.privilege_type,x.is_grantable"
                               " FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid"
                               " CROSS JOIN LATERAL aclexplode(a.attacl) x"
                               " WHERE pg_get_userbyid(x.grantee) IN (:s,:e) ORDER BY 1,2,3,4"),
                          {"s": service, "e": executor}).all(),
                c.scalar(text("SELECT nspacl::text FROM pg_namespace WHERE nspname='kalillac'")),
                c.scalar(text("SELECT has_schema_privilege(r.oid,'kalillac','CREATE') FROM pg_roles r"
                              " WHERE r.rolname=:e"), {"e": executor}),
                c.execute(text("SELECT tgname,tgenabled::text,tgfoid::regprocedure::text FROM pg_trigger"
                               " WHERE tgrelid='kalillac.users'::regclass AND NOT tgisinternal ORDER BY 1")).all(),
                memberships(w))


def functions(state):
    return {row[0]: row[1] for row in state[0]}


# --- authority boundary ------------------------------------------------------------------------

def test_service_issues_through_set_role_while_nologin(world):
    w = world
    with w.admin_engine.connect() as c:
        assert c.execute(text("SELECT rolname, rolcanlogin, rolinherit FROM pg_roles WHERE rolname IN (:s,:e)"
                              " ORDER BY 1"), {"s": w.names["service"], "e": w.names["executor"]}).all() == sorted(
            [(w.names["service"], False, False), (w.names["executor"], False, False)])
    user, stored = account(w)
    k = key()
    before = database_now(w)
    outcome, issued_at, valid_until, replayed = issue(w, *k, user, stored)
    after = database_now(w)
    assert (outcome, replayed) == ("issued", False)
    # Database time, and the stored row is exactly what was returned.
    assert before < issued_at < after and valid_until - issued_at == timedelta(hours=24 * DAYS)
    assert sessions(w, user) == [(k[0], k[1], issued_at, valid_until, None)]
    assert authorized(w, user, k) is True


def test_runtime_registration_public_and_owner_cannot_execute_issuance(world):
    w = world
    user, stored = account(w)
    call = "SELECT * FROM kalillac.v1_issue_session(gen_random_uuid(),repeat('a',64),:u,:h,14)"
    for engine in (w.runtime_engine, w.engine):
        with engine.connect() as c:
            assert sqlstate(c, call, {"u": user, "h": stored}) == "42501"
    with w.admin_engine.connect() as c:
        for principal in ("public", w.names["app"], w.names["registration"], w.names["owner"]):
            assert not c.scalar(text("SELECT has_function_privilege(:p,:f,'EXECUTE')"), {"p": principal, "f": SIGNATURE})
        assert c.scalar(text("SELECT has_function_privilege(:p,:f,'EXECUTE')"),
                        {"p": w.names["service"], "f": SIGNATURE})
    assert sessions(w, user) == []


def test_only_the_executor_may_write_the_epoch_or_issue_session_rows(world):
    w = world
    user, stored = account(w)
    with w.admin_engine.connect() as c:
        for principal in ("public", w.names["app"], w.names["registration"], w.names["service"]):
            assert not c.scalar(text("SELECT has_column_privilege(:p,'kalillac.users','sessions_valid_after','UPDATE')"),
                                {"p": principal})
        assert c.scalar(text("SELECT has_column_privilege(:p,'kalillac.users','sessions_valid_after','UPDATE')"),
                        {"p": w.names["executor"]})
        for principal in ("public", w.names["registration"], w.names["service"]):
            assert not c.scalar(text("SELECT has_any_column_privilege(:p,'kalillac.account_sessions','INSERT')"),
                                {"p": principal})
    advance = "UPDATE kalillac.users SET sessions_valid_after=clock_timestamp() WHERE id=:u"
    for engine in (w.runtime_engine, w.engine):
        with engine.connect() as c:
            assert sqlstate(c, advance, {"u": user}) == "42501"
    with w.admin_engine.connect() as c:
        c.begin()
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["service"])
        assert sqlstate(c, advance, {"u": user}) == "42501"
        assert sqlstate(c, "INSERT INTO kalillac.account_sessions(id,user_id,token_hash,created_at,expires_at)"
                           " VALUES (gen_random_uuid(),:u,repeat('b',64),clock_timestamp(),clock_timestamp())",
                        {"u": user}) == "42501"
        c.rollback()
    assert epoch(w, user) is None and sessions(w, user) == []


def test_executor_epoch_writes_only_move_forward_and_touch_nothing_else(world):
    w = world
    user, stored = account(w)
    with w.admin_engine.connect() as c:
        c.begin()
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["executor"])
        update = "UPDATE kalillac.users SET {} WHERE id=:u"
        assert sqlstate(c, update.format("sessions_valid_after=clock_timestamp()"), {"u": user}) == "accepted"
        assert sqlstate(c, update.format("sessions_valid_after=sessions_valid_after-interval '1 microsecond'"),
                        {"u": user}) == "23514"
        assert sqlstate(c, update.format("sessions_valid_after=NULL"), {"u": user}) == "23514"
        assert sqlstate(c, update.format("sessions_valid_after=sessions_valid_after+interval '1 microsecond'"),
                        {"u": user}) == "accepted"
        for assignment in ("quarantined_at=clock_timestamp()", "password_hash='x'", "is_active=false",
                           "email_verified_at=NULL", "password_reset_required=true",
                           "deletion_requested_at=clock_timestamp()", "email='other@example.test'"):
            assert sqlstate(c, update.format(assignment), {"u": user}) == "42501", assignment
        assert sqlstate(c, "INSERT INTO kalillac.account_sessions(id,user_id,token_hash,created_at,expires_at,revoked_at)"
                           " VALUES (gen_random_uuid(),:u,repeat('c',64),clock_timestamp(),clock_timestamp(),NULL)",
                        {"u": user}) == "42501"
        assert sqlstate(c, "DELETE FROM kalillac.account_sessions") == "42501"
        assert sqlstate(c, "SELECT email FROM kalillac.users") == "42501"
        c.rollback()
    assert epoch(w, user) is None


def test_guard_v2_allows_only_the_forward_epoch_transition_to_any_writer(world):
    """Column privilege bounds who may write the epoch; the guard bounds what
    any writer, the owner included, may do with it."""
    w = world
    user, stored = account(w)
    update = "UPDATE kalillac.users SET {} WHERE id=:u"
    with w.admin_engine.connect() as c:
        c.begin()
        for assignment in ("email='moved@example.test'", "quarantined_at=clock_timestamp()",
                           "sessions_valid_after=clock_timestamp(), quarantined_at=clock_timestamp()",
                           "sessions_valid_after=clock_timestamp(), email='moved@example.test'",
                           "sessions_valid_after=clock_timestamp(), created_at=created_at-interval '1 day'"):
            assert sqlstate(c, update.format(assignment), {"u": user}) == "23514", assignment
        assert sqlstate(c, update.format("sessions_valid_after=clock_timestamp()"), {"u": user}) == "accepted"
        assert sqlstate(c, update.format("sessions_valid_after=sessions_valid_after"), {"u": user}) == "accepted"
        assert sqlstate(c, update.format("sessions_valid_after=sessions_valid_after-interval '1 microsecond'"),
                        {"u": user}) == "23514"
        assert sqlstate(c, update.format("sessions_valid_after=NULL"), {"u": user}) == "23514"
        c.rollback()


# --- outcomes ----------------------------------------------------------------------------------

def _change(assignments):
    return lambda w, u, h: set_user(w, u, assignments) or (u, h)


DENIALS = {
    "unknown-account": lambda w, u, h: (uuid.uuid4(), h),
    "other-verified-hash": lambda w, u, h: (u, h + "-other"),
    "null-verified-hash": lambda w, u, h: (u, None),
    "password-changed": lambda w, u, h: admin_sql(w, "UPDATE kalillac.users SET password_hash=:n WHERE id=:u",
                                                  n=h + "-new", u=u) or (u, h),
    "inactive": _change("is_active=false"),
    "unverified": _change("email_verified_at=NULL"),
    "quarantined": _change("quarantined_at=clock_timestamp()"),
    "deletion-requested": _change("deletion_requested_at=clock_timestamp()"),
    "reset-required": _change("password_reset_required=true"),
}


@pytest.mark.parametrize("case", list(DENIALS))
def test_expected_denials_are_uniform_and_store_nothing(world, case):
    w = world
    user, stored = account(w)
    before = security_state(w)
    target, verified = DENIALS[case](w, user, stored)
    assert issue(w, *key(), target, verified) == DENIED
    assert sessions(w, user) == [] and epoch(w, user) is None and security_state(w) == before


INVALID = {
    "null-session-id": lambda k: (None, k[1], DAYS),
    "null-token-hash": lambda k: (k[0], None, DAYS),
    "uppercase-token-hash": lambda k: (k[0], k[1].upper(), DAYS),
    "short-token-hash": lambda k: (k[0], k[1][:63], DAYS),
    "long-token-hash": lambda k: (k[0], k[1] + "0", DAYS),
    "non-hex-token-hash": lambda k: (k[0], "g" * 64, DAYS),
    "token-hash-with-newline": lambda k: (k[0], k[1] + "\n", DAYS),
    "token-hash-with-space": lambda k: (k[0], k[1][:63] + " ", DAYS),
    "zero-lifetime": lambda k: (k[0], k[1], 0),
    "negative-lifetime": lambda k: (k[0], k[1], -1),
    "lifetime-over-366": lambda k: (k[0], k[1], 367),
    "null-lifetime": lambda k: (k[0], k[1], None),
}


@pytest.mark.parametrize("case", list(INVALID))
def test_invalid_inputs_are_refused_and_store_nothing(world, case):
    w = world
    user, stored = account(w)
    session_id, token_hash, days = INVALID[case](key())
    assert issue(w, session_id, token_hash, user, stored, days=days) == UNAVAILABLE
    assert sessions(w, user) == []


def test_null_account_id_is_refused_and_stores_nothing(world):
    w = world
    user, stored = account(w)
    assert issue(w, *key(), None, stored) == UNAVAILABLE
    assert sessions(w, user) == []


def test_invalid_inputs_are_refused_without_waiting_for_the_account_lock(world):
    w = world
    user, stored = account(w)
    blocker = holding(w, f"SELECT 1 FROM kalillac.users WHERE id='{user}' FOR UPDATE", 3)
    assert issue(w, *key(), user, stored, days=0, settings=("SET LOCAL lock_timeout = '100ms'",)) == UNAVAILABLE
    blocker.join(15)
    assert blocker.error is None


@pytest.mark.parametrize("days", [1, 366])
def test_lifetime_bounds_are_accepted_exactly(world, days):
    w = world
    user, stored = account(w)
    k = key()
    outcome, issued_at, valid_until, replayed = issue(w, *k, user, stored, days=days)
    assert (outcome, replayed) == ("issued", False) and valid_until - issued_at == timedelta(hours=24 * days)
    assert issue(w, *k, user, stored, days=days) == ("issued", issued_at, valid_until, True)


def test_issuance_writes_no_event_admission_or_outbox(world):
    w = world
    user, stored = account(w)
    before = security_state(w)[:3]
    k = key()
    issue(w, *k, user, stored)
    issue(w, *k, user, stored)
    issue(w, *key(), user, stored + "-other")
    assert security_state(w)[:3] == before
    with w.admin_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM kalillac.account_outbox")) == 0


# --- time and the epoch ------------------------------------------------------------------------

def test_issuance_behind_a_future_epoch_lands_one_microsecond_after_it(world):
    """A database clock behind the epoch never yields a session at or before it."""
    w = world
    user, stored = account(w)
    set_user(w, user, "sessions_valid_after=clock_timestamp()+interval '1 hour'")
    boundary = epoch(w, user)
    k = key()
    assert issue(w, *k, user, stored) == ("issued", boundary + MICRO, boundary + MICRO + timedelta(hours=24 * DAYS),
                                          False)
    assert issue(w, *k, user, stored)[3] is True
    assert authorized(w, user, k) is True


@pytest.mark.parametrize("offset,valid", [(-1, True), (0, False), (1, False)],
                         ids=["created-one-microsecond-after", "created-at-the-epoch", "created-one-microsecond-before"])
def test_epoch_validity_is_strict_to_the_microsecond(world, offset, valid):
    w = world
    user, stored = account(w)
    k = key()
    _, issued_at, valid_until, _ = issue(w, *k, user, stored)
    set_user(w, user, f"sessions_valid_after=(SELECT created_at FROM kalillac.account_sessions WHERE id='{k[0]}')"
                      f" + interval '{offset} microsecond'")
    assert epoch(w, user) == issued_at + offset * MICRO
    assert issue(w, *k, user, stored) == (("issued", issued_at, valid_until, True) if valid else DENIED)
    assert authorized(w, user, k) == (True if valid else 401)


# --- replay ------------------------------------------------------------------------------------

def test_replay_returns_the_original_stored_times_and_creates_nothing(world):
    w = world
    user, stored = account(w)
    k = key()
    first = issue(w, *k, user, stored)
    assert issue(w, *k, user, stored) == ("issued", first[1], first[2], True)
    assert sessions(w, user) == [(k[0], k[1], first[1], first[2], None)]


REPLAY_COLLISIONS = {
    "session-id-of-another-account": lambda k, other: ((k[0], secrets.token_hex(32)), other),
    "session-id-with-another-token-hash": lambda k, other: ((k[0], secrets.token_hex(32)), None),
    "token-hash-under-another-session-id": lambda k, other: ((uuid.uuid4(), k[1]), None),
    "token-hash-of-another-account": lambda k, other: ((uuid.uuid4(), k[1]), other),
}


@pytest.mark.parametrize("case", list(REPLAY_COLLISIONS))
def test_sequential_collisions_are_refused_and_change_nothing(world, case):
    w = world
    user, stored = account(w)
    other = account(w)
    k = key()
    issue(w, *k, user, stored)
    before = sessions(w, user)
    attempt, owner = REPLAY_COLLISIONS[case](k, other)
    target, verified = owner or (user, stored)
    assert issue(w, *attempt, target, verified) == UNAVAILABLE
    assert sessions(w, user) == before and sessions(w, other[0]) == []


def test_replay_with_another_lifetime_is_refused(world):
    w = world
    user, stored = account(w)
    k = key()
    issue(w, *k, user, stored)
    before = sessions(w, user)
    assert issue(w, *k, user, stored, days=DAYS + 1) == UNAVAILABLE
    assert issue(w, *k, user, stored, days=DAYS - 1) == UNAVAILABLE
    assert sessions(w, user) == before


def _shift_back(w, user, k):
    # Expire the session without changing its stored lifetime.
    admin_sql(w, "UPDATE kalillac.account_sessions SET created_at=created_at-interval '15 days',"
                 " expires_at=expires_at-interval '15 days' WHERE id=:s", s=k[0])


REPLAY_AFTER_CHANGE = {
    "password-changed": lambda w, u, h, k: admin_sql(w, "UPDATE kalillac.users SET password_hash=:n WHERE id=:u",
                                                     n=h + "-new", u=u),
    "inactive": lambda w, u, h, k: set_user(w, u, "is_active=false"),
    "unverified": lambda w, u, h, k: set_user(w, u, "email_verified_at=NULL"),
    "quarantined": lambda w, u, h, k: set_user(w, u, "quarantined_at=clock_timestamp()"),
    "deletion-requested": lambda w, u, h, k: set_user(w, u, "deletion_requested_at=clock_timestamp()"),
    "reset-required": lambda w, u, h, k: set_user(w, u, "password_reset_required=true"),
    "session-revoked": lambda w, u, h, k: admin_sql(w, "UPDATE kalillac.account_sessions SET revoked_at="
                                                       "clock_timestamp() WHERE id=:s", s=k[0]),
    "session-expired": lambda w, u, h, k: _shift_back(w, u, k),
    "revoke-all": lambda w, u, h, k: revoke(w, uuid.uuid4(), k, "all"),
    "epoch-reaches-creation": lambda w, u, h, k: set_user(
        w, u, f"sessions_valid_after=(SELECT created_at FROM kalillac.account_sessions WHERE id='{k[0]}')"),
}


@pytest.mark.parametrize("case", list(REPLAY_AFTER_CHANGE))
def test_replay_after_any_state_change_is_not_authenticated(world, case):
    w = world
    user, stored = account(w)
    k = key()
    assert issue(w, *k, user, stored)[0] == "issued"
    REPLAY_AFTER_CHANGE[case](w, user, stored, k)
    before = sessions(w, user)
    assert issue(w, *k, user, stored) == DENIED
    assert sessions(w, user) == before and len(before) == 1


def test_concurrent_identical_requests_serialize_into_one_session_and_a_replay(world):
    w = world
    user, stored = account(w)
    k = key()
    with w.admin_engine.connect() as first:
        first.begin()
        original = issue(w, *k, user, stored, c=first)
        second = Call(w, lambda c: issue(w, *k, user, stored, c=c)).started()
        wait_blocked(w, second.pid)
        first.commit()
    second.join(15)
    assert second.error is None and second.result == ("issued", original[1], original[2], True)
    assert len(sessions(w, user)) == 1


@pytest.mark.parametrize("finish", ["commit", "rollback"])
@pytest.mark.parametrize("collision", ["session-id", "token-hash"])
def test_concurrent_cross_account_collision_never_creates_a_second_session(world, collision, finish):
    w = world
    user, stored = account(w)
    other, other_stored = account(w)
    k = key()
    attempt = (k[0], secrets.token_hex(32)) if collision == "session-id" else (uuid.uuid4(), k[1])
    with w.admin_engine.connect() as first:
        first.begin()
        assert issue(w, *k, user, stored, c=first)[0] == "issued"
        loser = Call(w, lambda c: issue(w, *attempt, other, other_stored, c=c)).started()
        wait_blocked(w, loser.pid)
        getattr(first, finish)()
    loser.join(15)
    assert loser.error is None
    if finish == "commit":
        # The uniqueness violation rolled the loser's whole attempt back.
        assert loser.result == UNAVAILABLE and sessions(w, other) == [] and len(sessions(w, user)) == 1
    else:
        assert loser.result[0] == "issued" and len(sessions(w, other)) == 1 and sessions(w, user) == []
    # Nothing of the attempt holds the other account: it issues at once.
    assert issue(w, *key(), other, other_stored, settings=("SET LOCAL lock_timeout = '1s'",))[0] == "issued"


# --- revoke-all and the epoch ------------------------------------------------------------------

def test_new_revoke_all_advances_the_epoch_once_to_its_effective_time(world):
    w = world
    user, stored = account(w)
    keys = [key(), key()]
    for k in keys:
        issue(w, *k, user, stored)
    operation = uuid.uuid4()
    assert revoke(w, operation, keys[0], "all") == ("revoked", None, False)
    occurred_at = [e for e in security_state(w)[0] if e[0] == operation][0][3]
    assert epoch(w, user) == occurred_at
    assert all(s[2] <= occurred_at and s[4] == occurred_at for s in sessions(w, user))
    # The replay returns before any write: the epoch does not move again.
    assert revoke(w, operation, keys[0], "all") == ("revoked", None, True)
    assert epoch(w, user) == occurred_at


def test_revoke_all_behind_a_future_epoch_advances_it_by_one_microsecond(world):
    w = world
    user, stored = account(w)
    set_user(w, user, "sessions_valid_after=clock_timestamp()+interval '1 hour'")
    old = epoch(w, user)
    k = key()
    assert issue(w, *k, user, stored)[1] == old + MICRO
    assert revoke(w, uuid.uuid4(), k, "all")[0] == "revoked"
    assert epoch(w, user) == old + MICRO
    assert authorized(w, user, k) == 401


def test_revoke_others_never_changes_the_epoch(world):
    w = world
    user, stored = account(w)
    keys = [key(), key()]
    for k in keys:
        issue(w, *k, user, stored)
    assert revoke(w, uuid.uuid4(), keys[0], "others")[0] == "revoked"
    assert epoch(w, user) is None
    set_user(w, user, "sessions_valid_after=clock_timestamp()-interval '1 hour'")
    before = epoch(w, user)
    assert revoke(w, uuid.uuid4(), keys[0], "others")[0] == "revoked"
    assert epoch(w, user) == before


def test_future_dated_sessions_do_not_push_the_epoch_and_are_revoked_by_revoked_at(world):
    w = world
    user, stored = account(w)
    k = key()
    issue(w, *k, user, stored)
    future = key()
    admin_sql(w, "INSERT INTO kalillac.account_sessions(id,user_id,token_hash,created_at,expires_at)"
                 " VALUES (:s,:u,:t,clock_timestamp()+interval '1 day',clock_timestamp()+interval '30 days')",
              s=future[0], u=user, t=future[1])
    operation = uuid.uuid4()
    assert revoke(w, operation, k, "all")[0] == "revoked"
    occurred_at = [e for e in security_state(w)[0] if e[0] == operation][0][3]
    rows = {s[0]: s for s in sessions(w, user)}
    assert epoch(w, user) == occurred_at < rows[future[0]][2]
    assert rows[future[0]][4] == occurred_at and authorized(w, user, future) == 401


def test_session_issued_before_a_revoke_all_is_invalid_after_it(world):
    w = world
    user, stored = account(w)
    original, earlier = key(), key()
    issue(w, *original, user, stored)
    first = issue(w, *earlier, user, stored)
    assert revoke(w, uuid.uuid4(), original, "all")[0] == "revoked"
    assert first[1] <= epoch(w, user)
    assert issue(w, *earlier, user, stored) == DENIED and authorized(w, user, earlier) == 401


def test_session_issued_after_a_revoke_all_is_valid(world):
    w = world
    user, stored = account(w)
    original, later = key(), key()
    issue(w, *original, user, stored)
    assert revoke(w, uuid.uuid4(), original, "all")[0] == "revoked"
    outcome, issued_at, _, _ = issue(w, *later, user, stored)
    assert outcome == "issued" and issued_at > epoch(w, user) and authorized(w, user, later) is True


def test_issuance_waiting_on_a_revoke_all_lands_after_its_epoch(world):
    w = world
    user, stored = account(w)
    original, later = key(), key()
    issue(w, *original, user, stored)
    with w.admin_engine.connect() as first:
        first.begin()
        assert revoke(w, uuid.uuid4(), original, "all", c=first)[0] == "revoked"
        waiting = Call(w, lambda c: issue(w, *later, user, stored, c=c)).started()
        wait_blocked(w, waiting.pid)
        first.commit()
    waiting.join(15)
    assert waiting.error is None and waiting.result[0] == "issued"
    assert waiting.result[1] > epoch(w, user) and authorized(w, user, later) is True


def test_revoke_all_waiting_on_an_issuance_revokes_and_outdates_it(world):
    w = world
    user, stored = account(w)
    original, pending = key(), key()
    issue(w, *original, user, stored)
    with w.admin_engine.connect() as first:
        first.begin()
        issued = issue(w, *pending, user, stored, c=first)
        waiting = Call(w, lambda c: revoke(w, uuid.uuid4(), original, "all", c=c)).started()
        wait_blocked(w, waiting.pid)
        first.commit()
    waiting.join(15)
    assert waiting.error is None and waiting.result[0] == "revoked"
    row = {s[0]: s for s in sessions(w, user)}[pending[0]]
    assert row[4] is not None and issued[1] <= epoch(w, user)
    assert issue(w, *pending, user, stored) == DENIED and authorized(w, user, pending) == 401


def test_legacy_runtime_session_dml_remains_outside_the_guarantee(world):
    """The documented limitation: R keeps direct session DML, so it can still
    insert a session that never passed issuance's gates (here, for an account
    that must reset its password). No production-wide guarantee is claimed
    until V1-02E removes that route."""
    w = world
    user, stored = account(w)
    set_user(w, user, "password_reset_required=true")
    assert issue(w, *key(), user, stored) == DENIED
    legacy = key()
    with w.runtime_engine.begin() as c:
        c.execute(text("INSERT INTO kalillac.account_sessions(id,user_id,token_hash,expires_at)"
                       " VALUES (:s,:u,:t,clock_timestamp()+interval '1 day')"),
                  {"s": legacy[0], "u": user, "t": legacy[1]})
    assert authorized(w, user, legacy) is True


# --- failure and rollback ----------------------------------------------------------------------

FAILURES = {
    "pg-cancel-backend": ((), "57014"),
    "lock-timeout": (("SET LOCAL lock_timeout = '200ms'",), "55P03"),
    "statement-timeout": (("SET LOCAL statement_timeout = '200ms'",), "57014"),
}


@pytest.mark.parametrize("case", list(FAILURES))
def test_interrupted_issuance_rolls_back_everything(world, case):
    w = world
    settings, state = FAILURES[case]
    user, stored = account(w)
    k = key()
    blocker = holding(w, f"SELECT 1 FROM kalillac.users WHERE id='{user}' FOR UPDATE", 3)
    call = Call(w, lambda c: issue(w, *k, user, stored, c=c, settings=settings)).started()
    if case == "pg-cancel-backend":
        wait_blocked(w, call.pid)
        admin_sql(w, "SELECT pg_cancel_backend(:p)", p=call.pid)
    call.join(15)
    blocker.join(15)
    assert isinstance(call.error, DBAPIError) and call.error.orig.sqlstate == state
    assert stored not in reported(call.error) and k[1] not in reported(call.error)
    assert sessions(w, user) == [] and epoch(w, user) is None and locks_left(w) == 0
    assert issue(w, *key(), user, stored)[0] == "issued"


def test_failure_after_the_session_insert_rolls_back_everything(world):
    w = world
    user, stored = account(w)
    admin_sql(w, "CREATE FUNCTION public.v1_fault() RETURNS trigger LANGUAGE plpgsql AS"
                 " $f$BEGIN RAISE EXCEPTION 'injected fault after the session insert'; END$f$",
              "CREATE TRIGGER v1_fault AFTER INSERT ON kalillac.account_sessions"
              " FOR EACH ROW EXECUTE FUNCTION public.v1_fault()")
    k = key()
    with pytest.raises(DBAPIError) as error:
        issue(w, *k, user, stored)
    assert error.value.orig.sqlstate == "P0001"
    assert stored not in reported(error.value) and k[1] not in reported(error.value)
    assert sessions(w, user) == [] and epoch(w, user) is None and locks_left(w) == 0
    admin_sql(w, "DROP TRIGGER v1_fault ON kalillac.account_sessions", "DROP FUNCTION public.v1_fault()")
    assert issue(w, *k, user, stored)[0] == "issued"


@pytest.mark.parametrize("settings,exposed", [
    ((), False),
    (("SET LOCAL log_parameter_max_length_on_error = -1", "SET LOCAL log_parameter_max_length_on_error = 0"), False),
    (("SET LOCAL log_parameter_max_length_on_error = -1",), True),
], ids=["caller-default", "caller-pins-zero", "caller-logs-bound-values"])
def test_only_the_callers_bind_time_setting_keeps_bound_values_out_of_error_reports(world, settings, exposed):
    """PostgreSQL renders bound parameters at Bind, before the function's own
    configuration applies, so the function cannot protect its caller's bound
    hash; a caller that logs bound values on error (V1-02E's prerequisite is
    that it never does) would expose it. Expected outcomes are returned, never
    raised, so only interruptions reach this path."""
    w = world
    user, stored = account(w)
    k = key()
    blocker = holding(w, f"SELECT 1 FROM kalillac.users WHERE id='{user}' FOR UPDATE", 3)
    with pytest.raises(DBAPIError) as error:
        issue(w, *k, user, stored, settings=("SET LOCAL statement_timeout = '200ms'", *settings))
    blocker.join(15)
    assert error.value.orig.sqlstate == "57014"
    assert (stored in reported(error.value)) is exposed and (k[1] in reported(error.value)) is exposed
    assert sessions(w, user) == []


# --- migration ---------------------------------------------------------------------------------

ISSUANCE_CREATE = [literal(MIGRATION.FUNCTION),
                   "REVOKE ALL ON FUNCTION kalillac.v1_issue_session(uuid, text, uuid, text, integer) FROM PUBLIC",
                   "GRANT EXECUTE ON FUNCTION kalillac.v1_issue_session(uuid, text, uuid, text, integer) TO {service}",
                   "ALTER FUNCTION kalillac.v1_issue_session(uuid, text, uuid, text, integer) OWNER TO {executor}"]
ISSUANCE_DROP = ["DROP FUNCTION kalillac.v1_issue_session(uuid, text, uuid, text, integer)"]
REVOCATION_ALTER = "ALTER FUNCTION kalillac.v1_revoke_sessions(uuid, uuid, text, text) "
ADDED_GRANTS = ["GRANT SELECT (password_hash, email_verified_at, password_reset_required), UPDATE (sessions_valid_after)"
                " ON kalillac.users TO {executor}",
                "GRANT INSERT (id, user_id, token_hash, created_at, expires_at) ON kalillac.account_sessions TO {executor}"]
ADDED_REVOKES = [g.replace("GRANT", "REVOKE").replace(" TO ", " FROM ") for g in ADDED_GRANTS]

PREREQUISITES = {
    "service-login": (["ALTER ROLE {service} LOGIN"], ["ALTER ROLE {service} NOLOGIN"]),
    "executor-inherit": (["ALTER ROLE {executor} INHERIT"], ["ALTER ROLE {executor} NOINHERIT"]),
    "edge-missing": (["REVOKE {executor} FROM {owner}"], ["GRANT {executor} TO {owner} WITH INHERIT FALSE, SET TRUE"]),
    "edge-admin": (["GRANT {executor} TO {owner} WITH ADMIN OPTION"], ["REVOKE ADMIN OPTION FOR {executor} FROM {owner}"]),
    "executor-holds-0014-grants": (ADDED_GRANTS, ADDED_REVOKES),
    "revocation-already-v2": ([literal(MIGRATION.REVOCATION_FUNCTION)], [literal(MIGRATION.REVOCATION_FUNCTION_V1)]),
    "revocation-extra-setting": ([REVOCATION_ALTER + "SET work_mem = '1MB'"], [REVOCATION_ALTER + "RESET work_mem"]),
    "guard-already-v2": ([guard_sql(2)], [guard_sql(1)]),
    "guard-trigger-origin-only": (["ALTER TABLE kalillac.users ENABLE TRIGGER trg_users_registration_identity"],
                                  ["ALTER TABLE kalillac.users ENABLE ALWAYS TRIGGER trg_users_registration_identity"]),
    "issuance-already-present": (ISSUANCE_CREATE, ISSUANCE_DROP),
}


@pytest.mark.parametrize("case", list(PREREQUISITES))
def test_migration_refuses_invalid_prerequisites_atomically(world, case):
    w = world
    tamper, undo = PREREQUISITES[case]
    migrate(w, PREVIOUS, down=True)
    run(w, tamper)
    before = (catalog_state(w), authority_state(w))
    with pytest.raises(DBAPIError) as error:
        migrate(w, REVISION)
    assert error.value.orig.diag.message_primary == "V1_ISSUANCE_PREREQUISITE_INVALID"
    assert (catalog_state(w), authority_state(w)) == before and before[0][0] == [(PREVIOUS,)]
    run(w, undo)
    migrate(w, REVISION)
    assert classify(w.owner_factory) == v1_schema.PREPARED


@pytest.mark.parametrize("setting,value,error", [
    (migration_roles.V1_SECURITY_SERVICE_ROLE_ENV, None, migration_roles.RoleNameRequired),
    (migration_roles.V1_SECURITY_EXECUTOR_ROLE_ENV, "", migration_roles.RoleNameRequired),
    (migration_roles.V1_SECURITY_EXECUTOR_ROLE_ENV, "Bad Name", migration_roles.InvalidRoleName),
])
def test_migration_refuses_without_valid_role_settings(world, monkeypatch, setting, value, error):
    w = world
    migrate(w, PREVIOUS, down=True)
    before = (catalog_state(w), authority_state(w))
    restore = os.environ[setting]
    if value is None:
        monkeypatch.delenv(setting)
    else:
        monkeypatch.setenv(setting, value)
    with pytest.raises(error):
        migrate(w, REVISION)
    assert (catalog_state(w), authority_state(w)) == before
    monkeypatch.setenv(setting, restore)
    migrate(w, REVISION)


def test_postcondition_failure_rolls_back_every_effect_including_temporary_create(world):
    w = world
    migrate(w, PREVIOUS, down=True)
    # Owner default privileges would add the runtime role to the new function's ACL.
    run(w, ["ALTER DEFAULT PRIVILEGES FOR ROLE {owner} GRANT EXECUTE ON FUNCTIONS TO {app}"])
    before = (catalog_state(w), authority_state(w))
    with pytest.raises(DBAPIError) as error:
        migrate(w, REVISION)
    assert error.value.orig.diag.message_primary == "V1_ISSUANCE_POSTCONDITION_INVALID"
    assert (catalog_state(w), authority_state(w)) == before
    assert "v1_issue_session" not in functions(before[1]) and before[1][4] is False
    run(w, ["ALTER DEFAULT PRIVILEGES FOR ROLE {owner} REVOKE EXECUTE ON FUNCTIONS FROM {app}"])
    migrate(w, REVISION)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def test_upgrade_cancelled_while_blocked_rolls_back_every_0014_step(world):
    w = world
    migrate(w, PREVIOUS, down=True)
    before = (catalog_state(w), authority_state(w))
    outcome = {}
    with w.migration.connect() as blocker, w.migration.connect() as worker:
        pid = worker.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        worker.rollback()
        blocker.exec_driver_sql("SET LOCAL ROLE " + w.names["owner"])
        # Every 0014 statement runs first; the version stamp then waits here.
        blocker.exec_driver_sql("LOCK TABLE kalillac.alembic_version IN EXCLUSIVE MODE")

        def upgrade():
            try:
                with worker.begin():
                    command.upgrade(_config(connection=worker), REVISION)
            except BaseException as error:
                outcome["error"] = error
        thread = threading.Thread(target=upgrade)
        thread.start()
        deadline = time.monotonic() + 15
        with w.migration.connect() as watcher:
            while not watcher.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:p AND NOT granted)"),
                                     {"p": pid}):
                assert time.monotonic() < deadline, "the upgrade never waited mid-transaction"
                watcher.rollback()
                time.sleep(0.01)
            assert "alembic_version" in watcher.scalar(text("SELECT query FROM pg_stat_activity WHERE pid=:p"),
                                                       {"p": pid})
            # By now issuance, revocation v2, guard v2 and the grants all exist in the open transaction.
            assert watcher.scalar(text("SELECT pg_cancel_backend(:p)"), {"p": pid})
        thread.join(15)
        blocker.rollback()
    assert not thread.is_alive() and outcome["error"].orig.sqlstate == "57014"
    assert (catalog_state(w), authority_state(w)) == before and before[1][4] is False
    migrate(w, REVISION)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def test_successful_migration_leaves_exactly_the_declared_authority(world):
    w = world
    service, executor, owner = w.names["service"], w.names["executor"], w.names["owner"]
    state = authority_state(w)
    installed = functions(state)
    assert installed["v1_issue_session"] == v1_schema.ISSUANCE_SOURCE_SHA256
    assert installed["v1_revoke_sessions"] == v1_schema.REVOCATION_SOURCE_SHA256_V2
    owner_, acl, config, definer = {row[0]: row[2:] for row in state[0]}["v1_issue_session"]
    assert (owner_, config, definer) == (executor, ["search_path=pg_catalog, pg_temp"], True)
    assert sorted(acl.strip("{}").split(",")) == sorted([f"{executor}=X/{executor}", f"{service}=X/{executor}"])
    # One ACL dependency per table-level grant and one per granted column.
    granted = len(v1_schema.EXECUTOR_TABLE_GRANTS) + len({
        (table, column) for (table, _), columns in v1_schema.EXECUTOR_COLUMN_GRANTS_V2.items() for column in columns})
    assert state[1] == sorted([(executor, "pg_class", "a", granted), (executor, "pg_namespace", "a", 1),
                               (executor, "pg_proc", "o", 2), (service, "pg_namespace", "a", 1),
                               (service, "pg_proc", "a", 2)])
    assert state[2] == sorted((table, column, executor, right, False)
                              for (table, right), columns in v1_schema.EXECUTOR_COLUMN_GRANTS_V2.items()
                              for column in columns)
    assert f"{executor}=U/{owner}" in state[3] and f"{service}=U/{owner}" in state[3] and state[4] is False
    assert ("trg_users_registration_identity", "A", "kalillac.registration_user_identity_guard()") in state[5]
    assert [e for e in state[6] if executor in e[:2] or service in e[:2]] == [(executor, owner, False, True, False)]


def _guard_row(w):
    with w.admin_engine.connect() as c:
        return tuple(c.execute(text(
            "SELECT p.oid,pg_get_userbyid(p.proowner),p.proacl::text,p.proconfig,p.prosecdef,p.provolatile::text,"
            "p.proparallel::text,t.oid,t.tgfoid,t.tgenabled::text,pg_get_triggerdef(t.oid),p.prosrc FROM pg_proc p"
            " JOIN pg_trigger t ON t.tgfoid=p.oid WHERE p.oid='kalillac.registration_user_identity_guard()'"
            "::regprocedure")).one())


def test_guard_v2_keeps_the_owner_acl_configuration_and_trigger_binding(world):
    w = world
    registration = w.names["registration"]
    v2 = _guard_row(w)
    migrate(w, PREVIOUS, down=True)
    v1 = _guard_row(w)
    assert v2[:-1] == v1[:-1] and v2[1] == w.names["owner"] and v2[9] == "A"
    assert v1[-1] == guard_functions(registration, "unused")["registration_user_identity_guard"].split("$guard$")[1]
    assert v2[-1] == identity_guard_v2(registration).split("$guard$")[1]
    migrate(w, REVISION)
    assert _guard_row(w) == v2


GUARD_ON = " ON FUNCTION kalillac.registration_user_identity_guard() "
# Non-canonical guard ACLs, each with its exact effect and the operator's own
# removal, which restores the canonical owner-only ACL. PUBLIC is the required
# case; the named role and the owner's grant option share the same assertion.
GUARD_ACLS = {
    "public-execute": ("GRANT EXECUTE" + GUARD_ON + "TO PUBLIC", "REVOKE EXECUTE" + GUARD_ON + "FROM PUBLIC",
                       lambda o, a: [(o, "EXECUTE", False, o), ("PUBLIC", "EXECUTE", False, o)]),
    "named-role-execute": ("GRANT EXECUTE" + GUARD_ON + "TO {app}", "REVOKE EXECUTE" + GUARD_ON + "FROM {app}",
                           lambda o, a: [(o, "EXECUTE", False, o), (a, "EXECUTE", False, o)]),
    "owner-grant-option": ("GRANT EXECUTE" + GUARD_ON + "TO {owner} WITH GRANT OPTION",
                           "REVOKE GRANT OPTION FOR EXECUTE" + GUARD_ON + "FROM {owner}",
                           lambda o, a: [(o, "EXECUTE", True, o)]),
}


def guard_acl(w):
    with w.admin_engine.connect() as c:
        return sorted(tuple(r) for r in c.execute(text(
            "SELECT CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END,x.privilege_type,"
            "x.is_grantable,pg_get_userbyid(x.grantor) FROM pg_proc p, aclexplode(p.proacl) x"
            " WHERE p.oid='kalillac.registration_user_identity_guard()'::regprocedure")))


def _guard_acl_refused(w, target, message, observe):
    """Each non-canonical guard ACL refuses the migration to target before
    anything changes, stays in place, is refused by the classifier too, and
    the operator's removal restores the canonical catalog exactly."""
    owner, app = w.names["owner"], w.names["app"]
    canonical = (_guard_row(w), guard_acl(w))
    assert canonical[1] == [(owner, "EXECUTE", False, owner)] and canonical[0][2] == f"{{{owner}=X/{owner}}}"
    for case, (grant, removal, effect) in GUARD_ACLS.items():
        run(w, [grant])
        tampered = guard_acl(w)
        assert tampered == sorted(effect(owner, app)), case
        before = (catalog_state(w), authority_state(w), observe(), _guard_row(w))
        with pytest.raises(DBAPIError) as error:
            migrate(w, target, down=target == PREVIOUS)
        assert error.value.orig.diag.message_primary == message, case
        assert (catalog_state(w), authority_state(w), observe(), _guard_row(w)) == before, case
        assert guard_acl(w) == tampered and before[3][:2] == canonical[0][:2] and before[3][3:] == canonical[0][3:], case
        refused(w)
        run(w, [removal])
        assert (_guard_row(w), guard_acl(w)) == canonical, case
    return before


def test_upgrade_refuses_a_non_canonical_identity_guard_acl_atomically(world):
    w = world
    migrate(w, PREVIOUS, down=True)
    user, stored = account(w)
    keys = [key(), key()]
    for k in keys:
        admin_sql(w, "INSERT INTO kalillac.account_sessions(id,user_id,token_hash,expires_at)"
                     " VALUES (:s,:u,:t,clock_timestamp()+interval '1 day')", s=k[0], u=user, t=k[1])
    assert revoke(w, uuid.uuid4(), keys[0], "others")[0] == "revoked"
    before = _guard_acl_refused(w, REVISION, "V1_ISSUANCE_PREREQUISITE_INVALID",
                                lambda: (security_state(w), sessions(w, user), epoch(w, user)))
    # Still exactly 0013: no issuance, revocation v1, guard v1, no 0014 grant, no temporary CREATE.
    state = before[1]
    assert before[0][0] == [(PREVIOUS,)] and "v1_issue_session" not in functions(state)
    assert functions(state)["v1_revoke_sessions"] == v1_schema.REVOCATION_SOURCE_SHA256 and state[4] is False
    assert state[2] == sorted((table, column, w.names["executor"], right, False)
                              for (table, right), columns in v1_schema.EXECUTOR_COLUMN_GRANTS.items()
                              for column in columns)
    assert before[3][-1] == guard_functions(w.names["registration"],
                                            "unused")["registration_user_identity_guard"].split("$guard$")[1]
    events, epoch_value = before[2][0][0], before[2][2]
    assert len(events) == 1 and epoch_value is None
    migrate(w, REVISION)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def test_downgrade_refuses_a_non_canonical_identity_guard_acl_atomically(world):
    w = world
    user, stored = account(w)
    original, kept = key(), key()
    issue(w, *original, user, stored)
    assert revoke(w, uuid.uuid4(), original, "all")[0] == "revoked"
    assert issue(w, *kept, user, stored)[0] == "issued"
    before = _guard_acl_refused(w, PREVIOUS, "V1_ISSUANCE_POSTCONDITION_INVALID",
                                lambda: (security_state(w), sessions(w, user), epoch(w, user)))
    # Still exactly 0014: issuance, revocation v2, guard v2 and grants v2.
    state = before[1]
    assert before[0][0] == [(REVISION,)]
    assert functions(state)["v1_issue_session"] == v1_schema.ISSUANCE_SOURCE_SHA256
    assert functions(state)["v1_revoke_sessions"] == v1_schema.REVOCATION_SOURCE_SHA256_V2
    assert before[3][-1] == identity_guard_v2(w.names["registration"]).split("$guard$")[1]
    assert state[2] == sorted((table, column, w.names["executor"], right, False)
                              for (table, right), columns in v1_schema.EXECUTOR_COLUMN_GRANTS_V2.items()
                              for column in columns)
    rows, advanced = before[2][1], before[2][2]
    assert len(rows) == 2 and advanced is not None
    migrate(w, PREVIOUS, down=True)
    assert classify(w.owner_factory) == v1_schema.PREPARED
    assert sessions(w, user) == rows and epoch(w, user) == advanced


def test_pristine_downgrade_restores_exactly_what_0013_installs_and_reupgrade_succeeds(world):
    w = world
    at_0014 = authority_state(w)
    migrate(w, PREVIOUS, down=True)
    restored = authority_state(w)
    assert classify(w.owner_factory) == v1_schema.PREPARED
    migrate(w, BASE, down=True)
    migrate(w, PREVIOUS)
    assert authority_state(w) == restored
    assert "v1_issue_session" not in functions(restored)
    assert functions(restored)["v1_revoke_sessions"] == v1_schema.REVOCATION_SOURCE_SHA256
    assert restored[4] is False
    migrate(w, REVISION)
    assert authority_state(w) == at_0014 and classify(w.owner_factory) == v1_schema.PREPARED


def test_downgrade_keeps_issued_sessions_and_advanced_epochs(world):
    w = world
    user, stored = account(w)
    original, kept = key(), key()
    issue(w, *original, user, stored)
    assert revoke(w, uuid.uuid4(), original, "all")[0] == "revoked"
    issue(w, *kept, user, stored)
    rows, advanced = sessions(w, user), epoch(w, user)
    migrate(w, PREVIOUS, down=True)
    assert sessions(w, user) == rows and epoch(w, user) == advanced
    assert classify(w.owner_factory) == v1_schema.PREPARED and authorized(w, user, kept) is True
    migrate(w, REVISION)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def test_downgrade_refuses_a_modified_0014_catalog_atomically(world):
    w = world
    run(w, ["GRANT EXECUTE ON FUNCTION kalillac.v1_issue_session(uuid, text, uuid, text, integer) TO {app}"])
    before = (catalog_state(w), authority_state(w))
    with pytest.raises(DBAPIError) as error:
        migrate(w, PREVIOUS, down=True)
    assert error.value.orig.diag.message_primary == "V1_ISSUANCE_POSTCONDITION_INVALID"
    assert (catalog_state(w), authority_state(w)) == before and before[0][0] == [(REVISION,)]


# --- catalog qualification ---------------------------------------------------------------------

def test_canonical_0014_catalog_classifies_prepared_and_startup_still_refuses(world):
    w = world
    assert classify(w.owner_factory) == v1_schema.PREPARED
    with pytest.raises(v1_schema.V1SchemaRefused, match="authority_state_unreadable"):
        classify(w.runtime_factory)
    with pytest.raises(V1SecurityConfigError, match=r"\(prepared_inactive\)"):
        load_v1_startup(lambda: w.owner_factory, ENV)


def test_all_three_prepared_catalogs_are_accepted(world):
    w = world
    for target, down in ((REVISION, False), (PREVIOUS, True), (BASE, True), (PREVIOUS, False), (REVISION, False)):
        migrate(w, target, down=down)
        assert classify(w.owner_factory) == v1_schema.PREPARED, target


def test_installed_definitions_match_the_classifier_constants(world):
    w = world
    with w.admin_engine.connect() as c:
        rows = {name: tuple(rest) for name, *rest in c.execute(text("""SELECT p.proname,
            pg_get_function_identity_arguments(p.oid),pg_get_function_result(p.oid),l.lanname,p.provolatile::text,
            p.prosecdef,p.proleakproof,p.proisstrict,p.proparallel::text,p.prokind::text,p.proconfig,p.procost::float8,
            p.prorows::float8,encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')
            FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='kalillac' AND p.proname IN ('v1_issue_session','v1_revoke_sessions')"""))}
    assert rows["v1_issue_session"] == (*v1_schema.ISSUANCE_DEFINITION, v1_schema.ISSUANCE_SOURCE_SHA256)
    assert rows["v1_revoke_sessions"] == (*v1_schema.REVOCATION_DEFINITION, v1_schema.REVOCATION_SOURCE_SHA256_V2)


ISSUE_ON = " ON FUNCTION kalillac.v1_issue_session(uuid, text, uuid, text, integer) "
ISSUE_ALTER = "ALTER FUNCTION kalillac.v1_issue_session(uuid, text, uuid, text, integer) "

# Every partial, mixed or modified 0014 catalog is refused.
TAMPERS = {
    "issuance-body": [literal(MIGRATION.FUNCTION.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")
                              .replace("p_lifetime_days > 366", "p_lifetime_days > 3660"))],
    "issuance-owner": [ISSUE_ALTER + "OWNER TO {owner}"],
    "issuance-acl-runtime": ["GRANT EXECUTE" + ISSUE_ON + "TO {app}"],
    "issuance-acl-registration": ["GRANT EXECUTE" + ISSUE_ON + "TO {registration}"],
    "issuance-acl-public": ["GRANT EXECUTE" + ISSUE_ON + "TO PUBLIC"],
    "issuance-acl-service-missing": ["REVOKE EXECUTE" + ISSUE_ON + "FROM {service}"],
    "issuance-acl-grant-option": ["SET LOCAL ROLE {executor}", "GRANT EXECUTE" + ISSUE_ON + "TO {service} WITH GRANT OPTION"],
    "issuance-search-path": [ISSUE_ALTER + "SET search_path = public, pg_temp"],
    "issuance-extra-setting": [ISSUE_ALTER + "SET log_parameter_max_length_on_error = 0"],
    "issuance-security-invoker": [ISSUE_ALTER + "SECURITY INVOKER"],
    "issuance-stable": [ISSUE_ALTER + "STABLE"],
    "issuance-strict": [ISSUE_ALTER + "STRICT"],
    "issuance-leakproof": [ISSUE_ALTER + "LEAKPROOF"],
    "issuance-parallel": [ISSUE_ALTER + "PARALLEL SAFE"],
    "issuance-overload": ["CREATE FUNCTION kalillac.v1_issue_session(p uuid) RETURNS void LANGUAGE sql AS 'SELECT'"],
    "issuance-dropped": ISSUANCE_DROP,
    "revocation-v1-with-issuance": [literal(MIGRATION.REVOCATION_FUNCTION_V1)],
    "guard-v1-with-issuance": [guard_sql(1)],
    "guard-trigger-origin-only": ["ALTER TABLE kalillac.users ENABLE TRIGGER trg_users_registration_identity"],
    "executor-0013-grants": ADDED_REVOKES,
    "executor-password-update": ["GRANT UPDATE (password_hash) ON kalillac.users TO {executor}"],
    "executor-quarantine-update": ["GRANT UPDATE (quarantined_at) ON kalillac.users TO {executor}"],
    "executor-email-read": ["GRANT SELECT (email) ON kalillac.users TO {executor}"],
    "executor-insert-revoked-at": ["GRANT INSERT (revoked_at) ON kalillac.account_sessions TO {executor}"],
    "executor-session-delete": ["GRANT DELETE ON kalillac.account_sessions TO {executor}"],
    "executor-schema-create": ["GRANT CREATE ON SCHEMA kalillac TO {executor}"],
    "executor-owns-extra": ["CREATE TABLE public.v1_owned (x int)", "ALTER TABLE public.v1_owned OWNER TO {executor}"],
    "runtime-epoch-update": ["GRANT UPDATE (sessions_valid_after) ON kalillac.users TO {app}"],
    "registration-epoch-update": ["GRANT UPDATE (sessions_valid_after) ON kalillac.users TO {registration}"],
    "public-epoch-update": ["GRANT UPDATE (sessions_valid_after) ON kalillac.users TO PUBLIC"],
    "service-epoch-update": ["GRANT UPDATE (sessions_valid_after) ON kalillac.users TO {service}"],
    "service-session-insert": ["GRANT INSERT (id) ON kalillac.account_sessions TO {service}"],
    "service-login": ["ALTER ROLE {service} LOGIN"],
    "edge-inherit": ["GRANT {executor} TO {owner} WITH INHERIT TRUE"],
}


@pytest.mark.parametrize("case", list(TAMPERS))
def test_classifier_refuses_every_modified_0014_catalog(world, case):
    w = world
    assert classify(w.owner_factory) == v1_schema.PREPARED
    run(w, TAMPERS[case])
    refused(w)


# Mixed catalogs below 0014: a 0014 piece without the rest.
MIXED = {
    "guard-v2-at-0013": (PREVIOUS, [guard_sql(2)]),
    "issuance-at-0013": (PREVIOUS, ISSUANCE_CREATE),
    "revocation-v2-at-0013": (PREVIOUS, [literal(MIGRATION.REVOCATION_FUNCTION)]),
    "executor-0014-grants-at-0013": (PREVIOUS, ADDED_GRANTS),
    "guard-v2-at-0012": (BASE, [guard_sql(2)]),
}


@pytest.mark.parametrize("case", list(MIXED))
def test_classifier_refuses_mixed_catalogs_below_0014(world, case):
    w = world
    target, tamper = MIXED[case]
    migrate(w, target, down=True)
    assert classify(w.owner_factory) == v1_schema.PREPARED
    run(w, tamper)
    refused(w)
