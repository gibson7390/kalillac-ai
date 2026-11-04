"""V1-02B private session-revocation authority on real disposable PostgreSQL 16.

Uses the registration harness (loopback/disposable only; CI's REQUIRE_POSTGRES
switch turns an absent database into a failure). S stays NOLOGIN: it is
exercised only through SET LOCAL ROLE from the disposable superuser, which
also seeds fixtures and injects faults. No application path calls it.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import os
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

import kalillac_accounts.v1_schema as v1_schema
from kalillac_accounts.auth import AccountIdentity, RequestRejected, authorize_original_identity
from kalillac_accounts.v1_security import FLAG, V1SecurityConfigError, load_v1_startup
from kalillac_db import migration_roles
from registration_test_support import postgres_world
from test_v1_schema_postgres import (ENV, catalog_state, classify, memberships, migrate, refused, roles,  # noqa: F401
                                     run, sqlstate)

REVISION = "0013_v1_session_revocation"
PREVIOUS = "0012_v1_account_security"
SIGNATURE = "kalillac.v1_revoke_sessions(uuid,uuid,text,text)"
# A fixed effective time after every real clock reading in these tests: an
# admission row last seen at ANCHOR makes the function's effective time exactly ANCHOR.
ANCHOR = datetime(2031, 1, 1, tzinfo=timezone.utc)


@contextmanager
def _world(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    with postgres_world(monkeypatch, active=False) as w:
        url = make_url(os.environ["KALILLAC_REGISTRATION_TEST_POSTGRES_URL"]).set(database=w.migration.url.database)
        w.admin_engine = create_engine(url, hide_parameters=True, pool_size=12, max_overflow=0)
        w.owner_engine = create_engine(w.migration.url, hide_parameters=True,
                                       connect_args={"options": "-c role=" + w.names["owner"]})
        w.owner_factory = sessionmaker(bind=w.owner_engine)
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


def account(w, sessions=2):
    """A verified active user with live sessions; returns (user_id, [(session_id, token_hash)])."""
    user, keys = uuid.uuid4(), []
    with w.admin_engine.begin() as c:
        c.execute(text("INSERT INTO kalillac.users(id,email,password_hash,is_active,email_verified_at)"
                       " VALUES (:i,:e,'synthetic-hash',true,clock_timestamp())"),
                  {"i": user, "e": f"v102b-{user.hex[:16]}@example.test"})
        for _ in range(sessions):
            key = (uuid.uuid4(), hashlib.sha256(uuid.uuid4().bytes).hexdigest())
            c.execute(text("INSERT INTO kalillac.account_sessions(id,user_id,token_hash,expires_at)"
                           " VALUES (:s,:u,:t,clock_timestamp()+interval '1 day')"),
                      {"s": key[0], "u": user, "t": key[1]})
            keys.append(key)
    return user, keys


def admin_sql(w, *statements, **params):
    with w.admin_engine.begin() as c:
        for statement in statements:
            c.execute(text(statement), params)


def set_user(w, user, assignments):
    """Owner-level fixture edit; the 0011 identity guard is paused only for it."""
    admin_sql(w, "ALTER TABLE kalillac.users DISABLE TRIGGER trg_users_registration_identity",
              f"UPDATE kalillac.users SET {assignments} WHERE id=:u",
              "ALTER TABLE kalillac.users ENABLE ALWAYS TRIGGER trg_users_registration_identity", u=user)


def revoke(w, operation, key, scope, c=None, settings=()):
    """Call the function as the NOLOGIN service role S."""
    def call(c):
        for setting in settings:
            c.exec_driver_sql(setting)
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["service"])
        return tuple(c.execute(text("SELECT * FROM kalillac.v1_revoke_sessions(:o,:s,:t,:p)"),
                               {"o": operation, "s": key[0], "t": key[1], "p": scope}).one())
    if c is not None:
        return call(c)
    with w.admin_engine.begin() as c:
        return call(c)


def security_state(w):
    with w.admin_engine.connect() as c:
        return (c.execute(text("SELECT operation_id,user_id,event_type,occurred_at FROM kalillac.account_security_events"
                               " ORDER BY 1")).all(),
                c.execute(text("SELECT entry_slot,user_id,manual_charges,recovery_charges,last_seen_at"
                               " FROM kalillac.account_security_admission ORDER BY 1")).all(),
                c.execute(text("SELECT admission_class,charges,last_seen_at FROM kalillac.account_security_admission_aggregate"
                               " ORDER BY 1")).all(),
                c.execute(text("SELECT id,revoked_at FROM kalillac.account_sessions ORDER BY 1")).all())


def revoked(w, *keys):
    with w.admin_engine.connect() as c:
        return [c.scalar(text("SELECT revoked_at IS NOT NULL FROM kalillac.account_sessions WHERE id=:s"), {"s": k[0]})
                for k in keys]


def authorize(w, user, key):
    """V1-01's fresh original-session authorization, as R."""
    with w.runtime_factory() as s, s.begin():
        return authorize_original_identity(s, AccountIdentity(user, key[0], key[1]))


def authority_state(w):
    service, executor = w.names["service"], w.names["executor"]
    with w.admin_engine.connect() as c:
        return (c.scalar(text("SELECT to_regprocedure(:f)::text"), {"f": SIGNATURE}),
                c.execute(text("SELECT pg_get_userbyid(refobjid),classid::regclass::text,objid,objsubid,deptype"
                               " FROM pg_shdepend WHERE refclassid='pg_authid'::regclass"
                               " AND pg_get_userbyid(refobjid) IN (:s,:e) ORDER BY 1,2,3,4"),
                          {"s": service, "e": executor}).all(),
                c.scalar(text("SELECT nspacl::text FROM pg_namespace WHERE nspname='kalillac'")),
                c.scalar(text("SELECT has_schema_privilege(r.oid,'kalillac','CREATE') FROM pg_roles r WHERE r.rolname=:e"),
                         {"e": executor}),
                memberships(w))


