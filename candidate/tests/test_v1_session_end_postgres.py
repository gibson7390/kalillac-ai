"""0018, the private sign-out authority (V1-02D, D2 first part), on real disposable PostgreSQL 16.

The world stands on the head revision. S stays NOLOGIN here: it is exercised through SET LOCAL ROLE
from the disposable superuser, which also seeds fixtures and tampers with the catalog. Every
password hash is synthetic.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import secrets
import uuid

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

import kalillac_accounts.v1_schema as v1_schema
from kalillac_accounts.v1_security import FLAG
from registration_test_support import postgres_world
from test_v1_issuance_postgres import account, authorized, issue, key, sessions
from test_v1_revocation_postgres import admin_sql
from test_v1_schema_postgres import classify, migrate, refused, sqlstate

HEAD = "0018_v1_session_end"
PREVIOUS = "0017_billing_cases"
SIGNATURE = "kalillac.v1_end_session(uuid,text)"


@contextmanager
def head_world(monkeypatch):
    """The shared disposable world at the head revision, with superuser and owner engines."""
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
    with head_world(monkeypatch) as w:
        yield w
        assert w.admin_engine.pool.checkedout() == 0 and w.runtime_engine.pool.checkedout() == 0


def end(w, session_id, token_hash):
    """Call the function as the NOLOGIN service role S."""
    with w.admin_engine.begin() as c:
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["service"])
        return [tuple(row) for row in c.execute(text("SELECT * FROM kalillac.v1_end_session(:s,:t)"),
                                                {"s": session_id, "t": token_hash})]


def signed_in(w):
    """A verified account with one live session; returns (user, (session_id, token_hash))."""
    user, stored = account(w)
    k = key()
    assert issue(w, *k, user, stored)[0] == "issued"
    return user, k


def present(w):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT to_regprocedure(:f) IS NOT NULL"), {"f": SIGNATURE})


def test_the_head_classifies_as_prepared_with_three_authorities(world):
    assert classify(world.owner_factory) == classify(world.runtime_factory) == v1_schema.PREPARED
    with world.admin_engine.connect() as c:
        acl = c.scalar(text("SELECT array_agg(pg_get_userbyid(x.grantee) ORDER BY 1) FROM pg_proc p,"
                            " aclexplode(p.proacl) x WHERE p.oid = to_regprocedure(:f)"), {"f": SIGNATURE})
        owner = c.scalar(text("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid = to_regprocedure(:f)"),
                         {"f": SIGNATURE})
    assert sorted(acl) == sorted([world.names["executor"], world.names["service"]])
    assert owner == world.names["executor"]


def test_sign_out_revokes_exactly_the_presented_session(world):
    w = world
    user, mine = signed_in(w)
    _, theirs = signed_in(w)
    second = key()
    stored = account_hash(w, user)
    assert issue(w, *second, user, stored)[0] == "issued"
    assert end(w, *mine) == [("ended",)]
    rows = {row[0]: row[4] for row in sessions(w, user)}
    assert rows[mine[0]] is not None and rows[second[0]] is None       # Only the presented session.
    assert authorized(w, user, mine) == 401 and authorized(w, user, second) is True
    assert authorized(w, theirs_user(w, theirs), theirs) is True       # Another account is untouched.
    ended_at = rows[mine[0]]
    assert end(w, *mine) == [("ended",)]                                 # Idempotent: the time stays.
    assert {row[0]: row[4] for row in sessions(w, user)}[mine[0]] == ended_at


def test_every_non_matching_call_answers_the_same_and_changes_nothing(world):
    w = world
    user, mine = signed_in(w)
    before = sessions(w, user)
    for session_id, token_hash in ((mine[0], secrets.token_hex(32)),       # Right id, wrong token.
                                   (uuid.uuid4(), mine[1]),                 # Unknown id, right token.
                                   (uuid.uuid4(), secrets.token_hex(32))):  # Nothing known.
        assert end(w, session_id, token_hash) == [("ended",)]
    assert sessions(w, user) == before and authorized(w, user, mine) is True
    for token_hash in (None, mine[1].upper(), mine[1][:-1], "x" * 64):
        assert end(w, mine[0], token_hash) == [("security_unavailable",)]
    assert end(w, None, mine[1]) == [("security_unavailable",)]
    assert sessions(w, user) == before


def test_only_s_may_call_it(world):
    w = world
    for engine in (w.runtime_engine, w.engine):                           # R, and A (PUBLIC's rights too).
        with engine.connect() as c:
            assert sqlstate(c, "SELECT * FROM kalillac.v1_end_session(:s,:t)",
                            {"s": uuid.uuid4(), "t": secrets.token_hex(32)}) == "42501"


def test_downgrade_and_upgrade_restore_the_exact_catalogs(world):
    w = world
    user, mine = signed_in(w)
    assert end(w, *mine) == [("ended",)]
    migrate(w, PREVIOUS, down=True)
    assert not present(w) and classify(w.owner_factory) == v1_schema.PREPARED
    assert {row[0]: row[4] for row in sessions(w, user)}[mine[0]] is not None   # Ended stays ended.
    migrate(w, HEAD)
    assert present(w) and classify(w.owner_factory) == v1_schema.PREPARED


@pytest.mark.parametrize("tamper", [
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('" + SIGNATURE + "')",
    "GRANT EXECUTE ON FUNCTION " + SIGNATURE + " TO PUBLIC",
    "GRANT EXECUTE ON FUNCTION " + SIGNATURE + " TO {app}",
    "ALTER FUNCTION " + SIGNATURE + " SET search_path = public",
    "ALTER FUNCTION " + SIGNATURE + " OWNER TO {owner}",
])
def test_a_tampered_function_is_refused_by_startup_and_by_the_downgrade(world, tamper):
    w = world
    admin_sql(w, tamper.format(**w.names))
    refused(w)
    with pytest.raises(DBAPIError, match="V1_SESSION_END_POSTCONDITION_INVALID"):
        migrate(w, PREVIOUS, down=True)
    assert present(w)


def test_the_upgrade_refuses_a_preexisting_sign_out_function(world):
    w = world
    migrate(w, PREVIOUS, down=True)
    with w.migration.begin() as c:
        c.exec_driver_sql("SET ROLE " + w.names["owner"])
        c.exec_driver_sql("CREATE FUNCTION kalillac.v1_end_session(p_session_id uuid, p_token_hash text)"
                          " RETURNS TABLE(outcome text) LANGUAGE sql AS $$ SELECT 'ended'::text $$")
        c.exec_driver_sql("REVOKE ALL ON FUNCTION kalillac.v1_end_session(uuid, text) FROM PUBLIC")
    with pytest.raises(DBAPIError, match="V1_SESSION_END_PREREQUISITE_INVALID"):
        migrate(w, HEAD)
    with w.migration.begin() as c:
        c.exec_driver_sql("SET ROLE " + w.names["owner"])
        c.exec_driver_sql("DROP FUNCTION kalillac.v1_end_session(uuid, text)")
    migrate(w, HEAD)
    assert classify(w.owner_factory) == v1_schema.PREPARED


def account_hash(w, user):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT password_hash FROM kalillac.users WHERE id = :u"), {"u": user})


def theirs_user(w, k):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT user_id FROM kalillac.account_sessions WHERE id = :s"), {"s": k[0]})
