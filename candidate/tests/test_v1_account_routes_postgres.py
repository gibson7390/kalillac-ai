"""D3, the remaining V1 account routes through real HTTP on disposable PostgreSQL 16: sign-out, revoking
other or all sessions, password change, the customer's own deletion request and recovery confirmation.

The fixture is test_v1_login_postgres's: the real account router in V1 mode, V1-01's read path as the
runtime role R and the authorities as S with its own SCRAM credential (in the disposable database only).
A test-only statement-level trigger records the session login of every statement that writes the
account tables, so each test proves R wrote nothing: every write is S's, through an authority. Every
password and token is synthetic."""
from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from kalillac_accounts.recovery_router import build_recovery_router
from kalillac_accounts.recovery_tokens import new_recovery_token
from kalillac_accounts.router import build_account_router
from test_v1_login_postgres import COOKIE, PASSWORD, SETTINGS, customer, http, login, me, rows, signed_in, world  # noqa: F401
from test_v1_revocation_postgres import admin_sql

NEW_PASSWORD = "synthetic replacement passphrase"
TABLES = ("users", "account_sessions", "account_recovery_tokens", "account_security_events")


@pytest.fixture
def writes(world):  # noqa: F811 (the imported fixture)
    """Records (session login, table, operation) for every writing statement on the account tables."""
    w = world
    admin_sql(w, "CREATE TABLE public.d3_writes(who text NOT NULL, tbl text NOT NULL, op text NOT NULL)",
              "GRANT INSERT ON public.d3_writes TO PUBLIC",
              "CREATE FUNCTION public.d3_record() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS"
              " $$BEGIN INSERT INTO public.d3_writes VALUES (session_user, TG_TABLE_NAME, TG_OP); RETURN NULL;"
              " END$$",
              *[f"CREATE TRIGGER d3_record AFTER INSERT OR UPDATE OR DELETE ON kalillac.{t}"
                " FOR EACH STATEMENT EXECUTE FUNCTION public.d3_record()" for t in TABLES])

    def recorded():
        with w.admin_engine.connect() as c:
            return sorted(set(map(tuple, c.execute(text("SELECT who, tbl, op FROM public.d3_writes")))))

    def clear():
        admin_sql(w, "TRUNCATE public.d3_writes")

    w.recorded, w.clear_writes = recorded, clear
    yield w
    admin_sql(w, *[f"DROP TRIGGER d3_record ON kalillac.{t}" for t in TABLES],
              "DROP FUNCTION public.d3_record()", "DROP TABLE public.d3_writes")


def only_s_wrote(w):
    who = {row[0] for row in w.recorded()}
    assert who <= {w.names["service"]}, w.recorded()
    assert w.names["app"] not in who


def cookie(token):
    return {"Cookie": f"{COOKIE}={token}"}


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def sessions(w, user):
    """{token digest: revoked?} for the account."""
    return {r[1]: r[2] is not None for r in rows(w, user)}


def account(w, user):
    with w.admin_engine.connect() as c:
        return c.execute(text("SELECT password_hash, deletion_requested_at, sessions_valid_after"
                              " FROM kalillac.users WHERE id=:u"), {"u": user}).one()


def events(w, user):
    with w.admin_engine.connect() as c:
        return [r[0] for r in c.execute(text("SELECT event_type FROM kalillac.account_security_events"
                                             " WHERE user_id=:u ORDER BY occurred_at"), {"u": user})]


def post(w, path, token, **kwargs):
    with http(w) as client:
        return client.post(path, headers=cookie(token) if token else {}, **kwargs)


def test_the_write_recorder_sees_a_write_by_the_runtime_role(writes):
    """The instrument itself: the legacy (V1-off) sign-out revokes as R, and that is recorded."""
    w = writes
    user, email = customer(w)
    token = signed_in(w, email)
    w.clear_writes()
    api = FastAPI()
    api.include_router(build_account_router(lambda: w.runtime_factory, SETTINGS, v1_security=None))
    with TestClient(api, base_url="https://testserver") as client:
        assert client.post("/api/account/logout", headers=cookie(token)).status_code == 200
    assert (w.names["app"], "account_sessions", "UPDATE") in w.recorded()
    with pytest.raises(AssertionError):
        only_s_wrote(w)


# --- sign-out -----------------------------------------------------------------------------------------

def test_logout_ends_exactly_the_presented_session_and_r_writes_nothing(writes):
    w = writes
    user, email = customer(w)
    first, second = signed_in(w, email), signed_in(w, email)
    w.clear_writes()
    response = post(w, "/api/account/logout", first)
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert sessions(w, user) == {digest(first): True, digest(second): False}
    assert me(w, first) == 401 and me(w, second) == 200
    only_s_wrote(w)
    assert events(w, user) == []                                      # Sign-out is not an audited operation.
    again = post(w, "/api/account/logout", first)                     # Idempotent.
    assert again.status_code == 200 and sessions(w, user)[digest(second)] is False


