"""0024, reset-token issuance and the credential read path through S, on real disposable PostgreSQL 16.

The function tests call both functions as the NOLOGIN service role S (SET LOCAL ROLE from the disposable
superuser, which also seeds fixtures, holds locks and tampers with the catalog). The HTTP tests use
test_v1_login_postgres's fixture (S with its own SCRAM login, in the disposable database only) and then
take the runtime role R's read of users.password_hash away, as E1 will: sign-in, password change, the
customer's deletion request and recovery all still work, R cannot read a hash, and a test-only trigger
proves R writes nothing. Every password and token is synthetic.
"""
from __future__ import annotations

import hashlib
import importlib.util
import secrets
import threading
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import kalillac_accounts.v1_schema as v1_schema
from test_v1_issuance_postgres import account
from test_v1_password_reset_postgres import new_hash, reset
from test_v1_revocation_postgres import admin_sql
from test_v1_schema_postgres import classify, migrate, refused, sqlstate, version
from test_v1_session_end_postgres import head_world

HEAD = "0024_v1_recovery_credential"
PREVIOUS = "0023_v1_deletion_request"
ISSUE = "kalillac.v1_issue_reset_token(uuid,text,text)"
CREDENTIAL = "kalillac.v1_credential(text)"


@pytest.fixture
def db_world(monkeypatch):
    with head_world(monkeypatch) as w:
        # 0024's own catalog exactly: later revisions (0025) test theirs.
        migrate(w, HEAD, down=True)
        yield w
        assert w.admin_engine.pool.checkedout() == 0 and w.runtime_engine.pool.checkedout() == 0


def email_of(w, user):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT email FROM kalillac.users WHERE id=:u"), {"u": user})


def issue_reset(w, email, digest=None, token_id=None, role="service"):
    """Call the issuance as S (or another role, to show it is refused); returns (outcome, digest)."""
    digest = digest or secrets.token_hex(32)
    with w.admin_engine.begin() as c:
        c.exec_driver_sql("SET LOCAL ROLE " + w.names[role])
        outcome = c.execute(text("SELECT outcome FROM kalillac.v1_issue_reset_token(:i,:e,:t)"),
                            {"i": token_id or uuid.uuid4(), "e": email, "t": digest}).scalar_one()
    return outcome, digest


def credential(w, email, role="service"):
    with w.admin_engine.begin() as c:
        c.exec_driver_sql("SET LOCAL ROLE " + w.names[role])
        return [tuple(r) for r in c.execute(text("SELECT * FROM kalillac.v1_credential(:e)"), {"e": email})]


def open_tokens(w, user):
    with w.admin_engine.connect() as c:
        return [tuple(r) for r in c.execute(text(
            "SELECT token_hash, purpose, expires_at - created_at FROM kalillac.account_recovery_tokens"
            " WHERE user_id=:u AND consumed_at IS NULL ORDER BY created_at"), {"u": user})]


def all_tokens(w):
    with w.admin_engine.connect() as c:
        return sorted(tuple(map(str, r)) for r in c.execute(text(
            "SELECT id, user_id, token_hash, consumed_at FROM kalillac.account_recovery_tokens")))


def set_user(w, user, assignment):
    admin_sql(w, f"UPDATE kalillac.users SET {assignment} WHERE id=:u", u=user)


def acl(w, signature):
    with w.admin_engine.connect() as c:
        return sorted(c.scalar(text("SELECT array_agg(pg_get_userbyid(x.grantee)) FROM pg_proc p,"
                                    " aclexplode(p.proacl) x WHERE p.oid = to_regprocedure(:f)"), {"f": signature}))


# --- the catalog ---------------------------------------------------------------------------------------

def test_the_head_classifies_with_both_functions_owned_by_e_and_executable_by_s_only(db_world):
    w = db_world
    assert version(w) == HEAD and classify(w.owner_factory) == v1_schema.PREPARED
    for signature in (ISSUE, CREDENTIAL):
        assert acl(w, signature) == sorted([w.names["executor"], w.names["service"]])
        with w.admin_engine.connect() as c:
            assert c.scalar(text("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE oid = to_regprocedure(:f)"),
                            {"f": signature}) == w.names["executor"]
    with w.admin_engine.connect() as c:
        e = w.names["executor"]
        assert c.scalar(text("SELECT has_column_privilege(:e,'kalillac.users','email','SELECT')"), {"e": e})
        for column in ("id", "user_id", "purpose", "token_hash", "created_at", "expires_at"):
            assert c.scalar(text("SELECT has_column_privilege(:e,'kalillac.account_recovery_tokens',:c,'INSERT')"),
                            {"e": e, "c": column})
        assert not c.scalar(text("SELECT has_column_privilege(:e,'kalillac.account_recovery_tokens','consumed_at',"
                                 "'INSERT')"), {"e": e})
        assert not c.scalar(text("SELECT has_column_privilege(:e,'kalillac.users','email','UPDATE')"), {"e": e})


