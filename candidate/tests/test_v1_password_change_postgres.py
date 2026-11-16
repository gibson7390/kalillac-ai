"""0020, the private password-change authority (V1-02D, D2 third part), on real disposable PostgreSQL 16.

The world stands on the head revision (0021 leaves 0020's function and constraint unchanged).
S stays NOLOGIN here: it is exercised through SET LOCAL ROLE
from the disposable superuser, which also seeds fixtures and tampers with the catalog. Every password
hash is synthetic.
"""
from __future__ import annotations

import secrets
import threading
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import kalillac_accounts.v1_schema as v1_schema
from test_v1_issuance_postgres import account, authorized, epoch, issue, key, sessions
from test_v1_password_reset_postgres import new_hash, stored
from test_v1_revocation_postgres import admin_sql, revoke, security_state, set_user
from test_v1_schema_postgres import classify, migrate, refused, sqlstate
from test_v1_session_end_postgres import head_world

HEAD = "0020_v1_password_change"
PREVIOUS = "0019_v1_password_reset"
SIGNATURE = "kalillac.v1_change_password(uuid,uuid,text,text,text)"
DENIED = ("not_authenticated", None, False)
UNAVAILABLE = ("security_unavailable", None, False)


@pytest.fixture
def world(monkeypatch):
    with head_world(monkeypatch) as w:
        yield w
        assert w.admin_engine.pool.checkedout() == 0 and w.runtime_engine.pool.checkedout() == 0


def change(w, operation, k, verified, hashed):
    """Call the function as the NOLOGIN service role S."""
    with w.admin_engine.begin() as c:
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["service"])
        return tuple(c.execute(text("SELECT * FROM kalillac.v1_change_password(:o,:s,:t,:v,:h)"),
                               {"o": operation, "s": k[0], "t": k[1], "v": verified, "h": hashed}).one())


def signed_in(w, count=2):
    """An account with `count` live sessions; returns (user, stored hash, keys)."""
    user, old = account(w)
    keys = [key() for _ in range(count)]
    for k in keys:
        assert issue(w, *k, user, old)[0] == "issued"
    return user, old, keys


def present(w):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT to_regprocedure(:f) IS NOT NULL"), {"f": SIGNATURE})


def test_the_head_classifies_as_prepared_with_five_authorities(world):
    w = world
    assert classify(w.owner_factory) == v1_schema.PREPARED
    with w.admin_engine.connect() as c:
        acl = c.scalar(text("SELECT array_agg(pg_get_userbyid(x.grantee) ORDER BY 1) FROM pg_proc p,"
                            " aclexplode(p.proacl) x WHERE p.oid = to_regprocedure(:f)"), {"f": SIGNATURE})
        definition = c.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                                   " WHERE conname = 'ck_account_security_events_type'"))
    assert sorted(acl) == sorted([w.names["executor"], w.names["service"]])
    assert "'password_change'::character varying" in definition       # Later revisions widen it further.


def test_a_change_replaces_the_hash_keeps_this_session_ends_the_others_and_records_once(world):
    w = world
    user, old, (mine, other, third) = signed_in(w, 3)
    stranger, stranger_old, (theirs,) = signed_in(w, 1)
    before_epoch = epoch(w, user)
    operation, hashed = uuid.uuid4(), new_hash()
    assert change(w, operation, mine, old, hashed) == ("changed", None, False)
    assert stored(w, user)[0] == hashed and epoch(w, user) == before_epoch   # The epoch does not move.
    rows = {row[0]: row[4] for row in sessions(w, user)}
    assert rows[mine[0]] is None and rows[other[0]] is not None and rows[third[0]] is not None
    assert authorized(w, user, mine) is True and authorized(w, user, other) == 401
    events = [row for row in security_state(w)[0] if row[1] == user]
    assert [(row[0], row[2]) for row in events] == [(operation, "password_change")]
    assert rows[other[0]] == events[0][3]                                     # One effective time.
    admission = [row for row in security_state(w)[1] if row[1] == user]
    assert admission[0][2] == [events[0][3]] and admission[0][3] == []      # The manual ring, charged once.
    # Signing in again needs the new password; the old one is refused.
    assert issue(w, *key(), user, old)[0] == "not_authenticated"
    assert issue(w, *key(), user, hashed)[0] == "issued"
    # Another account is untouched.
    assert stored(w, stranger)[0] == stranger_old and authorized(w, stranger, theirs) is True
    # A replay reports and applies nothing.
    state = security_state(w)
    assert change(w, operation, mine, hashed, new_hash()) == ("changed", None, True)
    assert security_state(w) == state and stored(w, user)[0] == hashed
    assert change(w, operation, theirs, stranger_old, new_hash()) == UNAVAILABLE   # Not this account's operation.


