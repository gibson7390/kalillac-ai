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
    for role in ("service", "executor"):
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
