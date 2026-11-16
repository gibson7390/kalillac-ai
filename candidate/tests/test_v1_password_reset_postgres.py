"""0019, the private password-reset authority (V1-02D, D2 second part), on real disposable PostgreSQL 16.

The world stands on the head revision. S stays NOLOGIN here: it is exercised through SET LOCAL ROLE
from the disposable superuser, which also seeds fixtures and tampers with the catalog. Every password
hash and token is synthetic; tokens are stored only as their digests, as the application stores them.
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
from test_v1_revocation_postgres import admin_sql, security_state, set_user
from test_v1_schema_postgres import classify, migrate, refused, sqlstate
from test_v1_session_end_postgres import head_world

HEAD = "0019_v1_password_reset"
PREVIOUS = "0018_v1_session_end"
SIGNATURE = "kalillac.v1_reset_password(uuid,text,text)"
INVALID = ("invalid_token", None, False)
UNAVAILABLE = ("security_unavailable", None, False)


@pytest.fixture
def world(monkeypatch):
    with head_world(monkeypatch) as w:
        yield w
        assert w.admin_engine.pool.checkedout() == 0 and w.runtime_engine.pool.checkedout() == 0


def new_hash():
    """A synthetic Argon2id encoding in the application's shape."""
    return "$argon2id$v=19$m=65536,t=3,p=4$" + secrets.token_hex(11) + "$" + secrets.token_hex(16)


def token(w, user, *, minutes=30, created_minutes_ago=0, consumed=False):
    """A stored reset token; returns its digest (the token itself never exists here)."""
    digest = secrets.token_hex(32)
    admin_sql(w, "INSERT INTO kalillac.account_recovery_tokens(id,user_id,purpose,token_hash,created_at,expires_at,"
                 "consumed_at) VALUES (:i,:u,'password_reset',:t,clock_timestamp()-make_interval(mins=>:c),"
                 "clock_timestamp()+make_interval(mins=>:m),CASE WHEN :d THEN clock_timestamp() END)",
              i=uuid.uuid4(), u=user, t=digest, m=minutes, c=created_minutes_ago, d=consumed)
    return digest


def reset(w, operation, digest, hashed):
    """Call the function as the NOLOGIN service role S."""
    with w.admin_engine.begin() as c:
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["service"])
        return tuple(c.execute(text("SELECT * FROM kalillac.v1_reset_password(:o,:t,:h)"),
                               {"o": operation, "t": digest, "h": hashed}).one())


def stored(w, user):
    with w.admin_engine.connect() as c:
        return tuple(c.execute(text("SELECT password_hash,password_reset_required,sessions_valid_after"
                                    " FROM kalillac.users WHERE id=:u"), {"u": user}).one())


def tokens(w, user):
    with w.admin_engine.connect() as c:
        return {r[0]: r[1] for r in c.execute(text("SELECT token_hash,consumed_at FROM kalillac.account_recovery_tokens"
                                                   " WHERE user_id=:u"), {"u": user})}


def with_sessions(w, count=2):
    user, old = account(w)
    keys = [key() for _ in range(count)]
    for k in keys:
        assert issue(w, *k, user, old)[0] == "issued"
    return user, old, keys


def present(w):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT to_regprocedure(:f) IS NOT NULL"), {"f": SIGNATURE})


def test_the_head_classifies_as_prepared_with_four_authorities(world):
    w = world
    assert classify(w.owner_factory) == v1_schema.PREPARED
    with pytest.raises(v1_schema.V1SchemaRefused, match="authority_state_unreadable"):
        classify(w.runtime_factory)
    with w.admin_engine.connect() as c:
        acl = c.scalar(text("SELECT array_agg(pg_get_userbyid(x.grantee) ORDER BY 1) FROM pg_proc p,"
                            " aclexplode(p.proacl) x WHERE p.oid = to_regprocedure(:f)"), {"f": SIGNATURE})
        owner = c.scalar(text("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid = to_regprocedure(:f)"),
                         {"f": SIGNATURE})
    assert sorted(acl) == sorted([w.names["executor"], w.names["service"]]) and owner == w.names["executor"]