class Call(threading.Thread):
    """Run work(connection) in its own transaction; expose its backend PID."""

    def __init__(self, w, work, engine=None):
        super().__init__(daemon=True)
        self.engine, self.work = engine or w.admin_engine, work
        self.pid = self.result = self.error = None
        self.ready = threading.Event()

    def run(self):
        try:
            with self.engine.begin() as c:
                self.pid = c.scalar(text("SELECT pg_backend_pid()"))
                self.ready.set()
                self.result = self.work(c)
        except BaseException as error:
            self.error = error
        finally:
            self.ready.set()

    def started(self):
        self.start()
        assert self.ready.wait(10)
        return self


def holding(w, statement, seconds):
    """Hold a row lock taken by statement in another transaction for seconds."""
    locked = threading.Event()

    def work(c):
        c.exec_driver_sql(statement)
        locked.set()
        time.sleep(seconds)
    call = Call(w, work).started()
    assert locked.wait(10)
    return call


def wait_blocked(w, pid):
    deadline = time.monotonic() + 15
    with w.admin_engine.connect() as c:
        while not c.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:p AND NOT granted)"), {"p": pid}):
            assert time.monotonic() < deadline, "the call never waited on a lock"
            c.rollback()
            time.sleep(0.01)


# --- authority boundary ------------------------------------------------------------------------

def test_service_executes_through_set_role_while_nologin(world):
    w = world
    with w.admin_engine.connect() as c:
        assert c.execute(text("SELECT rolname, rolcanlogin, rolinherit FROM pg_roles WHERE rolname IN (:s,:e)"
                              " ORDER BY 1"), {"s": w.names["service"], "e": w.names["executor"]}).all() == sorted(
            [(w.names["service"], False, False), (w.names["executor"], False, False)])
    user, keys = account(w)
    assert revoke(w, uuid.uuid4(), keys[0], "others") == ("revoked", None, False)
    assert revoked(w, *keys) == [False, True]


def test_runtime_registration_and_public_cannot_execute(world):
    w = world
    user, keys = account(w)
    call = "SELECT * FROM kalillac.v1_revoke_sessions(gen_random_uuid(),:s,:t,'all')"
    for engine in (w.runtime_engine, w.engine):
        with engine.connect() as c:
            assert sqlstate(c, call, {"s": keys[0][0], "t": keys[0][1]}) == "42501"
    with w.admin_engine.connect() as c:
        for principal in ("public", w.names["app"], w.names["registration"], w.names["owner"]):
            assert not c.scalar(text("SELECT has_function_privilege(:p,:f,'EXECUTE')"), {"p": principal, "f": SIGNATURE})
    assert revoked(w, *keys) == [False, False] and security_state(w)[0] == []


def test_service_has_no_table_authority(world):
    w = world
    user, keys = account(w)
    with w.admin_engine.begin() as c:
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["service"])
        for statement in ("SELECT 1 FROM kalillac.users", "SELECT 1 FROM kalillac.account_sessions",
                          "UPDATE kalillac.account_sessions SET revoked_at=clock_timestamp()",
                          "INSERT INTO kalillac.account_security_events SELECT gen_random_uuid(),id,'session_revoke_all',"
                          "clock_timestamp() FROM kalillac.users",
                          "DELETE FROM kalillac.account_security_admission",
                          "SELECT 1 FROM kalillac.account_security_admission_aggregate"):
            assert sqlstate(c, statement) == "42501", statement
    assert revoked(w, *keys) == [False, False]


def test_executor_is_nologin_and_unreachable(world):
    w = world
    service, executor = w.names["service"], w.names["executor"]
    with w.admin_engine.connect() as c:
        for principal in (w.names["app"], w.names["registration"], service):
            for mode in ("MEMBER", "USAGE", "SET"):
                assert not c.scalar(text("SELECT pg_has_role(:p,:e,:m)"), {"p": principal, "e": executor, "m": mode})
        # The only route to E is the operator's owner->E edge with SET only.
        assert c.execute(text("SELECT pg_get_userbyid(roleid),pg_get_userbyid(member),admin_option,inherit_option,"
                              "set_option FROM pg_auth_members WHERE pg_get_userbyid(roleid) IN (:s,:e)"
                              " OR pg_get_userbyid(member) IN (:s,:e)"), {"s": service, "e": executor}).all() == [
            (executor, w.names["owner"], False, False, True)]
    for engine in (w.runtime_engine, w.engine):
        with engine.connect() as c:
            assert sqlstate(c, "SET ROLE " + executor) == "42501"
            assert sqlstate(c, "SET ROLE " + service) == "42501"


# --- outcomes ----------------------------------------------------------------------------------

DENIALS = {
    "unknown-session": (lambda w, u, k: ((uuid.uuid4(), k[0][1]), "others"), "not_authenticated"),
    "wrong-token": (lambda w, u, k: ((k[0][0], k[1][1]), "others"), "not_authenticated"),
    "inactive-account": (lambda w, u, k: set_user(w, u, "is_active=false") or (k[0], "others"), "not_authenticated"),
    "deletion-requested": (lambda w, u, k: set_user(w, u, "deletion_requested_at=clock_timestamp()") or (k[0], "all"),
                           "not_authenticated"),
    "revoked-original": (lambda w, u, k: admin_sql(w, "UPDATE kalillac.account_sessions SET revoked_at="
                                                      "clock_timestamp() WHERE id=:s", s=k[0][0]) or (k[0], "others"),
                         "not_authenticated"),
    "expired-original": (lambda w, u, k: admin_sql(w, "UPDATE kalillac.account_sessions SET expires_at="
                                                      "clock_timestamp()-interval '1 second' WHERE id=:s", s=k[0][0])
                         or (k[0], "all"), "not_authenticated"),
    "before-epoch": (lambda w, u, k: set_user(w, u, "sessions_valid_after=(SELECT created_at FROM"
                                                    " kalillac.account_sessions WHERE id='" + str(k[0][0]) + "')")
                     or (k[0], "others"), "not_authenticated"),
    "quarantined": (lambda w, u, k: set_user(w, u, "quarantined_at=clock_timestamp()") or (k[0], "all"),
                    "account_unavailable"),
    "invalid-scope": (lambda w, u, k: (k[0], "every"), "security_unavailable"),
    "null-scope": (lambda w, u, k: (k[0], None), "security_unavailable"),
}