@pytest.mark.parametrize("role", ["app", "registration", "operator", "owner"])
def test_no_role_but_s_may_execute_either_function(db_world, role):
    w = db_world
    user, _ = account(w)
    with w.admin_engine.connect() as c:
        c.exec_driver_sql("SET ROLE " + w.names[role])
        assert sqlstate(c, "SELECT * FROM kalillac.v1_credential(:e)", {"e": "x@example.test"}) == "42501"
        assert sqlstate(c, "SELECT * FROM kalillac.v1_issue_reset_token(:i,:e,:t)",
                        {"i": uuid.uuid4(), "e": email_of(w, user), "t": secrets.token_hex(32)}) == "42501"
        c.exec_driver_sql("RESET ROLE")
    assert open_tokens(w, user) == []


# --- issuance ------------------------------------------------------------------------------------------

def test_an_eligible_account_gets_one_token_and_its_open_tokens_are_consumed(db_world):
    w = db_world
    user, _ = account(w)
    email = email_of(w, user)
    first = issue_reset(w, email)
    assert first[0] == "issued"
    (only,) = open_tokens(w, user)
    assert only[0] == first[1] and only[1] == "password_reset" and only[2].total_seconds() == 30 * 60
    second = issue_reset(w, email)
    assert second[0] == "issued" and [t[0] for t in open_tokens(w, user)] == [second[1]]
    # The consumed token no longer resets; the new one does, once.
    assert reset(w, uuid.uuid4(), first[1], new_hash())[0] == "invalid_token"
    assert reset(w, uuid.uuid4(), second[1], new_hash())[0] == "reset"
    assert reset(w, uuid.uuid4(), second[1], new_hash())[0] == "invalid_token"


@pytest.mark.parametrize("case", ["unknown", "other_case", "inactive", "unverified", "quarantined",
                                  "deletion_requested"])
def test_an_account_that_may_not_reset_gets_nothing_and_its_open_tokens_stay(db_world, case):
    w = db_world
    user, _ = account(w)
    email = email_of(w, user)
    assert issue_reset(w, email)[0] == "issued"
    before = all_tokens(w)
    target = {"unknown": "nobody-" + email, "other_case": email.upper()}.get(case, email)
    if case in ("inactive", "unverified", "quarantined", "deletion_requested"):
        admin_sql(w, "ALTER TABLE kalillac.users DISABLE TRIGGER USER")      # Fixture only: set the state.
        try:
            set_user(w, user, {"inactive": "is_active = false", "unverified": "email_verified_at = NULL",
                               "quarantined": "quarantined_at = clock_timestamp()",
                               "deletion_requested": "deletion_requested_at = clock_timestamp()"}[case])
        finally:
            admin_sql(w, "ALTER TABLE kalillac.users ENABLE TRIGGER USER")
    assert issue_reset(w, target)[0] == "not_issued"
    assert all_tokens(w) == before


@pytest.mark.parametrize("field, value", [("email", None), ("email", "a" * 321), ("digest", "F" * 64),
                                          ("digest", "f" * 63), ("token_id", None)])
def test_malformed_issuance_is_refused_and_writes_nothing(db_world, field, value):
    w = db_world
    user, _ = account(w)
    arguments = {"i": uuid.uuid4(), "e": email_of(w, user), "t": secrets.token_hex(32)}
    arguments[{"email": "e", "digest": "t", "token_id": "i"}[field]] = value
    with w.admin_engine.begin() as c:
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["service"])
        assert c.execute(text("SELECT outcome FROM kalillac.v1_issue_reset_token(:i,:e,:t)"),
                         arguments).scalar_one() == "security_unavailable"
    assert all_tokens(w) == []


