"""0023, deletion requests by the operator role P and by the customer through S, on real disposable
PostgreSQL 16.

The world stands on 0023 (stepped down from the head, 0024, which tests its own catalog). P and S stay NOLOGIN: each is exercised through SET LOCAL
ROLE from the disposable superuser, which also seeds fixtures, holds locks and tampers with the catalog.
Every password hash is synthetic. Nothing here erases an account: a request marks it, ends every session
and moves the epoch, and that is the whole lifecycle of this checkpoint.
"""
from __future__ import annotations

import secrets
import threading
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import kalillac_accounts.v1_schema as v1_schema
from test_v1_issuance_postgres import account, authorized, epoch, issue, key, sessions
from test_v1_password_reset_postgres import new_hash, reset, token
from test_v1_revocation_postgres import admin_sql, revoke, security_state, set_user
from test_v1_schema_postgres import classify, migrate, refused, sqlstate, version
from test_v1_session_end_postgres import head_world

HEAD = "0023_v1_deletion_request"
PREVIOUS = "0022_v1_quarantine"
REQUEST = "kalillac.v1_request_deletion(uuid,uuid)"
OWN = "kalillac.v1_request_own_deletion(uuid,uuid,text,text)"
DENIED = ("not_authenticated", None, False)
UNAVAILABLE = ("security_unavailable", None, False)


@pytest.fixture
def world(monkeypatch):
    with head_world(monkeypatch) as w:
        # 0023's own catalog exactly: later revisions (0024) test theirs.
        migrate(w, HEAD, down=True)
        yield w
        assert w.admin_engine.pool.checkedout() == 0 and w.runtime_engine.pool.checkedout() == 0


def operate(w, operation, user, role="operator", function="v1_request_deletion", settings=()):
    """The operator's request, as P (or another role, to show it is refused)."""
    with w.admin_engine.begin() as c:
        for setting in settings:
            c.exec_driver_sql(setting)
        c.exec_driver_sql("SET LOCAL ROLE " + w.names[role])
        return tuple(c.execute(text(f"SELECT * FROM kalillac.{function}(:o,:u)"), {"o": operation, "u": user}).one())


def own(w, operation, k, verified, role="service"):
    """The customer's own request, as S (or another role, to show it is refused)."""
    with w.admin_engine.begin() as c:
        c.exec_driver_sql("SET LOCAL ROLE " + w.names[role])
        return tuple(c.execute(text("SELECT * FROM kalillac.v1_request_own_deletion(:o,:s,:t,:v)"),
                               {"o": operation, "s": k[0], "t": k[1], "v": verified}).one())


def marked(w, user):
    with w.admin_engine.connect() as c:
        return tuple(c.execute(text("SELECT deletion_requested_at, quarantined_at, sessions_valid_after"
                                    " FROM kalillac.users WHERE id=:u"), {"u": user}).one())


def signed_in(w, count=2):
    user, stored = account(w)
    keys = [key() for _ in range(count)]
    for k in keys:
        assert issue(w, *k, user, stored)[0] == "issued"
    return user, stored, keys


def events_of(w, user):
    return [(row[0], row[2], row[3]) for row in security_state(w)[0] if row[1] == user]


def together(*calls):
    """Run the calls at the same moment on separate connections; their results in call order."""
    results, start = [None] * len(calls), threading.Barrier(len(calls))

    def run(index, call):
        start.wait(10)
        results[index] = call()
    threads = [threading.Thread(target=run, args=(i, call)) for i, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results


def acl(w, signature):
    with w.admin_engine.connect() as c:
        return sorted(c.scalar(text("SELECT array_agg(pg_get_userbyid(x.grantee)) FROM pg_proc p,"
                                    " aclexplode(p.proacl) x WHERE p.oid = to_regprocedure(:f)"), {"f": signature}))


# --- the catalog ---------------------------------------------------------------------------------------

def test_the_head_classifies_with_exactly_the_two_deletion_functions(world):
    w = world
    assert version(w) == HEAD and classify(w.owner_factory) == v1_schema.PREPARED
    assert acl(w, REQUEST) == sorted([w.names["executor"], w.names["operator"]])
    assert acl(w, OWN) == sorted([w.names["executor"], w.names["service"]])
    with w.admin_engine.connect() as c:
        for signature in (REQUEST, OWN):
            assert c.scalar(text("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid = to_regprocedure(:f)"),
                            {"f": signature}) == w.names["executor"]
        assert c.scalar(text("SELECT has_column_privilege(:e, 'kalillac.users', 'deletion_requested_at', 'UPDATE')"),
                        {"e": w.names["executor"]})
        assert c.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                             " WHERE conname = 'ck_account_security_events_type'")) == v1_schema._among(
            "event_type", v1_schema.EVENT_TYPES_V4)