def test_a_stale_or_wrong_proof_is_denied_uniformly_and_changes_nothing(world):
    w = world
    user, old, (mine, other) = signed_in(w)
    revoked = key()
    assert issue(w, *revoked, user, old)[0] == "issued"
    assert revoke(w, uuid.uuid4(), mine, "others")[0] == "revoked"            # Ends `other` and `revoked`.
    state = security_state(w)
    for k, verified in ((mine, old + "x"),                                    # A hash that is no longer stored.
                        (other, old), (revoked, old),                          # Revoked sessions.
                        ((mine[0], secrets.token_hex(32)), old),               # Right id, wrong token.
                        ((uuid.uuid4(), mine[1]), old)):                       # Unknown session.
        assert change(w, uuid.uuid4(), k, verified, new_hash()) == DENIED
    assert security_state(w) == state and stored(w, user)[0] == old
    set_user(w, user, "password_reset_required = true")
    assert change(w, uuid.uuid4(), mine, old, new_hash()) == DENIED          # Reset first.
    set_user(w, user, "password_reset_required = false, is_active = false")
    assert change(w, uuid.uuid4(), mine, old, new_hash()) == DENIED
    set_user(w, user, "is_active = true, deletion_requested_at = clock_timestamp()")
    assert change(w, uuid.uuid4(), mine, old, new_hash()) == DENIED
    set_user(w, user, "deletion_requested_at = NULL, quarantined_at = clock_timestamp()")
    assert change(w, uuid.uuid4(), mine, old, new_hash()) == ("account_unavailable", None, False)
    assert security_state(w) == state and stored(w, user)[0] == old


@pytest.mark.parametrize("field, value", [
    ("operation", None), ("session", None), ("token", None), ("token", "A" * 64), ("token", "a" * 63),
    ("verified", None), ("verified", "h" * 256), ("new", None), ("new", "plain password"),
    ("new", "$argon2i$v=19$m=65536,t=3,p=4$" + "s" * 22 + "$" + "h" * 43),
    ("new", "$argon2id$v=19$m=65536,t=3,p=4$" + "s" * 22 + "$" + "h" * 250),
])
def test_malformed_calls_are_refused_and_store_nothing(world, field, value):
    w = world
    user, old, (mine,) = signed_in(w, 1)
    arguments = {"operation": uuid.uuid4(), "session": mine[0], "token": mine[1], "verified": old, "new": new_hash()}
    arguments[field] = value
    state = security_state(w)
    assert change(w, arguments["operation"], (arguments["session"], arguments["token"]), arguments["verified"],
                  arguments["new"]) == UNAVAILABLE
    assert security_state(w) == state and stored(w, user)[0] == old


def test_changes_and_revocations_share_the_customer_admission_budget(world):
    w = world
    user, old, (mine,) = signed_in(w, 1)
    first = new_hash()
    assert change(w, uuid.uuid4(), mine, old, first)[0] == "changed"
    assert revoke(w, uuid.uuid4(), mine, "others")[0] == "revoked"
    outcome, wait, replayed = change(w, uuid.uuid4(), mine, first, new_hash())
    assert outcome == "too_many_operations" and 1 <= wait <= 60 and replayed is False
    assert stored(w, user)[0] == first