def test_logout_ends_the_session_of_a_quarantined_account_too(writes):
    w = writes
    user, email = customer(w)
    token = signed_in(w, email)
    with w.admin_engine.begin() as c:                                 # The operator's quarantine, as P.
        c.exec_driver_sql("SET LOCAL ROLE " + w.names["operator"])
        assert c.execute(text("SELECT outcome FROM kalillac.v1_quarantine(:o,:u)"),
                         {"o": uuid.uuid4(), "u": user}).scalar() == "quarantined"
    w.clear_writes()
    assert post(w, "/api/account/logout", token).status_code == 200
    only_s_wrote(w)


def test_logout_without_an_authority_is_503_and_the_session_stays_for_a_retry(writes):
    w = writes
    user, email = customer(w)
    token = signed_in(w, email)
    w.clear_writes()
    with http(w, authority=None) as client:
        response = client.post("/api/account/logout", headers=cookie(token))
    assert (response.status_code, response.json()) == (503, {"error": "account_unavailable"})
    assert "set-cookie" not in response.headers
    assert sessions(w, user) == {digest(token): False} and w.recorded() == []
    assert post(w, "/api/account/logout", token).status_code == 200 and me(w, token) == 401


# --- revoking sessions --------------------------------------------------------------------------------

def test_revoke_others_keeps_only_the_presenting_session(writes):
    w = writes
    user, email = customer(w)
    keep, other, third = (signed_in(w, email) for _ in range(3))
    epoch = account(w, user)[2]
    w.clear_writes()
    response = post(w, "/api/account/sessions/revoke-others", keep, json={})
    assert (response.status_code, response.json()) == (200, {"ok": True}) and "set-cookie" not in response.headers
    assert sessions(w, user) == {digest(keep): False, digest(other): True, digest(third): True}
    assert me(w, keep) == 200 and me(w, other) == me(w, third) == 401
    assert account(w, user)[2] == epoch and events(w, user) == ["session_revoke_others"]
    only_s_wrote(w)


def test_revoke_all_ends_every_session_advances_the_epoch_and_clears_the_cookie(writes):
    w = writes
    user, email = customer(w)
    first, second = signed_in(w, email), signed_in(w, email)
    epoch = account(w, user)[2]
    w.clear_writes()
    response = post(w, "/api/account/sessions/revoke-all", first, json={})
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert set(sessions(w, user).values()) == {True} and me(w, first) == me(w, second) == 401
    assert epoch is None or account(w, user)[2] > epoch
    assert events(w, user) == ["session_revoke_all"]
    only_s_wrote(w)
    with http(w) as client:                                           # The customer signs in again.
        assert login(client, email).status_code == 200


def test_a_revoked_session_cannot_revoke_anything(writes):
    w = writes
    user, email = customer(w)
    first, second = signed_in(w, email), signed_in(w, email)
    assert post(w, "/api/account/sessions/revoke-others", second, json={}).status_code == 200
    w.clear_writes()
    response = post(w, "/api/account/sessions/revoke-all", first, json={})
    assert (response.status_code, response.json()) == (401, {"error": "not_authenticated"})
    assert sessions(w, user) == {digest(first): True, digest(second): False} and w.recorded() == []


# --- password change ----------------------------------------------------------------------------------

def test_password_change_replaces_the_hash_keeps_this_session_and_ends_the_others(writes):
    w = writes
    user, email = customer(w)
    keep, other = signed_in(w, email), signed_in(w, email)
    stored, _, epoch = account(w, user)
    w.clear_writes()
    response = post(w, "/api/account/password", keep,
                    json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert (response.status_code, response.json()) == (200, {"ok": True}) and "set-cookie" not in response.headers
    changed, _, after = account(w, user)
    assert changed != stored and changed.startswith("$argon2id$") and after == epoch
    assert sessions(w, user) == {digest(keep): False, digest(other): True}
    assert me(w, keep) == 200 and me(w, other) == 401
    assert events(w, user) == ["password_change"]
    only_s_wrote(w)
    with http(w) as client:
        assert login(client, email).status_code == 401                  # The old password no longer works.
        assert login(client, email, NEW_PASSWORD).status_code == 200


def test_a_wrong_current_password_changes_nothing(writes):
    w = writes
    user, email = customer(w)
    token = signed_in(w, email)
    before = (account(w, user), sessions(w, user))
    w.clear_writes()
    response = post(w, "/api/account/password", token,
                    json={"current_password": "not the password", "new_password": NEW_PASSWORD})
    assert (response.status_code, response.json()) == (403, {"error": "invalid_current_password"})
    assert (account(w, user), sessions(w, user)) == before and w.recorded() == [] and events(w, user) == []


def test_a_change_racing_another_change_cannot_apply_a_stale_proof(writes):
    """The first change replaces the hash; a second request verified against the old hash before that is
    refused by the authority (the stored hash it presents is no longer current): 401, nothing changed."""
    w = writes
    user, email = customer(w)
    token = signed_in(w, email)
    stored = account(w, user)[0]
    assert post(w, "/api/account/password", token,
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}).status_code == 200
    changed = account(w, user)[0]
    # Replay the stale proof directly through the authority, as an overlapping request would present it.
    with w.admin_engine.connect() as c:
        session_id = c.scalar(text("SELECT id FROM kalillac.account_sessions WHERE token_hash=:t"),
                              {"t": digest(token)})
    from kalillac_accounts.passwords import hash_password
    result = w.authority.change_password(operation_id=uuid.uuid4(), session_id=session_id, token_hash=digest(token),
                                         verified_password_hash=stored, new_password_hash=hash_password("third one!!"))
    assert result.outcome == "not_authenticated" and account(w, user)[0] == changed


