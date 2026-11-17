"""E3, the synthetic account lifecycle through the real routes under the final grants (V1-02E).

On real disposable PostgreSQL 16: the operator transition (E2) activates V1 first, so the runtime login R
holds only E1a's rights; then one account goes through the whole V1 lifecycle over HTTP, with the real
account, registration and recovery routers behind the unsafe-request guard, S's authority over its own
login, the registration authority A, and the operator tool through P:

register (email-first; ownership proven by the emailed link) → sign in twice → revoke others → change
password → sign out → recovery request and reset → sign in twice → revoke all → operator suspend (every
session refused, sign-in refused) → reinstate → sign in → the customer's deletion request (sign-in refused).

After every step: sessions, the epoch and the audit events; and a statement trigger (installed after
activation, since the classifier rightly refuses unknown triggers) shows R never wrote anything. Every
password and token is synthetic; nothing leaves the disposable database."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from kalillac_accounts.recovery_router import build_recovery_router
from kalillac_accounts.registration_http import RegistrationHTTP
from kalillac_accounts.request_guard import UnsafeRequestGuard
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_accounts.v1_authority import V1Operator, build_v1_authority
from kalillac_accounts.v1_security import load_v1_security
from test_v1_revocation_postgres import admin_sql
from test_v1_transition_postgres import DEPLOYMENT, RELEASE, credential, run as transition, world  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import v1_account_support as support_tool  # noqa: E402

ORIGIN = "https://kalillac.test"
SETTINGS = AccountSettings(cookie_secure=True, session_days=14)
COOKIE = SETTINGS.cookie_name
EMAIL = "lifecycle@example.test"
PASSWORDS = ["first synthetic passphrase", "second synthetic passphrase", "third synthetic passphrase"]
TABLES = ("users", "account_sessions", "account_recovery_tokens", "account_entitlements")


class Dispatch:
    """The registration dispatcher's job, run at once: the authority issues the link after the response."""

    def __init__(self, authority):
        self.authority = authority

    def submit(self, email):
        self.authority.issue_after_response(email)


@pytest.fixture
def live(world, monkeypatch):  # noqa: F811 (the imported fixture)
    w = world
    w.runtime_engine.dispose()                                    # Ingress closed: no open R session.
    assert transition(w, "activate", "--release-sha", RELEASE, "--apply") == 0
    w.environ["KALILLAC_V1_OPERATOR_CREDENTIALS_FILE"] = "operator"
    w.security = load_v1_security(lambda: w.runtime_factory, w.environ)
    w.service = build_v1_authority(w.runtime_engine.url, role=w.names["service"], executor=w.names["executor"],
                                   credential=credential(w, "service"), database_instance_id=w.instance,
                                   deployment_id=DEPLOYMENT)
    # The statement recorder, after activation.
    admin_sql(w, "CREATE TABLE public.e3_writes(who text NOT NULL, tbl text NOT NULL, op text NOT NULL)",
              "GRANT INSERT ON public.e3_writes TO PUBLIC",
              "CREATE FUNCTION public.e3_record() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS"
              " $$BEGIN INSERT INTO public.e3_writes VALUES (session_user, TG_TABLE_NAME, TG_OP); RETURN NULL;"
              " END$$",
              *[f"CREATE TRIGGER e3_record AFTER INSERT OR UPDATE OR DELETE ON kalillac.{t}"
                " FOR EACH STATEMENT EXECUTE FUNCTION public.e3_record()" for t in TABLES])
    yield w
    w.service.dispose()
    admin_sql(w, *[f"DROP TRIGGER e3_record ON kalillac.{t}" for t in TABLES], "DROP FUNCTION public.e3_record()",
              "DROP TABLE public.e3_writes")


def application(w):
    api = FastAPI()
    registration = RegistrationHTTP(w.authority, Dispatch(w.authority), SETTINGS)
    api.include_router(build_account_router(lambda: w.runtime_factory, SETTINGS, registration_http=registration,
                                            v1_security=w.security, v1_authority=w.service))
    import recovery_test_support as recovery
    w.recovery_gateway = getattr(w, "recovery_gateway", None) or recovery.FakeEmail()
    api.include_router(build_recovery_router(session_factory=lambda: w.runtime_factory, gateway=w.recovery_gateway,
                                             settings=SETTINGS, environ=recovery.ENV, v1_security=w.security,
                                             v1_authority=w.service))
    return TestClient(UnsafeRequestGuard(api, origin=ORIGIN), base_url="https://testserver")


def post(w, path, body=None, cookie=None):
    headers = {"Origin": ORIGIN}
    if cookie:
        headers["Cookie"] = f"{COOKIE}={cookie}"
    with application(w) as client:
        return client.post(path, json=body if body is not None else {}, headers=headers)


def me(w, cookie):
    with application(w) as client:
        return client.get("/api/account/me", headers={"Cookie": f"{COOKIE}={cookie}"}).status_code


def sign_in(w, password):
    response = post(w, "/api/account/login", {"email": EMAIL, "password": password})
    assert response.status_code == 200, response.text
    return response.cookies.get(COOKIE)


def account_row(w):
    with w.admin_engine.connect() as c:
        return c.execute(text("SELECT id, sessions_valid_after, quarantined_at, deletion_requested_at"
                              " FROM kalillac.users WHERE email = :e"), {"e": EMAIL}).one()


def events(w, user):
    with w.admin_engine.connect() as c:
        return [r[0] for r in c.execute(text("SELECT event_type FROM kalillac.account_security_events"
                                             " WHERE user_id = :u ORDER BY occurred_at"), {"u": user})]