# --- the operator's request ----------------------------------------------------------------------------

def test_an_operator_request_marks_the_account_ends_every_session_and_blocks_sign_in(world):
    w = world
    user, stored, keys = signed_in(w, 3)
    stranger, stranger_stored, (theirs,) = signed_in(w, 1)
    before = epoch(w, user)
    operation = uuid.uuid4()
    assert operate(w, operation, user) == ("requested", False)
    requested_at, quarantined_at, after = marked(w, user)
    assert requested_at is not None and after == requested_at and quarantined_at is None
    assert before is None or after > before
    assert {row[4] for row in sessions(w, user)} == {requested_at}               # Every session, one time.
    assert all(authorized(w, user, k) == 401 for k in keys)
    assert issue(w, *key(), user, stored)[0] == "not_authenticated"              # And no new one.
    assert events_of(w, user) == [(operation, "account_deletion_request", requested_at)]
    assert not [row for row in security_state(w)[1] if row[1] == user]           # The operator is not charged.
    # Another account is untouched.
    assert marked(w, stranger)[0] is None and authorized(w, stranger, theirs) is True
    # A replay reports and changes nothing; a second request is refused; the first time is kept.
    state = security_state(w)
    assert operate(w, operation, user) == ("requested", True)
    assert operate(w, uuid.uuid4(), user) == ("already_requested", False)
    assert security_state(w) == state and marked(w, user)[0] == requested_at
    # The operation id belongs to this account only; unknown accounts and malformed calls change nothing.
    assert operate(w, operation, stranger) == ("security_unavailable", False)
    assert operate(w, uuid.uuid4(), uuid.uuid4()) == ("not_found", False)
    assert operate(w, None, user) == ("security_unavailable", False)
    assert operate(w, uuid.uuid4(), None) == ("security_unavailable", False)
    assert security_state(w) == state and marked(w, stranger)[0] is None


def test_every_other_authority_refuses_a_deletion_requested_account(world):
    w = world
    user, stored, (mine, other) = signed_in(w)
    digest = token(w, user)
    assert operate(w, uuid.uuid4(), user)[0] == "requested"
    state = security_state(w)
    assert issue(w, *key(), user, stored)[0] == "not_authenticated"
    assert revoke(w, uuid.uuid4(), mine, "all")[0] == "not_authenticated"
    with w.admin_engine.begin() as c:
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["service"])
        changed = tuple(c.execute(text("SELECT * FROM kalillac.v1_change_password(:o,:s,:t,:v,:h)"),
                                  {"o": uuid.uuid4(), "s": mine[0], "t": mine[1], "v": stored,
                                   "h": new_hash()}).one())
    assert changed == DENIED
    assert reset(w, uuid.uuid4(), digest, new_hash())[0] == "invalid_token"      # Recovery cannot reopen it.
    assert own(w, uuid.uuid4(), other, stored) == DENIED
    assert security_state(w) == state