@pytest.mark.parametrize("case", list(DENIALS))
def test_pre_admission_denials_store_nothing(world, case):
    w = world
    user, keys = account(w)
    prepare, outcome = DENIALS[case]
    key, scope = prepare(w, user, keys)
    before = security_state(w)
    assert revoke(w, uuid.uuid4(), key, scope) == (outcome, None, False)
    assert security_state(w) == before
    events, admission, aggregate, _ = before
    assert events == admission == aggregate == []


def test_null_operation_id_is_refused_and_stores_nothing(world):
    w = world
    user, keys = account(w)
    before = security_state(w)
    assert revoke(w, None, keys[0], "all") == ("security_unavailable", None, False)
    assert security_state(w) == before


def test_revoke_others_keeps_exactly_the_original(world):
    w = world
    user, keys = account(w, sessions=4)
    other_user, other_keys = account(w)
    admin_sql(w, "UPDATE kalillac.account_sessions SET expires_at=clock_timestamp()-interval '1 day' WHERE id=:a",
              "UPDATE kalillac.account_sessions SET created_at=clock_timestamp()+interval '1 day' WHERE id=:b",
              a=keys[2][0], b=keys[3][0])
    operation = uuid.uuid4()
    assert revoke(w, operation, keys[0], "others") == ("revoked", None, False)
    assert revoked(w, *keys) == [False, True, True, True] and revoked(w, *other_keys) == [False, False]
    events, admission, aggregate, sessions = security_state(w)
    assert [(e[0], e[1], e[2]) for e in events] == [(operation, user, "session_revoke_others")]
    at = events[0][3]
    assert {s[1] for s in sessions if s[0] in {k[0] for k in keys[1:]}} == {at}
    assert [(a[1], a[2], a[4]) for a in admission] == [(user, [at], at)] and aggregate == [("manual", [at], at)]


def test_revoke_all_revokes_every_row_including_expired_and_future(world):
    w = world
    user, keys = account(w, sessions=3)
    admin_sql(w, "UPDATE kalillac.account_sessions SET expires_at=clock_timestamp()-interval '1 day' WHERE id=:a",
              "UPDATE kalillac.account_sessions SET created_at=clock_timestamp()+interval '1 day' WHERE id=:b",
              a=keys[1][0], b=keys[2][0])
    assert revoke(w, uuid.uuid4(), keys[0], "all") == ("revoked", None, False)
    assert revoked(w, *keys) == [True, True, True]
    assert security_state(w)[0][0][2] == "session_revoke_all"


def test_v1_01_refuses_a_session_revoked_by_the_function(world):
    w = world
    user, keys = account(w)
    assert authorize(w, user, keys[1]).id == user
    assert revoke(w, uuid.uuid4(), keys[0], "others")[0] == "revoked"
    with pytest.raises(RequestRejected) as error:
        authorize(w, user, keys[1])
    assert error.value.status == 401
    assert authorize(w, user, keys[0]).id == user


def test_successful_no_effect_others_is_charged_and_audited(world):
    w = world
    user, keys = account(w, sessions=1)
    assert revoke(w, uuid.uuid4(), keys[0], "others") == ("revoked", None, False)
    events, admission, aggregate, _ = security_state(w)
    assert len(events) == 1 and len(admission[0][2]) == 1 and len(aggregate[0][1]) == 1
    assert revoked(w, *keys) == [False]


# --- admission ---------------------------------------------------------------------------------

def _ring(*seconds_before_anchor):
    return [ANCHOR - timedelta(seconds=s) for s in seconds_before_anchor]


# (account ring, aggregate ring, expected outcome, expected retry-after): every
# stored time is relative to ANCHOR, which becomes the exact effective time.
ADMISSION = {
    "below-every-limit": (_ring(3000, 2400, 1800, 1200, 30), _ring(30), "revoked", None),
    "account-minute": (_ring(30, 20), _ring(30, 20), "too_many_operations", 30),
    "account-hour": (_ring(3000, 2400, 1800, 1200, 600, 300), _ring(3000, 2400, 1800, 1200, 600, 300),
                     "too_many_operations", 600),
    "aggregate-hour-binds": (None, _ring(*([30] * 60)), "too_many_operations", 3570),
    "aggregate-hour": (None, _ring(*(3540 - 30 * k for k in range(60))), "too_many_operations", 60),
    "minimum-one-second": (_ring(59.5, 1), _ring(59.5, 1), "too_many_operations", 1),
}


@pytest.mark.parametrize("case", list(ADMISSION))
def test_admission_limits_and_retry_after(world, case):
    w = world
    account_ring, aggregate_ring, outcome, retry = ADMISSION[case]
    user, keys = account(w)
    admin_sql(w, "UPDATE kalillac.account_sessions SET expires_at=:x WHERE user_id=:u",
              "INSERT INTO kalillac.account_security_admission_aggregate VALUES ('manual',:g,:a)",
              x=ANCHOR + timedelta(days=1), u=user, g=aggregate_ring, a=ANCHOR)
    if account_ring is not None:
        admin_sql(w, "INSERT INTO kalillac.account_security_admission VALUES (0,:u,:r,'{}',:a)",
                  u=user, r=account_ring, a=ANCHOR)
    before = security_state(w)
    assert revoke(w, uuid.uuid4(), keys[0], "others") == (outcome, retry, False)
    if outcome == "too_many_operations":
        assert security_state(w) == before and revoked(w, *keys) == [False, False]
    else:
        events, admission, aggregate, _ = security_state(w)
        assert events[0][3] == ANCHOR and admission[0][2] == [*account_ring, ANCHOR]
        assert aggregate[0][1] == [*aggregate_ring, ANCHOR]