@pytest.mark.parametrize("taken", ["token_id", "digest"])
def test_a_taken_id_or_digest_issues_nothing_and_consumes_nothing(db_world, taken):
    w = db_world
    user, _ = account(w)
    other, _ = account(w)
    token_id, digest = uuid.uuid4(), secrets.token_hex(32)
    assert issue_reset(w, email_of(w, other), digest=digest, token_id=token_id)[0] == "issued"
    assert issue_reset(w, email_of(w, user))[0] == "issued"
    before = all_tokens(w)
    clash = {"token_id": {"token_id": token_id}, "digest": {"digest": digest}}[taken]
    assert issue_reset(w, email_of(w, user), **clash)[0] == "security_unavailable"
    assert all_tokens(w) == before                     # The user's open token was not consumed either.


def test_concurrent_issuance_for_one_account_leaves_exactly_one_open_token(db_world):
    w = db_world
    user, _ = account(w)
    email = email_of(w, user)
    results, start = [], threading.Barrier(4)

    def run():
        start.wait(10)
        results.append(issue_reset(w, email))
    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(r[0] for r in results) == ["issued"] * 4
    (only,) = open_tokens(w, user)
    assert only[0] in {r[1] for r in results}


def test_a_held_account_lock_times_out_issuance_and_changes_nothing(db_world):
    w = db_world
    user, _ = account(w)
    holder = w.admin_engine.connect()
    try:
        holder.begin()
        holder.execute(text("SELECT 1 FROM kalillac.users WHERE id=:u FOR UPDATE"), {"u": user})
        with w.admin_engine.begin() as c:
            c.exec_driver_sql("SET LOCAL lock_timeout = '200ms'")
            c.exec_driver_sql("SET LOCAL ROLE " + w.names["service"])
            assert sqlstate(c, "SELECT * FROM kalillac.v1_issue_reset_token(:i,:e,:t)",
                            {"i": uuid.uuid4(), "e": email_of(w, user), "t": secrets.token_hex(32)}) == "55P03"
        holder.rollback()
    finally:
        holder.close()
    assert all_tokens(w) == []


# --- the credential read ---------------------------------------------------------------------------------

def test_the_credential_read_returns_exactly_one_accounts_id_and_hash(db_world):
    w = db_world
    user, stored = account(w)
    other, _ = account(w)
    email = email_of(w, user)
    assert credential(w, email) == [(user, stored)]
    assert credential(w, email.upper()) == [] and credential(w, "nobody@example.test") == []
    assert credential(w, "a" * 321) == []
    with w.admin_engine.begin() as c:
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["service"])
        assert c.execute(text("SELECT * FROM kalillac.v1_credential(NULL)")).all() == []
    # Read only: nothing is locked; a held account lock does not block it.
    holder = w.admin_engine.connect()
    try:
        holder.begin()
        holder.execute(text("SELECT 1 FROM kalillac.users WHERE id=:u FOR UPDATE"), {"u": user})
        assert credential(w, email) == [(user, stored)]
        holder.rollback()
    finally:
        holder.close()


# --- migration -------------------------------------------------------------------------------------------