def test_quarantine_and_a_deletion_request_compose_and_reinstatement_does_not_reopen_sign_in(world):
    w = world
    user, stored, _keys = signed_in(w, 1)
    assert operate(w, uuid.uuid4(), user, function="v1_quarantine") == ("quarantined", False)
    quarantine_epoch = marked(w, user)[2]
    assert operate(w, uuid.uuid4(), user) == ("requested", False)
    requested_at, quarantined_at, after = marked(w, user)
    assert quarantined_at == quarantine_epoch and after == requested_at > quarantine_epoch
    assert operate(w, uuid.uuid4(), user, function="v1_reinstate") == ("reinstated", False)
    assert marked(w, user)[:2] == (requested_at, None)
    assert issue(w, *key(), user, stored)[0] == "not_authenticated"             # Still deletion-requested.
    # And a deletion-requested account can still be quarantined (the epoch moves forward again).
    assert operate(w, uuid.uuid4(), user, function="v1_quarantine") == ("quarantined", False)
    assert marked(w, user)[2] > after
    assert [event for _, event, _ in events_of(w, user)].count("account_deletion_request") == 1


def test_a_future_dated_session_is_revoked_and_a_clock_behind_the_epoch_still_moves_it_forward(world):
    w = world
    user, _stored, (k,) = signed_in(w, 1)
    ahead = key()
    admin_sql(w, "INSERT INTO kalillac.account_sessions(id,user_id,token_hash,created_at,expires_at)"
                 " VALUES (:s,:u,:t,clock_timestamp()+interval '1 hour',clock_timestamp()+interval '2 days')",
              s=ahead[0], u=user, t=ahead[1])
    set_user(w, user, "sessions_valid_after = clock_timestamp() + interval '1 day'")
    future_epoch = epoch(w, user)
    assert operate(w, uuid.uuid4(), user)[0] == "requested"
    requested_at, _, after = marked(w, user)
    assert after > future_epoch and requested_at == after                       # Never backward.
    assert all(row[4] is not None for row in sessions(w, user))


# --- the customer's own request ------------------------------------------------------------------------

def test_a_customers_own_request_ends_every_session_including_this_one_and_records_once(world):
    w = world
    user, stored, (mine, other, third) = signed_in(w, 3)
    stranger, _, (theirs,) = signed_in(w, 1)
    operation = uuid.uuid4()
    assert own(w, operation, mine, stored) == ("requested", None, False)
    requested_at, _, after = marked(w, user)
    assert requested_at is not None and after == requested_at
    assert {row[4] for row in sessions(w, user)} == {requested_at}
    assert all(authorized(w, user, k) == 401 for k in (mine, other, third))
    assert issue(w, *key(), user, stored)[0] == "not_authenticated"
    assert events_of(w, user) == [(operation, "account_deletion_request", requested_at)]
    admission = [row for row in security_state(w)[1] if row[1] == user]
    assert admission[0][2] == [requested_at] and admission[0][3] == []          # The manual ring, once.
    assert authorized(w, stranger, theirs) is True and marked(w, stranger)[0] is None
    # A replay through the same (now revoked) session reports and changes nothing.
    state = security_state(w)
    assert own(w, operation, mine, stored) == ("requested", None, True)
    assert own(w, uuid.uuid4(), mine, stored) == DENIED                         # Nothing proves it any more.
    assert operate(w, uuid.uuid4(), user) == ("already_requested", False)
    assert own(w, operation, theirs, _stored_of(w, stranger)) == UNAVAILABLE    # Another account's operation.
    assert security_state(w) == state and marked(w, user)[0] == requested_at


def _stored_of(w, user):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT password_hash FROM kalillac.users WHERE id=:u"), {"u": user})