def test_entry_creation_stops_at_2048_and_reclaims_only_stale_unlocked_entries(world):
    w = world
    admin_sql(w, "INSERT INTO kalillac.users(id,email,password_hash,is_active,email_verified_at)"
                 " SELECT gen_random_uuid(),'v102b-full-'||g||'@example.test','synthetic-hash',true,clock_timestamp()"
                 " FROM generate_series(1,2048) g",
              "INSERT INTO kalillac.account_security_admission"
              " SELECT (row_number() OVER (ORDER BY id))-1,id,ARRAY[CAST(:a AS timestamptz)],'{}',:a"
              " FROM kalillac.users WHERE email LIKE 'v102b-full-%'",
              "INSERT INTO kalillac.account_security_admission_aggregate VALUES ('manual','{}',:a)", a=ANCHOR)
    user, keys = account(w)
    admin_sql(w, "UPDATE kalillac.account_sessions SET expires_at=:x WHERE user_id=:u", x=ANCHOR + timedelta(days=1),
              u=user)
    before = security_state(w)
    # Every entry is charged inside its window: nothing can be created or reclaimed.
    assert revoke(w, uuid.uuid4(), keys[0], "others") == ("security_unavailable", None, False)
    assert security_state(w) == before
    # Two stale entries; the lower one is in use, so the other is reclaimed.
    stale = [ANCHOR - timedelta(hours=2)]
    admin_sql(w, "UPDATE kalillac.account_security_admission SET manual_charges=:r WHERE entry_slot IN (3,7)", r=stale)
    holder = holding(w, "SELECT 1 FROM kalillac.account_security_admission WHERE entry_slot=3 FOR UPDATE", 2)
    try:
        assert revoke(w, uuid.uuid4(), keys[0], "others") == ("revoked", None, False)
    finally:
        holder.join(10)
    with w.admin_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM kalillac.account_security_admission")) == 2048
        assert c.execute(text("SELECT entry_slot,manual_charges,recovery_charges FROM kalillac.account_security_admission"
                              " WHERE user_id=:u"), {"u": user}).one() == (7, [ANCHOR], [])
        assert c.scalar(text("SELECT manual_charges FROM kalillac.account_security_admission WHERE entry_slot=3")) == stale


def test_effective_time_never_moves_backward(world):
    w = world
    user, keys = account(w)
    later_user, later_keys = account(w)
    admin_sql(w, "INSERT INTO kalillac.account_security_admission_aggregate VALUES ('manual','{}',:a)",
              "UPDATE kalillac.account_sessions SET expires_at=:x WHERE user_id=:u",
              a=ANCHOR, x=ANCHOR + timedelta(days=1), u=user)
    # The clock now reads earlier than stored admission time: the stored time wins.
    assert revoke(w, uuid.uuid4(), keys[0], "all") == ("revoked", None, False)
    events, admission, aggregate, sessions = security_state(w)
    assert events[0][3] == ANCHOR and aggregate == [("manual", [ANCHOR], ANCHOR)]
    assert {s[1] for s in sessions if s[0] in {k[0] for k in keys}} == {ANCHOR}
    # A session that expires before that effective time is no longer live.
    before = security_state(w)
    assert revoke(w, uuid.uuid4(), later_keys[0], "others") == ("not_authenticated", None, False)
    assert security_state(w) == before


# --- replay --------------------------------------------------------------------------------------

def test_replay_after_others_is_not_charged_or_applied_again(world):
    w = world
    user, keys = account(w, sessions=3)
    operation = uuid.uuid4()
    assert revoke(w, operation, keys[0], "others") == ("revoked", None, False)
    after = security_state(w)
    admin_sql(w, "UPDATE kalillac.account_sessions SET revoked_at=NULL WHERE id=:s", s=keys[1][0])
    assert revoke(w, operation, keys[0], "others") == ("revoked", None, True)
    events, admission, aggregate, _ = security_state(w)
    assert (events, admission, aggregate) == after[:3] and revoked(w, *keys) == [False, False, True]


def test_replay_after_all_is_recognized_with_the_original_revoked(world):
    w = world
    user, keys = account(w)
    operation = uuid.uuid4()
    assert revoke(w, operation, keys[0], "all") == ("revoked", None, False)
    after = security_state(w)
    assert revoke(w, operation, keys[0], "all") == ("revoked", None, True)
    assert security_state(w) == after
    # A new operation from the revoked original fails the live check.
    assert revoke(w, uuid.uuid4(), keys[0], "all") == ("not_authenticated", None, False)


def test_operation_id_collision_by_scope_or_account_is_refused(world):
    w = world
    user, keys = account(w)
    other_user, other_keys = account(w)
    operation = uuid.uuid4()
    assert revoke(w, operation, keys[0], "others")[0] == "revoked"
    after = security_state(w)
    assert revoke(w, operation, keys[0], "all") == ("security_unavailable", None, False)
    assert revoke(w, operation, other_keys[0], "others") == ("security_unavailable", None, False)
    assert security_state(w) == after and revoked(w, *other_keys) == [False, False]


def test_operation_id_can_be_used_after_a_denial_that_stored_nothing(world):
    w = world
    user, keys = account(w)
    operation = uuid.uuid4()
    set_user(w, user, "quarantined_at=clock_timestamp()")
    assert revoke(w, operation, keys[0], "others")[0] == "account_unavailable"
    set_user(w, user, "quarantined_at=NULL")
    assert revoke(w, operation, keys[0], "others") == ("revoked", None, False)