# --- the customer's own deletion request --------------------------------------------------------------

def test_the_customers_deletion_request_marks_the_account_and_refuses_every_later_sign_in(writes):
    w = writes
    user, email = customer(w)
    token, other = signed_in(w, email), signed_in(w, email)
    stranger, stranger_email = customer(w)
    theirs = signed_in(w, stranger_email)
    w.clear_writes()
    response = post(w, "/api/account/deletion-request", token, json={"current_password": PASSWORD})
    assert (response.status_code, response.json()) == (200, {"ok": True})
    assert "Max-Age=0" in response.headers["set-cookie"]
    _, marked, _ = account(w, user)
    assert marked is not None and set(sessions(w, user).values()) == {True}
    assert me(w, token) == me(w, other) == 401 and events(w, user) == ["account_deletion_request"]
    only_s_wrote(w)
    with http(w) as client:
        response = login(client, email)
        assert (response.status_code, response.json()) == (401, {"error": "invalid_credentials"})
    assert me(w, theirs) == 200 and account(w, stranger)[1] is None   # Another account is untouched.


def test_a_wrong_password_cannot_request_deletion(writes):
    w = writes
    user, email = customer(w)
    token = signed_in(w, email)
    w.clear_writes()
    response = post(w, "/api/account/deletion-request", token, json={"current_password": "wrong password!"})
    assert (response.status_code, response.json()) == (403, {"error": "invalid_current_password"})
    assert account(w, user)[1] is None and me(w, token) == 200 and w.recorded() == []


# --- recovery confirmation ----------------------------------------------------------------------------

def recovery_client(w, gateway, authority="real"):
    import recovery_test_support as support
    api = FastAPI()
    api.include_router(build_account_router(lambda: w.runtime_factory, SETTINGS, v1_security=w.security,
                                            v1_authority=w.authority))
    api.include_router(build_recovery_router(
        session_factory=lambda: w.runtime_factory, gateway=gateway, settings=SETTINGS, environ=support.ENV,
        v1_security=w.security, v1_authority=w.authority if authority == "real" else authority))
    return TestClient(api, base_url="https://testserver")


def reset_link(w, user):
    """A reset token as the existing request path issues it (fixture: inserted directly)."""
    token = new_recovery_token()
    admin_sql(w, "INSERT INTO kalillac.account_recovery_tokens(id,user_id,purpose,token_hash,created_at,expires_at)"
                 " VALUES (gen_random_uuid(),:u,'password_reset',:h,clock_timestamp(),"
                 "clock_timestamp() + interval '30 minutes')", u=user, h=hashlib.sha256(token.encode()).hexdigest())
    return token


def test_recovery_confirmation_resets_through_the_authority_and_ends_every_session(writes):
    import recovery_test_support as support
    w = writes
    user, email = customer(w)
    first, second = signed_in(w, email), signed_in(w, email)
    link = reset_link(w, user)
    stored, _, epoch = account(w, user)
    gateway = support.FakeEmail()
    w.clear_writes()
    with recovery_client(w, gateway) as client:
        response = client.post("/api/account/recovery/confirm", json={"token": link, "password": NEW_PASSWORD})
    assert (response.status_code, response.json()) == (200, {"ok": True})
    changed, _, after = account(w, user)
    assert changed != stored and (epoch is None or after > epoch)
    assert set(sessions(w, user).values()) == {True} and me(w, first) == me(w, second) == 401
    assert events(w, user) == ["password_reset"] and gateway.changed == [email]
    only_s_wrote(w)
    with recovery_client(w, gateway) as client:                       # The link is single use.
        again = client.post("/api/account/recovery/confirm", json={"token": link, "password": "yet another pass"})
        assert (again.status_code, again.json()) == (400, {"error": "invalid_or_expired_token"})
    with http(w) as client:
        assert login(client, email).status_code == 401 and login(client, email, NEW_PASSWORD).status_code == 200


def test_recovery_without_an_authority_is_503_and_writes_nothing(writes):
    import recovery_test_support as support
    w = writes
    user, email = customer(w)
    token = signed_in(w, email)
    link = reset_link(w, user)
    before = (account(w, user), sessions(w, user))
    w.clear_writes()
    with recovery_client(w, support.FakeEmail(), authority=None) as client:
        response = client.post("/api/account/recovery/confirm", json={"token": link, "password": NEW_PASSWORD})
    assert (response.status_code, response.json()) == (503, {"error": "recovery_unavailable"})
    assert (account(w, user), sessions(w, user)) == before and w.recorded() == [] and me(w, token) == 200