def test_a_stale_or_wrong_proof_is_denied_uniformly_and_changes_nothing(world):
    w = world
    user, stored, (mine, other) = signed_in(w)
    revoked_key, expired = key(), key()
    assert issue(w, *revoked_key, user, stored)[0] == "issued"
    assert issue(w, *expired, user, stored)[0] == "issued"
    admin_sql(w, "UPDATE kalillac.account_sessions SET revoked_at=clock_timestamp() WHERE id=:r",
              "UPDATE kalillac.account_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE id=:x",
              r=revoked_key[0], x=expired[0])
    state, before = security_state(w), marked(w, user)
    for k, verified in ((mine, stored + "x"),                                   # Not the stored hash.
                        (revoked_key, stored), (expired, stored),
                        ((mine[0], secrets.token_hex(32)), stored),             # Right id, wrong token.
                        ((uuid.uuid4(), mine[1]), stored)):                     # Unknown session.
        assert own(w, uuid.uuid4(), k, verified) == DENIED
    set_user(w, user, "sessions_valid_after = clock_timestamp()")               # Created before the epoch.
    assert own(w, uuid.uuid4(), other, stored) == DENIED
    set_user(w, user, "sessions_valid_after = NULL")
    for assignment in ("password_reset_required = true", "is_active = false"):
        set_user(w, user, assignment)
        assert own(w, uuid.uuid4(), mine, stored) == DENIED
        set_user(w, user, "password_reset_required = false, is_active = true")
    set_user(w, user, "quarantined_at = clock_timestamp()")
    assert own(w, uuid.uuid4(), mine, stored) == ("account_unavailable", None, False)
    set_user(w, user, "quarantined_at = NULL")
    assert security_state(w) == state and marked(w, user)[0] is None
    assert authorized(w, user, mine) is True and before[0] is None


@pytest.mark.parametrize("field, value", [
    ("operation", None), ("session", None), ("token", None), ("token", "A" * 64), ("token", "a" * 63),
    ("verified", None), ("verified", "h" * 256),
])
def test_malformed_own_requests_are_refused_and_store_nothing(world, field, value):
    w = world
    user, stored, (mine,) = signed_in(w, 1)
    arguments = {"operation": uuid.uuid4(), "session": mine[0], "token": mine[1], "verified": stored}
    arguments[field] = value
    state = security_state(w)
    assert own(w, arguments["operation"], (arguments["session"], arguments["token"]), arguments["verified"]) == UNAVAILABLE
    assert security_state(w) == state and marked(w, user)[0] is None


def test_own_requests_share_the_customer_admission_budget_and_a_refusal_stores_nothing(world):
    w = world
    user, stored, (mine,) = signed_in(w, 1)
    assert revoke(w, uuid.uuid4(), mine, "others")[0] == "revoked"
    assert revoke(w, uuid.uuid4(), mine, "others")[0] == "revoked"
    before = marked(w, user)
    outcome, wait, replayed = own(w, uuid.uuid4(), mine, stored)
    assert outcome == "too_many_operations" and 1 <= wait <= 60 and replayed is False
    assert marked(w, user) == before and authorized(w, user, mine) is True
    assert not [event for _, event, _ in events_of(w, user) if event == "account_deletion_request"]


# --- who may call, and who may write the marker -----------------------------------------------------------

def test_only_p_may_request_an_accounts_deletion_and_only_s_a_customers_own(world):
    w = world
    user, stored, (mine,) = signed_in(w, 1)
    with pytest.raises(DBAPIError) as error:
        operate(w, uuid.uuid4(), user, role="service")
    assert error.value.orig.sqlstate == "42501"
    with pytest.raises(DBAPIError) as error:
        own(w, uuid.uuid4(), mine, stored, role="operator")
    assert error.value.orig.sqlstate == "42501"
    for engine in (w.runtime_engine, w.engine):                                 # R, and A (PUBLIC's rights too).
        with engine.connect() as c:
            assert sqlstate(c, "SELECT * FROM kalillac.v1_request_deletion(:o,:u)",
                            {"o": uuid.uuid4(), "u": user}) == "42501"
            assert sqlstate(c, "SELECT * FROM kalillac.v1_request_own_deletion(:o,:s,:t,:v)",
                            {"o": uuid.uuid4(), "s": mine[0], "t": mine[1], "v": stored}) == "42501"
    assert marked(w, user)[0] is None and authorized(w, user, mine) is True