def test_concurrent_changes_with_one_verified_hash_apply_exactly_once(world):
    w = world
    user, old, (mine,) = signed_in(w, 1)
    results, start = [], threading.Barrier(3)

    def attempt():
        start.wait(10)
        results.append(change(w, uuid.uuid4(), mine, old, new_hash()))
    threads = [threading.Thread(target=attempt) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(r[0] for r in results) == ["changed", "not_authenticated", "not_authenticated"]
    assert [row[2] for row in security_state(w)[0] if row[1] == user] == ["password_change"]


def test_only_s_may_call_it(world):
    w = world
    for engine in (w.runtime_engine, w.engine):                           # R, and A (PUBLIC's rights too).
        with engine.connect() as c:
            assert sqlstate(c, "SELECT * FROM kalillac.v1_change_password(:o,:s,:t,:v,:h)",
                            {"o": uuid.uuid4(), "s": uuid.uuid4(), "t": secrets.token_hex(32), "v": "x",
                             "h": new_hash()}) == "42501"


def test_the_runtime_cannot_record_a_password_change_itself(world):
    w = world
    user, _old, _keys = signed_in(w, 1)
    with w.runtime_engine.connect() as c:
        assert sqlstate(c, "INSERT INTO kalillac.account_security_events(operation_id,user_id,event_type,occurred_at)"
                           " VALUES (:o,:u,'password_change',now())", {"o": uuid.uuid4(), "u": user}) == "42501"


def test_downgrade_is_refused_while_change_history_exists_and_otherwise_restores_0019(world):
    w = world
    user, old, (mine,) = signed_in(w, 1)
    migrate(w, PREVIOUS, down=True)                                        # No change yet: allowed.
    assert not present(w) and classify(w.owner_factory) == v1_schema.PREPARED
    with w.admin_engine.connect() as c:
        assert c.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                             " WHERE conname = 'ck_account_security_events_type'")) == v1_schema._among(
            "event_type", v1_schema.EVENT_TYPES)
    migrate(w, HEAD)
    assert present(w) and classify(w.owner_factory) == v1_schema.PREPARED
    hashed = new_hash()
    assert change(w, uuid.uuid4(), mine, old, hashed)[0] == "changed"
    with pytest.raises(DBAPIError, match="V1_PASSWORD_CHANGE_DOWNGRADE_REFUSED"):
        migrate(w, PREVIOUS, down=True)
    assert present(w) and classify(w.owner_factory) == v1_schema.PREPARED and stored(w, user)[0] == hashed


@pytest.mark.parametrize("tamper", [
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('" + SIGNATURE + "')",
    "GRANT EXECUTE ON FUNCTION " + SIGNATURE + " TO PUBLIC",
    "GRANT EXECUTE ON FUNCTION " + SIGNATURE + " TO {app}",
    "ALTER FUNCTION " + SIGNATURE + " SET search_path = public",
    "ALTER FUNCTION " + SIGNATURE + " OWNER TO {owner}",
    "ALTER TABLE kalillac.account_security_events DROP CONSTRAINT ck_account_security_events_type",
    "ALTER TABLE kalillac.account_security_events DROP CONSTRAINT ck_account_security_events_type;"
    " ALTER TABLE kalillac.account_security_events ADD CONSTRAINT ck_account_security_events_type"
    " CHECK (event_type IN ('session_revoke_others','session_revoke_all','password_reset','password_change','x'))",
    "GRANT UPDATE (email) ON kalillac.users TO {executor}",
    # 0020's postcondition still proves 0019's function.
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('kalillac.v1_reset_password(uuid,text,text)')",
])
def test_a_tampered_catalog_is_refused_by_startup_and_by_the_downgrade(world, tamper):
    w = world
    migrate(w, HEAD, down=True)                  # 0020's own downgrade; later revisions test theirs.
    admin_sql(w, *tamper.format(**w.names).split("; "))
    refused(w)
    with pytest.raises(DBAPIError, match="V1_PASSWORD_CHANGE_POSTCONDITION_INVALID"):
        migrate(w, PREVIOUS, down=True)
    assert present(w)


def test_the_upgrade_refuses_a_preexisting_change_function(world):
    w = world
    migrate(w, PREVIOUS, down=True)
    with w.migration.begin() as c:
        c.exec_driver_sql("SET ROLE " + w.names["owner"])
        c.exec_driver_sql("CREATE FUNCTION kalillac.v1_change_password(p uuid) RETURNS integer"
                          " LANGUAGE sql AS $$ SELECT 1 $$")
        c.exec_driver_sql("REVOKE ALL ON FUNCTION kalillac.v1_change_password(uuid) FROM PUBLIC")
    with pytest.raises(DBAPIError, match="V1_PASSWORD_CHANGE_PREREQUISITE_INVALID"):
        migrate(w, HEAD)
    with w.migration.begin() as c:
        c.exec_driver_sql("SET ROLE " + w.names["owner"])
        c.exec_driver_sql("DROP FUNCTION kalillac.v1_change_password(uuid)")
    migrate(w, HEAD)
    assert classify(w.owner_factory) == v1_schema.PREPARED