# --- concurrency -------------------------------------------------------------------------------

def test_same_account_concurrent_duplicate_serializes_into_a_replay(world):
    w = world
    user, keys = account(w, sessions=3)
    operation = uuid.uuid4()
    with w.admin_engine.connect() as first:
        first.begin()
        assert revoke(w, operation, keys[0], "others", c=first)[0] == "revoked"
        second = Call(w, lambda c: revoke(w, operation, keys[0], "others", c=c)).started()
        wait_blocked(w, second.pid)
        first.commit()
    second.join(15)
    assert second.error is None and second.result == ("revoked", None, True)
    events, admission, aggregate, _ = security_state(w)
    assert len(events) == 1 and len(admission[0][2]) == 1 and len(aggregate[0][1]) == 1


def test_cross_account_collision_leaves_exactly_one_committed_success(world):
    w = world
    user, keys = account(w)
    other_user, other_keys = account(w)
    operation = uuid.uuid4()
    with w.admin_engine.connect() as first:
        first.begin()
        assert revoke(w, operation, keys[0], "others", c=first)[0] == "revoked"
        second = Call(w, lambda c: revoke(w, operation, other_keys[0], "others", c=c)).started()
        wait_blocked(w, second.pid)
        first.commit()
    second.join(15)
    assert second.error is None and second.result == ("security_unavailable", None, False)
    events, admission, aggregate, _ = security_state(w)
    assert [(e[0], e[1]) for e in events] == [(operation, user)]
    assert [a[1] for a in admission] == [user] and len(aggregate[0][1]) == 1
    assert revoked(w, *other_keys) == [False, False]


def test_legacy_session_committed_before_the_revocation_is_revoked(world):
    w = world
    user, keys = account(w)
    legacy = (uuid.uuid4(), hashlib.sha256(b"legacy-before").hexdigest())
    with w.runtime_engine.begin() as c:
        c.execute(text("INSERT INTO kalillac.account_sessions(id,user_id,token_hash,expires_at)"
                       " VALUES (:s,:u,:t,clock_timestamp()+interval '1 day')"), {"s": legacy[0], "u": user, "t": legacy[1]})
    assert revoke(w, uuid.uuid4(), keys[0], "all")[0] == "revoked"
    assert revoked(w, legacy) == [True]


def test_legacy_session_inserted_after_the_snapshot_survives(world):
    """The documented limitation: legacy issuance takes no user lock and no
    epoch, so a session it inserts after the revocation's snapshot survives,
    even with a creation time before the revocation. This authority therefore
    stays unexposed until the issuance/epoch slice removes this."""
    w = world
    user, keys = account(w)
    legacy = (uuid.uuid4(), hashlib.sha256(b"legacy-after").hexdigest())
    with w.runtime_engine.connect() as c:
        c.begin()
        started = c.scalar(text("SELECT now()"))
        assert revoke(w, uuid.uuid4(), keys[0], "all")[0] == "revoked"
        c.execute(text("INSERT INTO kalillac.account_sessions(id,user_id,token_hash,expires_at)"
                       " VALUES (:s,:u,:t,clock_timestamp()+interval '1 day')"), {"s": legacy[0], "u": user, "t": legacy[1]})
        c.commit()
    revoked_at = security_state(w)[0][0][3]
    with w.admin_engine.connect() as c:
        created = c.scalar(text("SELECT created_at FROM kalillac.account_sessions WHERE id=:s"), {"s": legacy[0]})
    assert created == started < revoked_at and revoked(w, legacy) == [False]
    assert authorize(w, user, legacy).id == user


def test_two_same_account_revocations_serialize(world):
    w = world
    user, keys = account(w)
    with w.admin_engine.connect() as first:
        first.begin()
        assert revoke(w, uuid.uuid4(), keys[0], "others", c=first)[0] == "revoked"
        second = Call(w, lambda c: revoke(w, uuid.uuid4(), keys[1], "others", c=c)).started()
        wait_blocked(w, second.pid)
        first.commit()
    second.join(15)
    assert second.error is None and second.result == ("not_authenticated", None, False)
    assert len(security_state(w)[0]) == 1 and revoked(w, *keys) == [False, True]


def test_v1_01_authorization_waiting_on_the_user_lock_sees_the_revocation(world):
    w = world
    user, keys = account(w)

    def authorize_waiting(c):
        try:
            authorize(w, user, keys[1])
        except RequestRejected as rejected:
            return rejected.status
    with w.admin_engine.connect() as first:
        first.begin()
        assert revoke(w, uuid.uuid4(), keys[0], "all", c=first)[0] == "revoked"
        reader = Call(w, lambda c: c.exec_driver_sql("SELECT 1") and authorize_waiting(c)).started()
        deadline = time.monotonic() + 15
        with w.admin_engine.connect() as watch:
            while not watch.scalar(text("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid"
                                        " WHERE NOT l.granted AND a.usename=:r"), {"r": w.names["app"]}):
                assert time.monotonic() < deadline, "V1-01 never waited on the user lock"
                watch.rollback()
                time.sleep(0.01)
        first.commit()
    reader.join(15)
    assert reader.error is None and reader.result == 401


def test_aggregate_contention_across_accounts_does_not_deadlock(world):
    w = world
    accounts = [account(w) for _ in range(8)]
    barrier = threading.Barrier(8)

    def contend(c, key):
        barrier.wait(10)
        return revoke(w, uuid.uuid4(), key, "others", c=c)
    calls = [Call(w, lambda c, key=keys[0]: contend(c, key)) for _, keys in accounts]
    for call in calls:
        call.start()
    for call in calls:
        call.join(30)
    assert [(c.error, c.result) for c in calls] == [(None, ("revoked", None, False))] * 8
    events, admission, aggregate, _ = security_state(w)
    assert len(events) == 8 and len(admission) == 8 and len(aggregate[0][1]) == 8