def test_no_role_can_write_the_marker_or_the_event_directly(world):
    w = world
    user, _stored, _keys = signed_in(w, 1)
    with w.runtime_engine.connect() as c:                                       # R: no column privilege.
        assert sqlstate(c, "UPDATE kalillac.users SET deletion_requested_at = clock_timestamp() WHERE id = :u",
                        {"u": user}) == "42501"
        assert sqlstate(c, "INSERT INTO kalillac.account_security_events(operation_id,user_id,event_type,occurred_at)"
                           " VALUES (:o,:u,'account_deletion_request',now())", {"o": uuid.uuid4(), "u": user}) == "42501"
    for role in ("operator", "service"):                                        # P and S: functions only.
        with w.admin_engine.connect() as c:
            c.exec_driver_sql("SET ROLE " + w.names[role])
            assert sqlstate(c, "UPDATE kalillac.users SET deletion_requested_at = clock_timestamp() WHERE id = :u",
                            {"u": user}) == "42501"
            c.exec_driver_sql("RESET ROLE")
            c.rollback()
    assert marked(w, user)[0] is None


# --- repeated and concurrent requests --------------------------------------------------------------------

def test_concurrent_operator_requests_mark_and_record_exactly_once(world):
    w = world
    user, _stored, _keys = signed_in(w)
    results = together(*(lambda: operate(w, uuid.uuid4(), user) for _ in range(4)))
    assert sorted(results) == [("already_requested", False)] * 3 + [("requested", False)]
    assert [event for _, event, _ in events_of(w, user)] == ["account_deletion_request"]
    assert all(row[4] is not None for row in sessions(w, user))


def test_concurrent_retries_of_one_operation_apply_it_once(world):
    w = world
    user, _stored, _keys = signed_in(w)
    operation = uuid.uuid4()
    results = together(*(lambda: operate(w, operation, user) for _ in range(4)))
    assert sorted(results) == [("requested", False)] + [("requested", True)] * 3
    assert len(events_of(w, user)) == 1


def test_a_customer_and_an_operator_racing_leave_one_request(world):
    w = world
    user, stored, (mine, other) = signed_in(w)
    customer, operator = together(lambda: own(w, uuid.uuid4(), mine, stored),
                                  lambda: operate(w, uuid.uuid4(), user))
    assert (customer, operator) in ((("requested", None, False), ("already_requested", False)),
                                    (DENIED, ("requested", False)))
    assert [event for _, event, _ in events_of(w, user)] == ["account_deletion_request"]
    assert marked(w, user)[0] is not None and all(row[4] is not None for row in sessions(w, user))


def test_two_sessions_of_one_customer_racing_leave_one_request(world):
    w = world
    user, stored, (mine, other) = signed_in(w)
    results = together(lambda: own(w, uuid.uuid4(), mine, stored), lambda: own(w, uuid.uuid4(), other, stored))
    assert sorted(results) == sorted([("requested", None, False), DENIED])
    assert len(events_of(w, user)) == 1


def test_a_sign_in_racing_a_request_never_leaves_a_live_session(world):
    w = world
    for _ in range(3):
        user, stored, _keys = signed_in(w, 1)
        fresh = key()
        signed, requested = together(lambda: issue(w, *fresh, user, stored),
                                     lambda: operate(w, uuid.uuid4(), user))
        assert requested == ("requested", False) and signed[0] in ("issued", "not_authenticated")
        assert authorized(w, user, fresh) != True                               # noqa: E712
        assert all(row[4] is not None for row in sessions(w, user))


# --- an operation that fails partway ---------------------------------------------------------------------

