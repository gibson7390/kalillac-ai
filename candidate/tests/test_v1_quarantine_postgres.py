"""0022, quarantine and reinstatement by the operator role P, on real disposable PostgreSQL 16.

P and S stay NOLOGIN: each is exercised through SET LOCAL ROLE from the disposable superuser, which also
seeds fixtures and tampers with the catalog. Every password hash is synthetic.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import kalillac_accounts.v1_schema as v1_schema
from test_v1_issuance_postgres import account, authorized, epoch, issue, key, sessions
from test_v1_revocation_postgres import admin_sql, revoke, security_state
from test_v1_schema_postgres import classify, migrate, refused, sqlstate
from test_v1_session_end_postgres import head_world

HEAD = "0022_v1_quarantine"
PREVIOUS = "0021_registration_activation"
QUARANTINE, REINSTATE = "kalillac.v1_quarantine(uuid,uuid)", "kalillac.v1_reinstate(uuid,uuid)"


@pytest.fixture
def world(monkeypatch):
    with head_world(monkeypatch) as w:
        yield w


def operate(w, function, operation, user, role="operator"):
    """Call an operator function as P (or another role, to show it is refused)."""
    with w.admin_engine.begin() as c:
        c.exec_driver_sql("SET LOCAL ROLE " + w.names[role])
        return tuple(c.execute(text(f"SELECT * FROM kalillac.{function}(:o,:u)"), {"o": operation, "u": user}).one())


def quarantined_at(w, user):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT quarantined_at FROM kalillac.users WHERE id=:u"), {"u": user})


def signed_in(w, count=2):
    user, stored = account(w)
    keys = [key() for _ in range(count)]
    for k in keys:
        assert issue(w, *k, user, stored)[0] == "issued"
    return user, stored, keys


def test_the_head_classifies_with_p_and_guard_v3(world):
    w = world
    assert classify(w.owner_factory) == v1_schema.PREPARED
    with w.admin_engine.connect() as c:
        for signature in (QUARANTINE, REINSTATE):
            acl = c.scalar(text("SELECT array_agg(pg_get_userbyid(x.grantee) ORDER BY 1) FROM pg_proc p,"
                                " aclexplode(p.proacl) x WHERE p.oid = to_regprocedure(:f)"), {"f": signature})
            assert sorted(acl) == sorted([w.names["executor"], w.names["operator"]])


def test_quarantine_ends_every_session_and_blocks_sign_in_then_reinstatement_lets_the_customer_back(world):
    w = world
    user, stored, keys = signed_in(w)
    stranger, _, (theirs,) = signed_in(w, 1)
    operation = uuid.uuid4()
    assert operate(w, "v1_quarantine", operation, user) == ("quarantined", False)
    moment = quarantined_at(w, user)
    assert moment is not None and epoch(w, user) == moment
    assert all(row[4] == moment for row in sessions(w, user))
    assert all(authorized(w, user, k) != True for k in keys)                      # noqa: E712
    assert issue(w, *key(), user, stored)[0] == "not_authenticated"              # No new session either.
    assert authorized(w, stranger, theirs) is True                               # Another account untouched.
    events = [row for row in security_state(w)[0] if row[1] == user]
    assert [(row[0], row[2], row[3]) for row in events] == [(operation, "account_quarantine", moment)]
    # Replays report and change nothing; a second quarantine is refused.
    state = security_state(w)
    assert operate(w, "v1_quarantine", operation, user) == ("quarantined", True)
    assert operate(w, "v1_quarantine", uuid.uuid4(), user) == ("already_quarantined", False)
    assert security_state(w) == state
    # Reinstatement clears the mark only: sessions stay ended, the customer signs in again.
    back = uuid.uuid4()
    assert operate(w, "v1_reinstate", back, user) == ("reinstated", False)
    assert quarantined_at(w, user) is None and epoch(w, user) == moment
    assert all(row[4] == moment for row in sessions(w, user))
    fresh = key()
    assert issue(w, *fresh, user, stored)[0] == "issued" and authorized(w, user, fresh) is True
    assert operate(w, "v1_reinstate", back, user) == ("reinstated", True)
    assert operate(w, "v1_reinstate", uuid.uuid4(), user) == ("not_quarantined", False)
    assert operate(w, "v1_quarantine", uuid.uuid4(), uuid.uuid4()) == ("not_found", False)
    assert operate(w, "v1_quarantine", None, user) == ("security_unavailable", False)


def test_a_quarantined_account_cannot_revoke_or_change_anything_itself(world):
    w = world
    user, _stored, (mine, other) = signed_in(w)
    assert operate(w, "v1_quarantine", uuid.uuid4(), user)[0] == "quarantined"
    assert revoke(w, uuid.uuid4(), mine, "others")[0] == "not_authenticated"   # Its sessions are gone.


def test_only_p_may_call_the_operator_functions(world):
    w = world
    user, _, _ = signed_in(w, 1)
    # S may not; E owns the functions (owner EXECUTE, as for every V1 authority) and is NOLOGIN, reachable
    # only through the operator's owner->E edge, so it is not a caller to refuse here.
    for role in ("service",):
        with pytest.raises(DBAPIError) as error:
            operate(w, "v1_quarantine", uuid.uuid4(), user, role=role)
        assert error.value.orig.sqlstate == "42501"
    for engine in (w.runtime_engine, w.engine):
        with engine.connect() as c:
            assert sqlstate(c, "SELECT * FROM kalillac.v1_reinstate(:o,:u)", {"o": uuid.uuid4(), "u": user}) == "42501"
    assert quarantined_at(w, user) is None


def test_guard_v3_admits_only_the_two_transitions(world):
    w = world
    user, _, _ = signed_in(w, 1)

    def owner_update(assignment):
        with w.migration.begin() as c:
            c.exec_driver_sql("SET ROLE " + w.names["owner"])
            c.execute(text(f"UPDATE kalillac.users SET {assignment} WHERE id = :u"), {"u": user})
    with pytest.raises(DBAPIError, match="REGISTRATION_IDENTITY_IMMUTABLE"):     # No epoch move.
        owner_update("quarantined_at = clock_timestamp()")
    with pytest.raises(DBAPIError, match="REGISTRATION_IDENTITY_IMMUTABLE"):     # Epoch before the quarantine.
        owner_update("sessions_valid_after = clock_timestamp(), quarantined_at = clock_timestamp() + interval '1 day'")
    owner_update("sessions_valid_after = clock_timestamp(), quarantined_at = clock_timestamp()")   # Admitted shape.
    with pytest.raises(DBAPIError, match="REGISTRATION_IDENTITY_IMMUTABLE"):     # Moved, not cleared.
        owner_update("quarantined_at = quarantined_at + interval '1 second'")
    with pytest.raises(DBAPIError, match="REGISTRATION_IDENTITY_IMMUTABLE"):     # Clearing may not move the epoch.
        owner_update("quarantined_at = NULL, sessions_valid_after = sessions_valid_after + interval '1 second'")
    owner_update("quarantined_at = NULL")
    with w.runtime_engine.connect() as c:                                      # R has no column privilege.
        assert sqlstate(c, "UPDATE kalillac.users SET quarantined_at = clock_timestamp() WHERE id = :u",
                        {"u": user}) == "42501"


def test_downgrade_restores_0021_and_is_refused_once_history_exists(world):
    w = world
    user, _, _ = signed_in(w, 1)
    migrate(w, PREVIOUS, down=True)
    assert classify(w.owner_factory) == v1_schema.PREPARED
    with w.admin_engine.connect() as c:
        assert c.scalar(text("SELECT to_regprocedure(:f) IS NULL"), {"f": QUARANTINE})
    migrate(w, HEAD)
    assert operate(w, "v1_quarantine", uuid.uuid4(), user)[0] == "quarantined"
    with pytest.raises(DBAPIError, match="V1_QUARANTINE_DOWNGRADE_REFUSED"):
        migrate(w, PREVIOUS, down=True)
    assert classify(w.owner_factory) == v1_schema.PREPARED


@pytest.mark.parametrize("tamper", [
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('" + QUARANTINE + "')",
    "GRANT EXECUTE ON FUNCTION " + QUARANTINE + " TO {service}",
    "GRANT EXECUTE ON FUNCTION " + REINSTATE + " TO PUBLIC",
    "GRANT EXECUTE ON FUNCTION kalillac.v1_end_session(uuid,text) TO {operator}",
    "GRANT {app} TO {operator}",
    "ALTER ROLE {operator} INHERIT",
    "GRANT SELECT ON kalillac.users TO {operator}",
    "REVOKE UPDATE (quarantined_at) ON kalillac.users FROM {executor}",
])
def test_a_tampered_catalog_is_refused_by_startup_and_by_the_downgrade(world, tamper):
    w = world
    admin_sql(w, tamper.format(**w.names))
    refused(w)
    with pytest.raises(DBAPIError, match="V1_QUARANTINE_POSTCONDITION_INVALID"):
        migrate(w, PREVIOUS, down=True)


# --- the guard swap and all-or-nothing behaviour, in the database itself ------------------------------

def _guard_source(w):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT p.prosrc FROM pg_proc p WHERE p.oid = "
                             "to_regprocedure('kalillac.registration_user_identity_guard()')"))


def _expected_guard(w, version):
    from kalillac_accounts import registration_schema
    build = registration_schema.identity_guard_v3 if version == 3 else registration_schema.identity_guard_v2
    return build(w.names["registration"]).split("$guard$")[1]


def _state(w):
    """Everything 0022 changes: revision, guard, functions, P's and E's rights, event types."""
    from test_v1_schema_postgres import version
    with w.admin_engine.connect() as c:
        return (version(w), _guard_source(w),
                c.scalar(text("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                              " WHERE n.nspname = 'kalillac' AND p.proname IN ('v1_quarantine', 'v1_reinstate')")),
                c.scalar(text("SELECT has_schema_privilege(:p, 'kalillac', 'USAGE')"), {"p": w.names["operator"]}),
                c.scalar(text("SELECT has_column_privilege(:e, 'kalillac.users', 'quarantined_at', 'UPDATE')"),
                         {"e": w.names["executor"]}),
                c.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                              " WHERE conname = 'ck_account_security_events_type'")))


def test_the_upgrade_installs_exactly_guard_v3_over_exactly_v2(world):
    w = world
    assert _guard_source(w) == _expected_guard(w, 3)
    migrate(w, PREVIOUS, down=True)
    assert _guard_source(w) == _expected_guard(w, 2)
    migrate(w, HEAD)
    assert _guard_source(w) == _expected_guard(w, 3)


@pytest.mark.parametrize("fault, message", [
    # The guard is not exactly v2: the preflight's catalog proof refuses it before any change.
    ("UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = "
     "to_regprocedure('kalillac.registration_user_identity_guard()')", "V1_QUARANTINE_PREREQUISITE_INVALID"),
    # P is not as the operator must provision it.
    ("ALTER ROLE {operator} LOGIN", "V1_QUARANTINE_PREREQUISITE_INVALID"),
    ("GRANT {app} TO {operator}", "V1_QUARANTINE_PREREQUISITE_INVALID"),
])
def test_a_refused_upgrade_leaves_nothing_changed(world, fault, message):
    w = world
    migrate(w, PREVIOUS, down=True)
    admin_sql(w, fault.format(**w.names))
    before = _state(w)
    assert before[0] == PREVIOUS and before[2] == 0
    with pytest.raises(DBAPIError, match=message):
        migrate(w, HEAD)
    assert _state(w) == before                 # Revision, guard, functions, rights and event types unchanged.


def test_a_failure_after_the_swap_rolls_the_swap_back(world, monkeypatch):
    """Fail at the very last statement (the postcondition), after the guard swap, the functions and the
    grants: one transaction, so nothing of it remains."""
    import importlib.util
    from pathlib import Path
    w = world
    migrate(w, PREVIOUS, down=True)
    before = _state(w)
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0022_v1_quarantine_authority.py"
    spec = importlib.util.spec_from_file_location("v1_0022_for_rollback", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = module.upgrade_statements(module._names())
    failing = statements[:-1] + ["DO $fail$ BEGIN RAISE EXCEPTION USING MESSAGE = 'INJECTED_AFTER_SWAP'; END $fail$"]
    with pytest.raises(DBAPIError, match="INJECTED_AFTER_SWAP"):
        with w.migration.begin() as c:
            c.exec_driver_sql("SET ROLE " + w.names["owner"])
            for statement in failing:
                c.exec_driver_sql(statement)
    assert _state(w) == before and _guard_source(w) == _expected_guard(w, 2)
    migrate(w, HEAD)
    assert _guard_source(w) == _expected_guard(w, 3) and classify(w.owner_factory) == v1_schema.PREPARED