def _state(w):
    with w.admin_engine.connect() as c:
        return (version(w),
                c.scalar(text("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                              " WHERE n.nspname = 'kalillac' AND p.proname IN ('v1_issue_reset_token','v1_credential')")),
                c.scalar(text("SELECT has_column_privilege(:e,'kalillac.users','email','SELECT')"),
                         {"e": w.names["executor"]}),
                c.scalar(text("SELECT has_any_column_privilege(:e,'kalillac.account_recovery_tokens','INSERT')"),
                         {"e": w.names["executor"]}),
                c.scalar(text("SELECT md5(prosrc) FROM pg_proc WHERE oid ="
                              " to_regprocedure('kalillac.registration_user_identity_guard()')")))


def test_downgrade_restores_0023_exactly_keeps_issued_tokens_and_upgrades_again(db_world):
    w = db_world
    user, _ = account(w)
    at_head = _state(w)
    outcome, digest = issue_reset(w, email_of(w, user))
    assert outcome == "issued"
    migrate(w, PREVIOUS, down=True)
    below = _state(w)
    assert below[:4] == (PREVIOUS, 0, False, False) and below[4] == at_head[4]
    assert classify(w.owner_factory) == v1_schema.PREPARED
    assert [t[0] for t in open_tokens(w, user)] == [digest]                 # Still usable by reset.
    migrate(w, HEAD)
    assert _state(w) == at_head and classify(w.owner_factory) == v1_schema.PREPARED
    assert reset(w, uuid.uuid4(), digest, new_hash())[0] == "reset"


@pytest.mark.parametrize("tamper", [
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('" + ISSUE + "')",
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid = to_regprocedure('" + CREDENTIAL + "')",
    "GRANT EXECUTE ON FUNCTION " + CREDENTIAL + " TO {app}",
    "GRANT EXECUTE ON FUNCTION " + CREDENTIAL + " TO {operator}",
    "GRANT EXECUTE ON FUNCTION " + ISSUE + " TO PUBLIC",
    "ALTER FUNCTION " + CREDENTIAL + " SET search_path = public",
    "ALTER FUNCTION " + ISSUE + " OWNER TO {owner}",
    "REVOKE SELECT (email) ON kalillac.users FROM {executor}",
    "GRANT INSERT (consumed_at) ON kalillac.account_recovery_tokens TO {executor}",
    "GRANT UPDATE (email) ON kalillac.users TO {executor}",
    # 0024's postcondition still proves 0023's functions.
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid ="
    " to_regprocedure('kalillac.v1_request_own_deletion(uuid,uuid,text,text)')",
])
def test_a_tampered_catalog_is_refused_by_startup_and_by_the_downgrade(db_world, tamper):
    w = db_world
    admin_sql(w, tamper.format(**w.names))
    refused(w)
    with pytest.raises(DBAPIError, match="V1_RECOVERY_CREDENTIAL_POSTCONDITION_INVALID"):
        migrate(w, PREVIOUS, down=True)
    assert version(w) == HEAD


@pytest.mark.parametrize("fault", [
    # 0023's catalog is not exact: the preflight refuses before any change.
    "UPDATE pg_catalog.pg_proc SET prosrc = prosrc || ' ' WHERE oid ="
    " to_regprocedure('kalillac.v1_request_deletion(uuid,uuid)')",
    "GRANT SELECT (email) ON kalillac.users TO {executor}",
    # A function of the same name already exists.
    "CREATE FUNCTION kalillac.v1_credential(p integer) RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$",
])
def test_a_refused_upgrade_leaves_nothing_changed(db_world, fault):
    w = db_world
    migrate(w, PREVIOUS, down=True)
    admin_sql(w, fault.format(**w.names))
    before = _state(w)
    with pytest.raises(DBAPIError, match="V1_RECOVERY_CREDENTIAL_PREREQUISITE_INVALID"):
        migrate(w, HEAD)
    assert _state(w) == before


def test_a_failure_at_the_last_statement_rolls_the_whole_upgrade_back(db_world):
    w = db_world
    migrate(w, PREVIOUS, down=True)
    before = _state(w)
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0024_v1_recovery_issuance_and_credential.py"
    spec = importlib.util.spec_from_file_location("v1_0024_for_rollback", path)
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
    assert _state(w)[:4] == (HEAD, 2, True, True)


# --- HTTP under the final credential grants (R cannot read a password hash) ------------------------------

from test_v1_account_routes_postgres import (account as user_row, digest, events, only_s_wrote,  # noqa: E402,F401
                                             post, sessions, writes)
from test_v1_login_postgres import (COOKIE, PASSWORD, SETTINGS, customer, http, login, me,  # noqa: E402,F401
                                    signed_in, world)

NEW_PASSWORD = "synthetic replacement passphrase"


@pytest.fixture
def final(world):  # noqa: F811 (the imported fixture)
    """Take R's read of users.password_hash away, as E1 will, keeping every other column R reads."""
    w = world
    runtime = w.names["app"]
    with w.admin_engine.connect() as c:
        table_level = c.scalar(text("SELECT has_table_privilege(:r,'kalillac.users','SELECT')"), {"r": runtime})
        columns = [r[0] for r in c.execute(text(
            "SELECT attname FROM pg_attribute WHERE attrelid='kalillac.users'::regclass AND attnum>0"
            " AND NOT attisdropped AND attname<>'password_hash' AND has_column_privilege(:r,attrelid,attnum,'SELECT')"),
            {"r": runtime})]
    if table_level:
        admin_sql(w, f"REVOKE SELECT ON kalillac.users FROM {runtime}",
                  f"GRANT SELECT ({', '.join(columns)}) ON kalillac.users TO {runtime}")
    else:
        admin_sql(w, f"REVOKE SELECT (password_hash) ON kalillac.users FROM {runtime}")
    with w.runtime_engine.connect() as c:
        assert sqlstate(c, "SELECT password_hash FROM kalillac.users LIMIT 1") == "42501"
        assert sqlstate(c, "SELECT id, email, created_at FROM kalillac.users LIMIT 1") == "accepted"
    yield w


@pytest.fixture
def final_writes(final, writes):  # noqa: F811 (the imported fixture)
    """The write recorder of test_v1_account_routes_postgres, on the final-grants world."""
    yield writes


def test_sign_in_password_change_and_deletion_work_without_rs_hash_read(final_writes):
    w = final_writes
    user, email = customer(w)
    w.clear_writes()
    with http(w) as client:
        response = login(client, email)
        assert response.status_code == 200 and response.json()["user"]["email"] == email
        assert client.get("/api/account/me").status_code == 200
        assert login(client, email, "not the password").status_code == 401
    keep, other = response.cookies.get(COOKIE), signed_in(w, email)
    changed = post(w, "/api/account/password", keep, json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert (changed.status_code, changed.json()) == (200, {"ok": True})
    assert sessions(w, user) == {digest(keep): False, digest(other): True}
    with http(w) as client:
        assert login(client, email).status_code == 401 and login(client, email, NEW_PASSWORD).status_code == 200
    deleted = post(w, "/api/account/deletion-request", keep, json={"current_password": NEW_PASSWORD})
    assert deleted.status_code == 200 and user_row(w, user)[1] is not None
    assert events(w, user) == ["password_change", "account_deletion_request"]
    only_s_wrote(w)
    with http(w) as client:
        assert login(client, email, NEW_PASSWORD).status_code == 401


def recovery_app(w, gateway):
    import recovery_test_support as support
    from kalillac_accounts.recovery_router import build_recovery_router
    from kalillac_accounts.router import build_account_router
    api = FastAPI()
    api.include_router(build_account_router(lambda: w.runtime_factory, SETTINGS, v1_security=w.security,
                                            v1_authority=w.authority))
    api.include_router(build_recovery_router(
        session_factory=lambda: w.runtime_factory, gateway=gateway, settings=SETTINGS, environ=support.ENV,
        v1_security=w.security, v1_authority=w.authority))
    return TestClient(api, base_url="https://testserver")


def test_recovery_request_and_confirmation_end_to_end_without_rs_hash_read_or_any_r_write(final_writes):
    import recovery_test_support as support
    w = final_writes
    user, email = customer(w)
    first, second = signed_in(w, email), signed_in(w, email)
    gateway = support.FakeEmail()
    w.clear_writes()
    with recovery_app(w, gateway) as client:
        for _ in range(2):                                             # The second link replaces the first.
            response = client.post("/api/account/recovery/request", json={"email": email.upper()},
                                   headers={"cf-connecting-ip": "192.0.2.10"})
            assert (response.status_code, response.json()) == (202, {"ok": True})
        assert [to for to, _ in gateway.reset] == [email, email]
        old, new = (link.split("#token=", 1)[1] for _, link in gateway.reset)
        assert client.post("/api/account/recovery/confirm",
                           json={"token": old, "password": NEW_PASSWORD}).status_code == 400
        confirmed = client.post("/api/account/recovery/confirm", json={"token": new, "password": NEW_PASSWORD})
        assert (confirmed.status_code, confirmed.json()) == (200, {"ok": True})
        unknown = client.post("/api/account/recovery/request", json={"email": "nobody@example.test"},
                              headers={"cf-connecting-ip": "192.0.2.11"})
        assert unknown.status_code == 202 and len(gateway.reset) == 2          # Same answer, nothing sent.
    assert set(sessions(w, user).values()) == {True} and me(w, first) == me(w, second) == 401
    assert gateway.changed == [email] and events(w, user) == ["password_reset"]
    only_s_wrote(w)
    with http(w) as client:
        assert login(client, email).status_code == 401 and login(client, email, NEW_PASSWORD).status_code == 200
    with w.admin_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM kalillac.account_recovery_tokens WHERE user_id=:u"),
                        {"u": user}) == 2
    assert hashlib.sha256(new.encode()).hexdigest() != hashlib.sha256(old.encode()).hexdigest()


def test_without_the_credential_path_v1_sign_in_is_503_never_a_read_as_r(final):
    w = final
    _user, email = customer(w)
    admin_sql(w, f"REVOKE EXECUTE ON FUNCTION {CREDENTIAL} FROM {w.names['service']}")
    try:
        with http(w) as client:
            response = login(client, email)
            assert (response.status_code, response.json()) == (503, {"error": "account_unavailable"})
            assert "set-cookie" not in response.headers
    finally:
        admin_sql(w, f"GRANT EXECUTE ON FUNCTION {CREDENTIAL} TO {w.names['service']}")
    assert signed_in(w, email)