def test_an_operation_id_taken_concurrently_rolls_back_every_effect(world):
    """The request locks the account, marks it, moves the epoch and revokes the sessions, and only then
    meets a unique violation on its event (another account's uncommitted event holds the id): every
    effect inside the request is undone and it answers security_unavailable."""
    w = world
    user, _stored, keys = signed_in(w)
    stranger, _, _ = signed_in(w, 1)
    operation, before = uuid.uuid4(), marked(w, user)
    holder = w.admin_engine.connect()
    try:
        holder.begin()
        holder.execute(text("INSERT INTO kalillac.account_security_events(operation_id,user_id,event_type,occurred_at)"
                            " VALUES (:o,:u,'session_revoke_all',clock_timestamp())"), {"o": operation, "u": stranger})
        result = []
        caller = threading.Thread(target=lambda: result.append(operate(w, operation, user)))
        caller.start()
        # Wait until the request has applied its effects and is blocked inserting its event.
        deadline, waiting = time.monotonic() + 20, 0
        with w.admin_engine.connect() as watcher:
            while not waiting and time.monotonic() < deadline:
                waiting = watcher.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                                              " AND query LIKE '%v1_request_deletion%' AND pid <> pg_backend_pid()"))
                watcher.rollback()
                time.sleep(0.05)
        holder.commit()
        caller.join(30)
    finally:
        holder.close()
    assert waiting == 1                                     # The partway path was really taken.
    assert result == [("security_unavailable", False)]
    assert marked(w, user) == before and all(row[4] is None for row in sessions(w, user))
    assert all(authorized(w, user, k) is True for k in keys) and events_of(w, user) == []


def test_a_caller_transaction_that_fails_after_the_request_leaves_nothing(world):
    w = world
    user, stored, (mine,) = signed_in(w, 1)
    before = security_state(w)
    calls = (("operator", "SELECT * FROM kalillac.v1_request_deletion(:o,:u)", {"u": user}),
             ("service", "SELECT * FROM kalillac.v1_request_own_deletion(:o,:s,:t,:v)",
              {"s": mine[0], "t": mine[1], "v": stored}))
    for role, statement, arguments in calls:
        with pytest.raises(DBAPIError, match="INJECTED_AFTER_REQUEST"):
            with w.admin_engine.begin() as c:
                c.exec_driver_sql("SET LOCAL ROLE " + w.names[role])
                assert c.execute(text(statement), {"o": uuid.uuid4(), **arguments}).one()[0] == "requested"
                assert marked(w, user)[0] is None                       # Not visible before it commits.
                c.exec_driver_sql("DO $f$ BEGIN RAISE EXCEPTION USING MESSAGE = 'INJECTED_AFTER_REQUEST'; END $f$")
        assert security_state(w) == before and marked(w, user)[0] is None
    assert authorized(w, user, mine) is True


def test_a_held_account_lock_times_out_and_changes_nothing(world):
    w = world
    user, _stored, (mine,) = signed_in(w, 1)
    before = security_state(w)
    holder = w.admin_engine.connect()
    try:
        holder.begin()
        holder.execute(text("SELECT 1 FROM kalillac.users WHERE id=:u FOR UPDATE"), {"u": user})
        with pytest.raises(DBAPIError) as error:
            operate(w, uuid.uuid4(), user, settings=("SET LOCAL lock_timeout = '200ms'",))
        assert error.value.orig.sqlstate == "55P03"
        holder.rollback()
    finally:
        holder.close()
    assert security_state(w) == before and marked(w, user)[0] is None and authorized(w, user, mine) is True


# --- the migration ---------------------------------------------------------------------------------------

def _state(w):
    """Everything 0023 changes: revision, functions, E's marker right, event types; and the guard."""
    with w.admin_engine.connect() as c:
        return (version(w),
                c.scalar(text("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                              " WHERE n.nspname = 'kalillac' AND p.proname IN"
                              " ('v1_request_deletion', 'v1_request_own_deletion')")),
                c.scalar(text("SELECT has_column_privilege(:e, 'kalillac.users', 'deletion_requested_at', 'UPDATE')"),
                         {"e": w.names["executor"]}),
                c.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                              " WHERE conname = 'ck_account_security_events_type'")),
                c.scalar(text("SELECT md5(prosrc) FROM pg_proc WHERE oid ="
                              " to_regprocedure('kalillac.registration_user_identity_guard()')")))