def test_injected_opposite_order_deadlock_rolls_back_its_victim(world):
    w = world
    user, keys = account(w)
    # PostgreSQL checks once, when a waiter's deadlock_timeout expires; the
    # revocation's must expire after the cycle forms, and before the other's.
    with w.admin_engine.connect() as legacy:
        legacy.begin()
        legacy.exec_driver_sql("SET LOCAL deadlock_timeout = '30s'")
        legacy.execute(text("SELECT 1 FROM kalillac.account_sessions WHERE id=:s FOR UPDATE"), {"s": keys[1][0]})
        victim = Call(w, lambda c: revoke(w, uuid.uuid4(), keys[0], "others", c=c,
                                          settings=("SET LOCAL deadlock_timeout = '2s'",))).started()
        wait_blocked(w, victim.pid)
        # The opposite order: session row first, then the user row.
        legacy.execute(text("SELECT 1 FROM kalillac.users WHERE id=:u FOR UPDATE"), {"u": user})
        victim.join(15)
        legacy.rollback()
    assert isinstance(victim.error, DBAPIError) and victim.error.orig.sqlstate == "40P01"
    assert security_state(w)[:3] == ([], [], []) and revoked(w, *keys) == [False, False]
    assert revoke(w, uuid.uuid4(), keys[0], "others") == ("revoked", None, False)


# --- failure and rollback ----------------------------------------------------------------------

def _hold_aggregate(w):
    admin_sql(w, "INSERT INTO kalillac.account_security_admission_aggregate VALUES ('manual','{}',clock_timestamp())")
    return holding(w, "SELECT 1 FROM kalillac.account_security_admission_aggregate FOR UPDATE", 3)


FAILURES = {
    "pg-cancel-backend": ((), "57014"),
    "lock-timeout": (("SET LOCAL lock_timeout = '200ms'",), "55P03"),
    "statement-timeout": (("SET LOCAL statement_timeout = '200ms'",), "57014"),
}


@pytest.mark.parametrize("case", list(FAILURES))
def test_interrupted_calls_roll_back_everything(world, case):
    w = world
    settings, state = FAILURES[case]
    user, keys = account(w)
    blocker = _hold_aggregate(w)
    before = security_state(w)
    call = Call(w, lambda c: revoke(w, uuid.uuid4(), keys[0], "all", c=c, settings=settings)).started()
    if case == "pg-cancel-backend":
        wait_blocked(w, call.pid)
        admin_sql(w, "SELECT pg_cancel_backend(:p)", p=call.pid)
    call.join(15)
    blocker.join(15)
    assert isinstance(call.error, DBAPIError) and call.error.orig.sqlstate == state
    assert security_state(w) == before and revoked(w, *keys) == [False, False]
    assert revoke(w, uuid.uuid4(), keys[0], "all") == ("revoked", None, False)


def test_failure_after_the_charge_rolls_back_everything(world):
    w = world
    user, keys = account(w)
    admin_sql(w, "CREATE FUNCTION public.v1_fault() RETURNS trigger LANGUAGE plpgsql AS"
                 " $f$BEGIN RAISE EXCEPTION 'injected fault after the charge'; END$f$",
              "CREATE TRIGGER v1_fault BEFORE INSERT ON kalillac.account_security_events"
              " FOR EACH ROW EXECUTE FUNCTION public.v1_fault()")
    before = security_state(w)
    with pytest.raises(DBAPIError) as error:
        revoke(w, uuid.uuid4(), keys[0], "all")
    assert error.value.orig.sqlstate == "P0001"
    assert security_state(w) == before and revoked(w, *keys) == [False, False]
    admin_sql(w, "DROP TRIGGER v1_fault ON kalillac.account_security_events", "DROP FUNCTION public.v1_fault()")
    assert revoke(w, uuid.uuid4(), keys[0], "all") == ("revoked", None, False)


# --- migration ---------------------------------------------------------------------------------

PREREQUISITES = {
    "service-missing": (["ALTER ROLE {service} RENAME TO {service}_x"], ["ALTER ROLE {service}_x RENAME TO {service}"]),
    "executor-missing": (["ALTER ROLE {executor} RENAME TO {executor}_x"],
                         ["ALTER ROLE {executor}_x RENAME TO {executor}"]),
    "service-login": (["ALTER ROLE {service} LOGIN"], ["ALTER ROLE {service} NOLOGIN"]),
    "executor-inherit": (["ALTER ROLE {executor} INHERIT"], ["ALTER ROLE {executor} NOINHERIT"]),
    "executor-bypassrls": (["ALTER ROLE {executor} BYPASSRLS"], ["ALTER ROLE {executor} NOBYPASSRLS"]),
    "edge-missing": (["REVOKE {executor} FROM {owner}"],
                     ["GRANT {executor} TO {owner} WITH INHERIT FALSE, SET TRUE"]),
    "edge-inherit": (["GRANT {executor} TO {owner} WITH INHERIT TRUE"],
                     ["GRANT {executor} TO {owner} WITH INHERIT FALSE"]),
    "edge-admin": (["GRANT {executor} TO {owner} WITH ADMIN OPTION"],
                   ["REVOKE ADMIN OPTION FOR {executor} FROM {owner}"]),
    "edge-without-set": (["GRANT {executor} TO {owner} WITH SET FALSE"], ["GRANT {executor} TO {owner} WITH SET TRUE"]),
    "extra-service-edge": (["GRANT {service} TO {hop1}"], ["REVOKE {service} FROM {hop1}"]),
    "service-member-of-executor": (["GRANT {executor} TO {service}"], ["REVOKE {executor} FROM {service}"]),
    "runtime-intermediary": (["GRANT {executor} TO {hop1}", "GRANT {hop1} TO {app} WITH INHERIT TRUE"],
                             ["REVOKE {hop1} FROM {app}", "REVOKE {executor} FROM {hop1}"]),
    "service-owns-object": (["CREATE TABLE public.v1_owned (x int)", "ALTER TABLE public.v1_owned OWNER TO {service}"],
                            ["DROP TABLE public.v1_owned"]),
    "executor-holds-grant": (["GRANT SELECT ON kalillac.users TO {executor}"],
                             ["REVOKE SELECT ON kalillac.users FROM {executor}"]),
    "service-can-connect": (["GRANT CONNECT ON DATABASE {database} TO {service}"],
                            ["REVOKE CONNECT ON DATABASE {database} FROM {service}"]),
    "executor-default-acl": (["ALTER DEFAULT PRIVILEGES FOR ROLE {executor} GRANT SELECT ON TABLES TO PUBLIC"],
                             ["ALTER DEFAULT PRIVILEGES FOR ROLE {executor} REVOKE SELECT ON TABLES FROM PUBLIC"]),
    "default-acl-to-service": (["ALTER DEFAULT PRIVILEGES FOR ROLE {owner} GRANT SELECT ON TABLES TO {service}"],
                               ["ALTER DEFAULT PRIVILEGES FOR ROLE {owner} REVOKE SELECT ON TABLES FROM {service}"]),
}