def live_sessions(w, user):
    with w.admin_engine.connect() as c:
        return c.scalar(text("SELECT count(*) FROM kalillac.account_sessions WHERE user_id = :u AND revoked_at IS NULL"),
                        {"u": user})


def writers(w):
    with w.admin_engine.connect() as c:
        return {r[0] for r in c.execute(text("SELECT DISTINCT who FROM public.e3_writes"))}


def reset_admission(w, user):
    """Clear the account's admission rings as the disposable superuser, in place of waiting out the window
    (the windows themselves are tested with the authorities)."""
    admin_sql(w, "UPDATE kalillac.account_security_admission SET manual_charges = '{}',"
                 " recovery_charges = '{}' WHERE user_id = :u", u=user)
    admin_sql(w, "UPDATE kalillac.account_security_admission_aggregate SET charges = '{}'")


def operator(w, command, user, reason):
    return support_tool.run(
        [command, str(user), "--reason", reason, "--apply"], reader_factory=w.runtime_factory, environ=w.environ,
        operator_factory=lambda: build_v1_authority(
            w.runtime_engine.url, role=w.names["operator"], executor=w.names["executor"],
            credential=credential(w, "operator"), database_instance_id=w.instance, deployment_id=DEPLOYMENT,
            authority_class=V1Operator))


def test_the_whole_v1_lifecycle_through_the_routes_under_the_final_grants(live):
    w = live
    # Register: the link proves ownership; A creates the account, never R.
    assert post(w, "/api/account/register", {"email": EMAIL}).status_code == 202
    token = w.token()
    confirmed = post(w, "/api/account/register/confirm", {"token": token, "password": PASSWORDS[0]})
    assert confirmed.status_code == 200, confirmed.text
    user, epoch, _, _ = account_row(w)

    # Sign in twice; revoke the other session.
    first, second = sign_in(w, PASSWORDS[0]), sign_in(w, PASSWORDS[0])
    assert me(w, first) == me(w, second) == 200
    assert post(w, "/api/account/sessions/revoke-others", cookie=first).status_code == 200
    assert me(w, first) == 200 and me(w, second) == 401 and events(w, user) == ["session_revoke_others"]

    # Change the password; this session stays.
    changed = post(w, "/api/account/password", {"current_password": PASSWORDS[0], "new_password": PASSWORDS[1]},
                   cookie=first)
    assert changed.status_code == 200 and me(w, first) == 200
    assert post(w, "/api/account/login", {"email": EMAIL, "password": PASSWORDS[0]}).status_code == 401

    # Sign out.
    assert post(w, "/api/account/logout", cookie=first).status_code == 200 and me(w, first) == 401

    # Recover: the request issues the token through S; the reset ends every session and moves the epoch.
    third = sign_in(w, PASSWORDS[1])
    assert post(w, "/api/account/recovery/request", {"email": EMAIL}).status_code == 202
    link = w.recovery_gateway.reset[-1][1].split("#token=", 1)[1]
    assert post(w, "/api/account/recovery/confirm", {"token": link, "password": PASSWORDS[2]}).status_code == 200
    moved = account_row(w)[1]
    assert me(w, third) == 401 and moved is not None and (epoch is None or moved > epoch)

    # Sign in twice; revoke all. Three manual operations within seconds exceed the customer's admission
    # budget: the authority answers 429 with Retry-After, and nothing is revoked.
    fourth, fifth = sign_in(w, PASSWORDS[2]), sign_in(w, PASSWORDS[2])
    limited = post(w, "/api/account/sessions/revoke-all", cookie=fourth)
    assert (limited.status_code, limited.json()) == (429, {"error": "too_many_operations"})
    assert int(limited.headers["retry-after"]) >= 1 and me(w, fourth) == me(w, fifth) == 200
    reset_admission(w, user)                                           # Test harness only: skip the wait.
    assert post(w, "/api/account/sessions/revoke-all", cookie=fourth).status_code == 200
    assert me(w, fourth) == me(w, fifth) == 401 and live_sessions(w, user) == 0

    # The operator suspends: every session ends, sign-in is refused; reinstatement reopens sign-in only.
    sixth = sign_in(w, PASSWORDS[2])
    assert operator(w, "suspend", user, "support_review") == 0
    assert me(w, sixth) == 401 and live_sessions(w, user) == 0
    assert post(w, "/api/account/login", {"email": EMAIL, "password": PASSWORDS[2]}).status_code == 401
    assert operator(w, "reinstate", user, "review_cleared") == 0
    assert me(w, sixth) == 401                                          # Sessions stay ended.

    # The customer's own deletion request: marked, every session ends, sign-in refused from then on.
    seventh = sign_in(w, PASSWORDS[2])
    reset_admission(w, user)
    assert post(w, "/api/account/deletion-request", {"current_password": PASSWORDS[2]}, cookie=seventh).status_code == 200
    _, _, quarantined, deletion = account_row(w)
    assert deletion is not None and quarantined is None and live_sessions(w, user) == 0
    assert post(w, "/api/account/login", {"email": EMAIL, "password": PASSWORDS[2]}).status_code == 401

    assert events(w, user) == ["session_revoke_others", "password_change", "password_reset", "session_revoke_all",
                               "account_quarantine", "account_reinstate", "account_deletion_request"]
    # Every write was A's (registration), S's (the application's authorities) or P's (the operator); never R's.
    assert writers(w) <= {w.registration_role, w.names["service"], w.names["operator"]}
    assert w.names["app"] not in writers(w)
    # And R could not have: it holds no write right on any account table.
    with w.runtime_engine.connect() as c:
        for table in TABLES[:3]:
            for right in ("INSERT", "DELETE"):
                assert not c.scalar(text("SELECT has_table_privilege(current_user, :t, :r)"),
                                    {"t": "kalillac." + table, "r": right})