def test_downgrade_restores_0022_and_is_refused_once_history_exists(world):
    w = world
    user, _stored, _keys = signed_in(w, 1)
    at_head = _state(w)
    migrate(w, PREVIOUS, down=True)
    below = _state(w)
    assert below[:4] == (PREVIOUS, 0, False, v1_schema._among("event_type", v1_schema.EVENT_TYPES_V3))
    assert below[4] == at_head[4] and classify(w.owner_factory) == v1_schema.PREPARED   # Guard v3 kept.
    migrate(w, HEAD)
    assert _state(w) == at_head
    assert operate(w, uuid.uuid4(), user)[0] == "requested"
    with pytest.raises(DBAPIError, match="V1_DELETION_REQUEST_DOWNGRADE_REFUSED"):
        migrate(w, PREVIOUS, down=True)
    assert _state(w) == at_head and classify(w.owner_factory) == v1_schema.PREPARED
    assert marked(w, user)[0] is not None


@pytest.mark.parametrize("tamper", [
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('" + REQUEST + "')",
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('" + OWN + "')",
    "GRANT EXECUTE ON FUNCTION " + REQUEST + " TO {service}",
    "GRANT EXECUTE ON FUNCTION " + OWN + " TO {operator}",
    "GRANT EXECUTE ON FUNCTION " + OWN + " TO PUBLIC",
    "ALTER FUNCTION " + REQUEST + " SET search_path = public",
    "ALTER FUNCTION " + OWN + " OWNER TO {owner}",
    "REVOKE UPDATE (deletion_requested_at) ON kalillac.users FROM {executor}",
    "GRANT UPDATE (email) ON kalillac.users TO {executor}",
    "ALTER TABLE kalillac.account_security_events DROP CONSTRAINT ck_account_security_events_type",
    # 0023's postcondition still proves 0022's functions.
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('kalillac.v1_quarantine(uuid,uuid)')",
])
def test_a_tampered_catalog_is_refused_by_startup_and_by_the_downgrade(world, tamper):
    w = world
    admin_sql(w, tamper.format(**w.names))
    refused(w)
    with pytest.raises(DBAPIError, match="V1_DELETION_REQUEST_POSTCONDITION_INVALID"):
        migrate(w, PREVIOUS, down=True)
    assert version(w) == HEAD


@pytest.mark.parametrize("fault", [
    # 0022's catalog is not exact: the preflight refuses before any change.
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('kalillac.v1_reinstate(uuid,uuid)')",
    "ALTER ROLE {operator} LOGIN",
    "GRANT UPDATE (deletion_requested_at) ON kalillac.users TO {executor}",
    # A function of the same name already exists.
    "CREATE FUNCTION kalillac.v1_request_deletion(p uuid) RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$",
])
def test_a_refused_upgrade_leaves_nothing_changed(world, fault):
    w = world
    migrate(w, PREVIOUS, down=True)
    admin_sql(w, fault.format(**w.names))
    before = _state(w)
    assert before[0] == PREVIOUS
    with pytest.raises(DBAPIError, match="V1_DELETION_REQUEST_PREREQUISITE_INVALID"):
        migrate(w, HEAD)
    assert _state(w) == before


def test_a_failure_at_the_last_statement_rolls_the_whole_upgrade_back(world):
    """Fail at the very last statement (the postcondition), after the constraint, both functions and every
    grant: one transaction, so nothing of it remains, and the upgrade then succeeds."""
    import importlib.util
    from pathlib import Path
    w = world
    migrate(w, PREVIOUS, down=True)
    before = _state(w)
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0023_v1_deletion_request_authority.py"
    spec = importlib.util.spec_from_file_location("v1_0023_for_rollback", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = module.upgrade_statements(module._names())
    failing = statements[:-1] + ["DO $fail$ BEGIN RAISE EXCEPTION USING MESSAGE = 'INJECTED_AT_THE_END'; END $fail$"]
    with pytest.raises(DBAPIError, match="INJECTED_AT_THE_END"):
        with w.migration.begin() as c:
            c.exec_driver_sql("SET ROLE " + w.names["owner"])
            for statement in failing:
                c.exec_driver_sql(statement)
    assert _state(w) == before and classify(w.owner_factory) == v1_schema.PREPARED
    migrate(w, HEAD)
    assert _state(w)[:3] == (HEAD, 2, True) and classify(w.owner_factory) == v1_schema.PREPARED