def test_a_reset_replaces_the_hash_consumes_tokens_revokes_sessions_and_records_once(world):
    w = world
    user, old, keys = with_sessions(w)
    other, other_old, other_keys = with_sessions(w, 1)
    set_user(w, user, "password_reset_required = true")
    digest, spare = token(w, user), token(w, user)
    other_digest = token(w, other)
    before_epoch = epoch(w, user)
    operation, hashed = uuid.uuid4(), new_hash()
    assert reset(w, operation, digest, hashed) == ("reset", None, False)

    password_hash, required, after_epoch = stored(w, user)
    assert password_hash == hashed and required is False
    assert after_epoch is not None and (before_epoch is None or after_epoch > before_epoch)
    consumed = tokens(w, user)
    assert consumed[digest] is not None and consumed[spare] is not None    # Every open token of the account.
    assert all(row[4] is not None for row in sessions(w, user))
    assert all(authorized(w, user, k) == 401 for k in keys)
    # One effective time for every effect.
    events = [row for row in security_state(w)[0] if row[1] == user]
    assert [(row[0], row[2]) for row in events] == [(operation, "password_reset")]
    assert {consumed[digest], consumed[spare], after_epoch, events[0][3],
            *(row[4] for row in sessions(w, user))} == {events[0][3]}
    # The recovery class was charged, not the manual one.
    admission = [row for row in security_state(w)[1] if row[1] == user]
    assert len(admission) == 1 and admission[0][2] == [] and admission[0][3] == [events[0][3]]
    assert [row[0] for row in security_state(w)[2]] == ["recovery"]
    # The other account is untouched.
    assert stored(w, other)[0] == other_old and tokens(w, other)[other_digest] is None
    assert authorized(w, other, other_keys[0]) is True

    # Only the new hash signs in; sessions created before the epoch stay dead.
    assert issue(w, *key(), user, old)[0] == "not_authenticated"
    fresh = key()
    assert issue(w, *fresh, user, hashed)[0] == "issued" and authorized(w, user, fresh) is True

    # The same operation is reported again and applies nothing.
    state = security_state(w)
    assert reset(w, operation, digest, new_hash()) == ("reset", None, True)
    assert security_state(w) == state and stored(w, user)[0] == hashed
    # A consumed token proves nothing any more.
    assert reset(w, uuid.uuid4(), digest, new_hash()) == INVALID
    # The operation id belongs to this account and kind only.
    assert reset(w, operation, other_digest, new_hash()) == UNAVAILABLE
    assert security_state(w) == state and tokens(w, other)[other_digest] is None


def test_every_invalid_token_answers_the_same_and_changes_nothing(world):
    w = world
    user, _, keys = with_sessions(w, 1)
    cases = [secrets.token_hex(32),                                       # Unknown.
             token(w, user, consumed=True),                                # Consumed.
             token(w, user, minutes=-1, created_minutes_ago=31)]           # Expired.
    inactive, _, _ = with_sessions(w, 1)
    cases.append(token(w, inactive))
    set_user(w, inactive, "is_active = false")
    deleting, _, _ = with_sessions(w, 1)
    cases.append(token(w, deleting))
    set_user(w, deleting, "deletion_requested_at = clock_timestamp()")
    state = security_state(w)
    for digest in cases:
        assert reset(w, uuid.uuid4(), digest, new_hash()) == INVALID
    assert security_state(w) == state and authorized(w, user, keys[0]) is True


def test_a_quarantined_account_is_unavailable_and_unchanged(world):
    w = world
    user, old, keys = with_sessions(w, 1)
    digest = token(w, user)
    set_user(w, user, "quarantined_at = clock_timestamp()")
    state = security_state(w)
    assert reset(w, uuid.uuid4(), digest, new_hash()) == ("account_unavailable", None, False)
    assert security_state(w) == state and stored(w, user)[0] == old and tokens(w, user)[digest] is None


@pytest.mark.parametrize("operation, digest, hashed", [
    (None, "a" * 64, "ok"), ("op", None, "ok"), ("op", "A" * 64, "ok"), ("op", "a" * 63, "ok"),
    ("op", "t", None), ("op", "t", ""), ("op", "t", "correct horse battery staple"),
    ("op", "t", "$argon2i$v=19$m=65536,t=3,p=4$c2FsdHNhbHRzYWx0$aGFzaGhhc2hoYXNoaGFzaA"),
    ("op", "t", "$2b$12$" + "a" * 53),
    ("op", "t", "$argon2id$v=19$m=65536,t=3,p=4$" + "s" * 22 + "$" + "h" * 250),   # Over 255 characters.
    ("op", "t", "$argon2id$v=19$m=65536,t=3,p=4$" + "s" * 22 + "$" + "h" * 43 + "\n"),
])
def test_malformed_calls_are_refused_and_store_nothing(world, operation, digest, hashed):
    w = world
    user, old, _ = with_sessions(w, 1)
    real = token(w, user)
    operation = uuid.uuid4() if operation == "op" else operation
    digest = real if digest == "t" else digest
    hashed = new_hash() if hashed == "ok" else hashed
    state = security_state(w)
    assert reset(w, operation, digest, hashed) == UNAVAILABLE
    assert security_state(w) == state and stored(w, user)[0] == old and tokens(w, user)[real] is None


def test_recovery_admission_allows_two_a_minute_per_account(world):
    w = world
    user, _, _ = with_sessions(w, 1)
    for _ in range(2):
        assert reset(w, uuid.uuid4(), token(w, user), new_hash())[0] == "reset"
    digest, hashed = token(w, user), new_hash()
    outcome, wait, replayed = reset(w, uuid.uuid4(), digest, hashed)
    assert outcome == "too_many_operations" and 1 <= wait <= 60 and replayed is False
    assert stored(w, user)[0] != hashed and tokens(w, user)[digest] is None     # Nothing applied.
    # The limit is per account: another account still resets.
    other, _, _ = with_sessions(w, 1)
    assert reset(w, uuid.uuid4(), token(w, other), new_hash())[0] == "reset"
    # Recovery charges never fill the manual ring that revocation uses.
    assert all(row[2] == [] for row in security_state(w)[1])