@pytest.mark.parametrize("case", list(PREREQUISITES))
def test_migration_refuses_invalid_prerequisites_atomically(world, roles, case):
    w = world
    tamper, undo = PREREQUISITES[case]
    names = {"hop1": roles("hop1"), "database": w.migration.url.database}
    migrate(w, PREVIOUS, down=True)
    run(w, tamper, **names)
    before = (catalog_state(w), authority_state(w))
    with pytest.raises(DBAPIError) as error:
        migrate(w, REVISION)
    assert error.value.orig.diag.message_primary == "V1_REVOCATION_PREREQUISITE_INVALID"
    assert (catalog_state(w), authority_state(w)) == before and before[0][0] == [(PREVIOUS,)]
    run(w, undo, **names)
    migrate(w, REVISION)
    assert classify(w.owner_factory) == v1_schema.PREPARED


@pytest.mark.parametrize("setting,value,error", [
    (migration_roles.V1_SECURITY_SERVICE_ROLE_ENV, None, migration_roles.RoleNameRequired),
    (migration_roles.V1_SECURITY_EXECUTOR_ROLE_ENV, "", migration_roles.RoleNameRequired),
    (migration_roles.V1_SECURITY_SERVICE_ROLE_ENV, "Bad Name", migration_roles.InvalidRoleName),
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
    assert error.value.orig.diag.message_primary == "V1_REVOCATION_POSTCONDITION_INVALID"
    assert (catalog_state(w), authority_state(w)) == before
    function, _, _, create, _ = authority_state(w)
    assert function is None and create is False
    run(w, ["ALTER DEFAULT PRIVILEGES FOR ROLE {owner} REVOKE EXECUTE ON FUNCTIONS FROM {app}"])
    migrate(w, REVISION)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def test_successful_migration_leaves_exactly_the_declared_authority(world):
    w = world
    service, executor, owner = w.names["service"], w.names["executor"], w.names["owner"]
    function, dependencies, schema_acl, create, edges = authority_state(w)
    assert function == SIGNATURE and create is False
    assert f"{executor}=U/{owner}" in schema_acl and f"{service}=U/{owner}" in schema_acl
    assert f"{executor}=UC/" not in schema_acl and f"{service}=UC/" not in schema_acl
    assert {(d[0], d[1], d[4]) for d in dependencies} == {
        (executor, "pg_proc", "o"), (executor, "pg_class", "a"), (executor, "pg_namespace", "a"),
        (service, "pg_namespace", "a"), (service, "pg_proc", "a")}
    assert [e for e in edges if executor in e[:2] or service in e[:2]] == [(executor, owner, False, True, False)]


def test_pristine_downgrade_and_reupgrade(world):
    w = world
    migrate(w, PREVIOUS, down=True)
    function, dependencies, schema_acl, create, _ = authority_state(w)
    assert function is None and dependencies == [] and create is False
    assert w.names["executor"] not in schema_acl and w.names["service"] not in schema_acl
    assert classify(w.owner_factory) == v1_schema.PREPARED
    migrate(w, REVISION)
    assert classify(w.owner_factory) == v1_schema.PREPARED


@pytest.mark.parametrize("state", ["revocation-event", "admission-entry"])
def test_non_pristine_downgrade_is_refused_atomically(world, state):
    w = world
    user, keys = account(w)
    if state == "revocation-event":
        assert revoke(w, uuid.uuid4(), keys[0], "others")[0] == "revoked"
    else:
        admin_sql(w, "INSERT INTO kalillac.account_security_admission VALUES (0,:u,'{}','{}',clock_timestamp())", u=user)
    before = (security_state(w), authority_state(w), catalog_state(w))
    with pytest.raises(RuntimeError, match="^V1_REVOCATION_DOWNGRADE_REFUSED$"):
        migrate(w, PREVIOUS, down=True)
    assert (security_state(w), authority_state(w), catalog_state(w)) == before
    assert before[2][0] == [(REVISION,)]


# --- catalog qualification ---------------------------------------------------------------------

def test_canonical_0013_catalog_classifies_prepared_and_startup_still_refuses(world):
    w = world
    assert classify(w.owner_factory) == v1_schema.PREPARED
    with pytest.raises(v1_schema.V1SchemaRefused, match="authority_state_unreadable"):
        classify(w.runtime_factory)
    with pytest.raises(V1SecurityConfigError, match=r"\(prepared_inactive\)"):
        load_v1_startup(lambda: w.owner_factory, ENV)


def test_installed_definition_matches_the_classifier_constants(world):
    w = world
    with w.admin_engine.connect() as c:
        row = c.execute(text("""SELECT pg_get_function_identity_arguments(p.oid),pg_get_function_result(p.oid),
            l.lanname,p.provolatile::text,p.prosecdef,p.proleakproof,p.proisstrict,p.proparallel::text,p.prokind::text,
            p.proconfig,p.procost::float8,p.prorows::float8,encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')
            FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang WHERE p.oid=to_regprocedure(:f)"""),
            {"f": SIGNATURE}).one()
    assert tuple(row[:-1]) == v1_schema.REVOCATION_DEFINITION and row[-1] == v1_schema.REVOCATION_SOURCE_SHA256


FUNCTION_HEAD = ("CREATE OR REPLACE FUNCTION kalillac.v1_revoke_sessions(p_operation_id uuid, p_session_id uuid,"
                 " p_token_hash text, p_scope text) RETURNS TABLE(outcome text, retry_after_seconds integer,"
                 " replayed boolean) LANGUAGE plpgsql VOLATILE SECURITY DEFINER PARALLEL UNSAFE"
                 " SET search_path = pg_catalog, pg_temp AS ")
ON_FUNCTION = " ON FUNCTION kalillac.v1_revoke_sessions(uuid, uuid, text, text) "
ALTER_FUNCTION = "ALTER FUNCTION kalillac.v1_revoke_sessions(uuid, uuid, text, text) "

# Every partial or modified 0013 catalog is refused.
TAMPERS = {
    "body": [FUNCTION_HEAD + "$b$BEGIN outcome := 'revoked'; RETURN NEXT; END$b$"],
    "owner": [ALTER_FUNCTION + "OWNER TO {owner}"],
    "acl-runtime": ["GRANT EXECUTE" + ON_FUNCTION + "TO {app}"],
    "acl-public": ["GRANT EXECUTE" + ON_FUNCTION + "TO PUBLIC"],
    "acl-service-missing": ["REVOKE EXECUTE" + ON_FUNCTION + "FROM {service}"],
    "acl-grant-option": ["SET LOCAL ROLE {executor}", "GRANT EXECUTE" + ON_FUNCTION + "TO {service} WITH GRANT OPTION"],
    "search-path": [ALTER_FUNCTION + "SET search_path = public, pg_temp"],
    "security-invoker": [ALTER_FUNCTION + "SECURITY INVOKER"],
    "volatility": [ALTER_FUNCTION + "STABLE"],
    "parallel": [ALTER_FUNCTION + "PARALLEL SAFE"],
    "leakproof": [ALTER_FUNCTION + "LEAKPROOF"],
    "strict": [ALTER_FUNCTION + "STRICT"],
    "overload": ["CREATE FUNCTION kalillac.v1_revoke_sessions(p uuid) RETURNS void LANGUAGE sql AS 'SELECT'"],
    "function-dropped-grants-kept": ["DROP FUNCTION kalillac.v1_revoke_sessions(uuid, uuid, text, text)"],
    "executor-extra-table-right": ["GRANT DELETE ON kalillac.account_security_events TO {executor}"],
    "executor-missing-table-right": ["REVOKE INSERT ON kalillac.account_security_events FROM {executor}"],
    "executor-extra-column-right": ["GRANT UPDATE (is_active) ON kalillac.users TO {executor}"],
    "executor-users-table-right": ["GRANT UPDATE ON kalillac.users TO {executor}"],
    "executor-grant-option": ["GRANT SELECT ON kalillac.account_security_events TO {executor} WITH GRANT OPTION"],
    "executor-schema-create": ["GRANT CREATE ON SCHEMA kalillac TO {executor}"],
    "executor-other-table": ["GRANT SELECT ON kalillac.account_recovery_tokens TO {executor}"],
    "executor-owns-extra": ["CREATE TABLE public.v1_owned (x int)", "ALTER TABLE public.v1_owned OWNER TO {executor}"],
    "service-table-right": ["GRANT SELECT ON kalillac.account_security_events TO {service}"],
    "service-schema-create": ["GRANT CREATE ON SCHEMA kalillac TO {service}"],
    "service-can-connect": ["GRANT CONNECT ON DATABASE {database} TO {service}"],
    "service-login": ["ALTER ROLE {service} LOGIN"],
    "executor-inherit": ["ALTER ROLE {executor} INHERIT"],
    "edge-inherit": ["GRANT {executor} TO {owner} WITH INHERIT TRUE"],
    "edge-admin": ["GRANT {executor} TO {owner} WITH ADMIN OPTION"],
    "extra-service-edge": ["GRANT {hop1} TO {service}"],
    "runtime-intermediary": ["GRANT {executor} TO {hop1}", "GRANT {hop1} TO {app} WITH INHERIT TRUE"],
    "executor-default-acl": ["ALTER DEFAULT PRIVILEGES FOR ROLE {executor} GRANT SELECT ON TABLES TO PUBLIC"],
    "service-owns-object": ["CREATE TABLE public.v1_owned (x int)", "ALTER TABLE public.v1_owned OWNER TO {service}"],
}


@pytest.mark.parametrize("case", list(TAMPERS))
def test_classifier_refuses_every_modified_0013_catalog(world, roles, case):
    w = world
    assert classify(w.owner_factory) == v1_schema.PREPARED
    run(w, TAMPERS[case], hop1=roles("hop1"), database=w.migration.url.database)
    refused(w)


def test_classifier_refuses_without_role_settings(world, monkeypatch):
    w = world
    monkeypatch.delenv(migration_roles.V1_SECURITY_EXECUTOR_ROLE_ENV)
    with pytest.raises(v1_schema.V1SchemaRefused, match="authority_roles_unconfigured"):
        classify(w.owner_factory)