def test_concurrent_resets_with_one_token_apply_exactly_once(world):
    w = world
    user, _, _ = with_sessions(w, 1)
    digest = token(w, user)
    results, start = [], threading.Barrier(4)

    def attempt():
        start.wait(10)
        results.append(reset(w, uuid.uuid4(), digest, new_hash()))
    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(r[0] for r in results) == ["invalid_token"] * 3 + ["reset"]
    assert [row[2] for row in security_state(w)[0] if row[1] == user] == ["password_reset"]


def test_only_s_may_call_it(world):
    w = world
    for engine in (w.runtime_engine, w.engine):                           # R, and A (PUBLIC's rights too).
        with engine.connect() as c:
            assert sqlstate(c, "SELECT * FROM kalillac.v1_reset_password(:o,:t,:h)",
                            {"o": uuid.uuid4(), "t": secrets.token_hex(32), "h": new_hash()}) == "42501"


def test_e_gains_exactly_the_reset_columns(world):
    w = world
    with w.admin_engine.connect() as c:
        granted = {tuple(r) for r in c.execute(text(
            "SELECT c.relname,a.attname,x.privilege_type FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid"
            " JOIN pg_namespace n ON n.oid=c.relnamespace, aclexplode(a.attacl) x"
            " WHERE n.nspname='kalillac' AND x.grantee=to_regrole(:e)"), {"e": w.names["executor"]})}
    assert granted == {(table, column, right) for (table, right), columns in v1_schema.EXECUTOR_COLUMN_GRANTS_V3.items()
                       for column in columns}
    assert ("users", "email", "UPDATE") not in granted and ("account_recovery_tokens", "token_hash", "UPDATE") not in granted


def test_downgrade_and_upgrade_restore_the_exact_catalogs(world):
    w = world
    user, _, _ = with_sessions(w, 1)
    hashed = new_hash()
    assert reset(w, uuid.uuid4(), token(w, user), hashed)[0] == "reset"
    migrate(w, PREVIOUS, down=True)
    assert not present(w) and classify(w.owner_factory) == v1_schema.PREPARED
    assert stored(w, user)[0] == hashed                                    # Completed resets stay.
    migrate(w, HEAD)
    assert present(w) and classify(w.owner_factory) == v1_schema.PREPARED


@pytest.mark.parametrize("tamper", [
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('" + SIGNATURE + "')",
    "GRANT EXECUTE ON FUNCTION " + SIGNATURE + " TO PUBLIC",
    "GRANT EXECUTE ON FUNCTION " + SIGNATURE + " TO {app}",
    "ALTER FUNCTION " + SIGNATURE + " SET search_path = public",
    "ALTER FUNCTION " + SIGNATURE + " OWNER TO {owner}",
    "GRANT UPDATE (email) ON kalillac.users TO {executor}",
    "GRANT UPDATE (token_hash) ON kalillac.account_recovery_tokens TO {executor}",
    "REVOKE UPDATE (consumed_at) ON kalillac.account_recovery_tokens FROM {executor}",
    "REVOKE UPDATE (password_hash) ON kalillac.users FROM {executor}",
    # 0019's postcondition still proves 0018's function.
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('kalillac.v1_end_session(uuid,text)')",
])
def test_a_tampered_catalog_is_refused_by_startup_and_by_the_downgrade(world, tamper):
    w = world
    admin_sql(w, tamper.format(**w.names))
    refused(w)
    with pytest.raises(DBAPIError, match="V1_PASSWORD_RESET_POSTCONDITION_INVALID"):
        migrate(w, PREVIOUS, down=True)
    assert present(w)


def test_the_upgrade_refuses_a_preexisting_reset_function(world):
    w = world
    migrate(w, PREVIOUS, down=True)
    with w.migration.begin() as c:
        c.exec_driver_sql("SET ROLE " + w.names["owner"])
        c.exec_driver_sql("CREATE FUNCTION kalillac.v1_reset_password(p uuid) RETURNS integer"
                          " LANGUAGE sql AS $$ SELECT 1 $$")
        c.exec_driver_sql("REVOKE ALL ON FUNCTION kalillac.v1_reset_password(uuid) FROM PUBLIC")
    with pytest.raises(DBAPIError, match="V1_PASSWORD_RESET_PREREQUISITE_INVALID"):
        migrate(w, HEAD)
    with w.migration.begin() as c:
        c.exec_driver_sql("SET ROLE " + w.names["owner"])
        c.exec_driver_sql("DROP FUNCTION kalillac.v1_reset_password(uuid)")
    migrate(w, HEAD)
    assert classify(w.owner_factory) == v1_schema.PREPARED
