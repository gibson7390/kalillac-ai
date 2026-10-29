"""Email-verification API, configuration, privacy, account-preservation and Checkout contracts."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import event, select, update

from account_test_db import make_session_factory, make_sqlite_engine
from verification_test_support import (
    COOKIE, EMAIL, ENV, NOW, PASSWORD, SENSITIVE, SETTINGS,
    FakeVerificationEmail, add_session, add_user, application, client_for, confirm,
    request_link, request_verification, rows, user_row,
)
from kalillac_accounts.email_gateway import EmailGateway, EmailVerificationGateway
from kalillac_accounts.email_verification import (
    VerificationService, hash_verification_token, new_verification_token,
)
from kalillac_accounts.passwords import verify_password
from kalillac_accounts.router import AccountSettings, load_account_settings
from kalillac_accounts.verification_config import (
    VerificationConfigError, load_verification_config,
)
from kalillac_db.models import (
    AccountBilling, AccountEmailVerificationToken, AccountEntitlement, AccountRecoveryToken,
    AccountSession, AccountUsageDaily, Base, User,
)

REQUEST = "/api/account/email-verification/request"
CONFIRM = "/api/account/email-verification/confirm"


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def gateway():
    return FakeVerificationEmail()


@pytest.fixture
def client(db, gateway):
    with client_for(db, gateway) as client:
        yield client


def assert_response(response, status, value):
    assert response.status_code == status
    assert response.json() == value
    assert response.headers["cache-control"] == "no-store"


def signed_in(db, **kwargs):
    user_id = add_user(db, **kwargs)
    return user_id, add_session(db, user_id)


def seed_token(db, user_id, *, email=EMAIL, created=NOW, expires=None, consumed=None):
    token = new_verification_token()
    with db() as session, session.begin():
        session.add(AccountEmailVerificationToken(
            user_id=user_id, email_hash=hashlib.sha256(email.encode()).hexdigest(),
            token_hash=hash_verification_token(token), created_at=created,
            expires_at=expires or created + timedelta(hours=24), consumed_at=consumed,
        ))
    return token


# --- feature flag and configuration -------------------------------------------------


@pytest.mark.parametrize("flag", [None, "", "false", "0", "off"])
def test_verification_disabled_has_no_routes(flag, db, gateway):
    env = {key: value for key, value in ENV.items() if key != "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED"}
    if flag is not None:
        env["KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED"] = flag
    assert load_verification_config(gateway, env) is None
    with client_for(db, gateway, environ=env) as client:
        for path in (REQUEST, CONFIRM):
            assert client.post(path, json={}).status_code == 404
    # No gateway or URL is needed while off.
    assert load_verification_config(None, {}) is None


def test_account_settings_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED", raising=False)
    assert AccountSettings().email_verification_enabled is False
    assert load_account_settings().email_verification_enabled is False
    monkeypatch.setenv("KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED", "true")
    assert load_account_settings().email_verification_enabled is True


@pytest.mark.parametrize("missing", [
    "KALILLAC_ACCOUNTS_ENABLED", "KALILLAC_DB_ENABLED",
    "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_URL", "KALILLAC_EMAIL_FROM",
])
def test_configuration_requires_every_prerequisite(missing, gateway):
    env = {key: value for key, value in ENV.items() if key != missing}
    with pytest.raises(VerificationConfigError) as error:
        load_verification_config(gateway, env)
    assert missing in str(error.value)


class _RecoveryOnly:
    def send_password_reset(self, to, link):
        pass

    def send_password_changed(self, to):
        pass


@pytest.mark.parametrize("supplied", [None, object(), _RecoveryOnly()])
def test_configuration_requires_explicit_verification_gateway(supplied):
    with pytest.raises(VerificationConfigError, match="EmailVerificationGateway"):
        load_verification_config(supplied, ENV)


def test_gateway_protocols_are_separate(gateway):
    # The recovery contract did not grow a verification method.
    assert isinstance(gateway, EmailVerificationGateway)
    assert not isinstance(gateway, EmailGateway)
    assert not isinstance(_RecoveryOnly(), EmailVerificationGateway)
    assert not hasattr(EmailGateway, "send_email_verification")


@pytest.mark.parametrize("url", [
    "", "relative/verify", "http://kalillac.ai/verify", "ftp://kalillac.ai/verify",
    "https://user:pass@kalillac.ai/verify", "https://kalillac.ai/verify?next=1",
    "https://kalillac.ai/verify#token=x", "https://kalillac.ai/verify?",
    "https:///verify", "https://kalillac.ai:0/verify", "https://kalillac.ai/ver ify",
    "https://kalillac.ai\\verify", "http://10.0.0.1/verify",
])
def test_verification_url_rejected_without_echoing_value(url, gateway):
    with pytest.raises(VerificationConfigError) as error:
        load_verification_config(gateway, {**ENV, "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_URL": url})
    assert str(error.value) == "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_URL is invalid."


@pytest.mark.parametrize("url", [
    "https://kalillac.ai/verify-email", "http://localhost:8000/verify",
    "http://127.0.0.1/verify", "http://[::1]:8080/verify",
])
def test_https_or_loopback_development_urls(url, gateway):
    config = load_verification_config(gateway, {**ENV, "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_URL": url})
    assert config.verification_url == url


@pytest.mark.parametrize("sender", ["", "missing", "a b@example.com", "x" * 321 + "@a.io"])
def test_sender_format_fails_closed(sender, gateway):
    with pytest.raises(VerificationConfigError, match="KALILLAC_EMAIL_FROM is invalid"):
        load_verification_config(gateway, {**ENV, "KALILLAC_EMAIL_FROM": sender})


def test_config_repr_does_not_disclose_url_or_email(gateway):
    config = load_verification_config(gateway, ENV)
    assert repr(config) == "VerificationConfig()"
    assert config.email_from == "help@kalillac.ai"


@pytest.mark.parametrize("enabled", [False, True])
def test_application_startup_flag_is_off_and_cannot_select_real_gateway(enabled):
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("KALILLAC_") or name == "CREDENTIALS_DIRECTORY":
            env.pop(name)
    env.update({"OPENAI_API_KEY": "not-real", "RUN_LIVE_TESTS": "false", "HF_HUB_OFFLINE": "1"})
    if enabled:
        env.update(ENV)
    code = (
        "import app_fastapi_candidate as app; "
        "print('VERIFICATION_ROUTES='+str(any('/email-verification/' in r.path for r in app.api.routes)))"
    )
    result = subprocess.run([sys.executable, "-B", "-c", code],
                            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True)
    if enabled:
        assert result.returncode != 0
        assert "explicitly provided EmailVerificationGateway" in result.stderr
        assert ENV["KALILLAC_ACCOUNT_EMAIL_VERIFICATION_URL"] not in result.stderr
    else:
        assert result.returncode == 0 and "VERIFICATION_ROUTES=False" in result.stdout


# --- body contracts --------------------------------------------------------------------


@pytest.mark.parametrize("path", [REQUEST, CONFIRM])
@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "application/jsonp", ""])
def test_requires_json(client, path, content_type):
    response = client.post(path, content=b"{}", headers={"content-type": content_type})
    assert_response(response, 415, {"error": "json_required"})


@pytest.mark.parametrize("path,payload,code", [
    (REQUEST, b"{", "invalid_json"), (REQUEST, b"[]", "invalid_body"),
    (REQUEST, b'{"email":"person@example.com"}', "invalid_body"), (REQUEST, b"null", "invalid_body"),
    (CONFIRM, b"{", "invalid_json"), (CONFIRM, b"{}", "invalid_body"), (CONFIRM, b'"x"', "invalid_body"),
    (CONFIRM, b'{"token":"x","extra":1}', "invalid_body"),
])
def test_invalid_json_and_shape(client, path, payload, code):
    response = client.post(path, content=payload, headers={"content-type": "application/json"})
    assert_response(response, 400, {"error": code})


@pytest.mark.parametrize("path", [REQUEST, CONFIRM])
def test_body_over_limit_rejected(client, path):
    body = b'{"token":"' + b"a" * 4096 + b'"}'
    response = client.post(path, content=body, headers={"content-type": "application/json"})
    assert_response(response, 413, {"error": "body_too_large"})


def test_body_exact_limit_accepted_and_still_validated(client):
    body = b'{"token":"' + b"a" * (4096 - 12) + b'"}'
    assert len(body) == 4096
    response = client.post(CONFIRM, content=body, headers={"content-type": "application/json"})
    assert_response(response, 400, {"error": "invalid_or_expired_token"})


# --- request route -------------------------------------------------------------------


def test_request_requires_authentication(db, client, gateway):
    user_id = add_user(db)
    from datetime import datetime, timezone
    expired = add_session(db, user_id, datetime.now(timezone.utc) - timedelta(minutes=1))
    for cookie in (None, "forged-session", expired):
        client.cookies.clear()
        if cookie:
            client.cookies.set(COOKIE, cookie)
        assert_response(client.post(REQUEST, json={}), 401, {"error": "not_authenticated"})
    assert gateway.sent == [] and rows(db, AccountEmailVerificationToken) == []


def test_request_issues_hashed_24_hour_fragment_link(db, client, gateway):
    user_id, cookie = signed_in(db)
    response = request_verification(client, cookie)
    assert_response(response, 202, {"ok": True})
    assert "set-cookie" not in response.headers
    [(to, link)] = gateway.sent
    assert to == EMAIL
    base, token = link.split("#token=", 1)
    assert base == ENV["KALILLAC_ACCOUNT_EMAIL_VERIFICATION_URL"] and "?" not in link
    [row] = rows(db, AccountEmailVerificationToken)
    assert row["user_id"] == user_id
    assert row["token_hash"] == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert row["email_hash"] == hashlib.sha256(EMAIL.encode()).hexdigest()
    assert row["expires_at"] - row["created_at"] == timedelta(hours=24)
    assert row["consumed_at"] is None
    assert token not in repr(rows(db, AccountEmailVerificationToken)) and EMAIL not in repr(row)
    assert token not in response.text and EMAIL not in response.text


def test_generated_token_is_32_random_bytes_canonically_encoded():
    import base64
    token = new_verification_token()
    assert len(token) == 43 and "=" not in token
    assert len(base64.urlsafe_b64decode(token + "=")) == 32
    assert hash_verification_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert len({new_verification_token() for _ in range(50)}) == 50


def test_reissue_invalidates_older_unused_tokens(db, client, gateway):
    _, cookie = signed_in(db)
    first = request_link(client, gateway, cookie)
    second = request_link(client, gateway, cookie)
    stored = rows(db, AccountEmailVerificationToken)
    assert len(stored) == 2 and first != second
    assert [row["consumed_at"] is not None for row in stored] == [True, False]
    assert_response(confirm(client, first), 400, {"error": "invalid_or_expired_token"})
    assert_response(confirm(client, second), 200, {"ok": True})


def test_already_verified_account_gets_same_202_and_nothing_is_sent(db, client, gateway):
    _, cookie = signed_in(db, verified_at=NOW)
    response = request_verification(client, cookie)
    assert_response(response, 202, {"ok": True})
    assert gateway.sent == [] and rows(db, AccountEmailVerificationToken) == []


def test_account_inactive_after_authentication_gets_202_and_nothing(db, client, gateway, monkeypatch):
    import kalillac_accounts.email_verification as service_module
    user_id, cookie = signed_in(db)
    real = service_module.lock_user
    def deactivated_meanwhile(session, locked_id):
        session.execute(update(User).where(User.id == locked_id).values(is_active=False))
        return real(session, locked_id)
    monkeypatch.setattr(service_module, "lock_user", deactivated_meanwhile)
    assert_response(request_verification(client, cookie), 202, {"ok": True})
    assert gateway.sent == [] and rows(db, AccountEmailVerificationToken) == []


def test_inactive_account_session_uses_existing_authentication_contract(db, client, gateway):
    # get_signed_in_user never authenticates an inactive account.
    _, cookie = signed_in(db, active=False)
    assert_response(request_verification(client, cookie), 401, {"error": "not_authenticated"})
    assert gateway.sent == []


def test_issuance_and_delivery_only_after_public_response(db, gateway):
    import asyncio
    user_id, cookie = signed_in(db)
    sent = []
    calls = []
    def factory():
        calls.append(bool(sent))
        return db
    app = application(db, gateway)
    app.router.routes[:] = []
    from kalillac_accounts.email_verification_router import build_email_verification_router
    app.include_router(build_email_verification_router(
        session_factory=factory, gateway=gateway, settings=SETTINGS, environ=ENV, clock=lambda: NOW,
    ))
    scope = dict(type="http", asgi={"version": "3.0"}, http_version="1.1",
                 method="POST", scheme="https", path=REQUEST, raw_path=REQUEST.encode(),
                 query_string=b"", root_path="",
                 headers=[(b"content-type", b"application/json"), (b"cookie", f"{COOKIE}={cookie}".encode())],
                 server=("testserver", 443), client=("192.0.2.10", 123))
    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}
    async def send(message):
        sent.append(message)
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 202
    # First factory use is authentication (before the response); issuance after.
    assert calls == [False, True] and len(gateway.sent) == 1


def test_delivery_failure_rolls_back_issuance_and_logs_only_fixed_code(db, client, gateway, capsys, caplog):
    _, cookie = signed_in(db)
    previous = request_link(client, gateway, cookie)
    gateway.fail = True
    failed = request_link(client, gateway, cookie)
    [stored] = rows(db, AccountEmailVerificationToken)
    assert stored["token_hash"] == hash_verification_token(previous) and stored["consumed_at"] is None
    assert_response(confirm(client, failed), 400, {"error": "invalid_or_expired_token"})
    assert_response(confirm(client, previous), 200, {"ok": True})
    output = capsys.readouterr()
    assert output.out == "ACCOUNT_EMAIL_VERIFICATION_DELIVERY_FAILED\n" and output.err == ""
    assert caplog.text == ""


def test_commit_failure_after_delivery_leaves_delivered_token_unusable(db, client, gateway, capsys, caplog):
    _, cookie = signed_in(db)
    def failed_commit(session):
        # Only the issuance commit (after delivery), not authentication's.
        if session.scalar(select(AccountEmailVerificationToken.id).limit(1)) is not None:
            raise RuntimeError(SENSITIVE)
    event.listen(db.class_, "before_commit", failed_commit)
    try:
        token = request_link(client, gateway, cookie)
    finally:
        event.remove(db.class_, "before_commit", failed_commit)
    assert len(gateway.sent) == 1
    assert rows(db, AccountEmailVerificationToken) == []
    assert_response(confirm(client, token), 400, {"error": "invalid_or_expired_token"})
    output = capsys.readouterr()
    assert output.out == "ACCOUNT_EMAIL_VERIFICATION_REQUEST_FAILED\n" and output.err == "" and caplog.text == ""


# --- confirmation route -------------------------------------------------------------------


def test_confirmation_verifies_only_that_user_and_preserves_everything_else(db, client, gateway):
    user_id, cookie = signed_in(db)
    other_id, other_cookie = signed_in(db, email="other@example.com")
    with db() as session, session.begin():
        session.add(AccountBilling(user_id=user_id, stripe_customer_id="cus_local",
                                   stripe_subscription_id="sub_local", stripe_price_id="price_local",
                                   subscription_status="active"))
        session.add(AccountEntitlement(user_id=user_id, tier="paid", source="billing"))
        session.add(AccountUsageDaily(user_id=user_id, usage_date=NOW.date(), successful_chats=4))
        session.add(AccountRecoveryToken(user_id=user_id, purpose="password_reset", token_hash="c" * 64,
                                         created_at=NOW, expires_at=NOW + timedelta(minutes=30)))
    token = request_link(client, gateway, cookie)
    preserved = {model: rows(db, model) for model in (
        AccountBilling, AccountEntitlement, AccountUsageDaily, AccountRecoveryToken, AccountSession)}
    identity, other = user_row(db, user_id), user_row(db, other_id)
    seen = []
    @event.listens_for(db.kw["bind"], "before_cursor_execute")
    def queries(connection, cursor, statement, params, context, many):
        seen.append(statement)
    client.cookies.clear()
    response = confirm(client, token)
    event.remove(db.kw["bind"], "before_cursor_execute", queries)
    assert_response(response, 200, {"ok": True})
    assert "set-cookie" not in response.headers
    for table in ("account_billing", "account_entitlements", "account_usage", "account_sessions",
                  "account_recovery_tokens", "anonymous_usage"):
        assert not any(table in sql for sql in seen), table
    for model, before in preserved.items():
        assert rows(db, model) == before
    after = user_row(db, user_id)
    assert after["email_verified_at"] is not None
    assert {k: v for k, v in after.items() if k != "email_verified_at"} == {
        k: v for k, v in identity.items() if k != "email_verified_at"}
    assert verify_password(after["password_hash"], PASSWORD)
    assert user_row(db, other_id) == other
    # Existing sessions keep working; nothing signed in or out.
    client.cookies.set(COOKIE, cookie)
    me = client.get("/api/account/me")
    assert me.status_code == 200 and me.json()["user"]["email_verified"] is True
    client.cookies.set(COOKIE, other_cookie)
    assert client.get("/api/account/me").json()["user"]["email_verified"] is False
    client.cookies.clear()
    assert client.get("/api/account/entitlements", headers={"Cookie": f"{COOKIE}={cookie}"}).json()[
        "entitlements"]["tier"] == "paid"


@pytest.mark.parametrize("kind", ["unknown", "expired", "consumed", "replayed", "email-mismatch", "inactive"])
def test_unusable_tokens_share_public_error(db, client, gateway, kind):
    user_id, cookie = signed_in(db)
    if kind == "unknown":
        token = new_verification_token()
    elif kind == "expired":
        token = seed_token(db, user_id, created=NOW - timedelta(hours=25), expires=NOW)
    elif kind == "consumed":
        token = seed_token(db, user_id, consumed=NOW)
    elif kind == "replayed":
        token = request_link(client, gateway, cookie)
        assert_response(confirm(client, token), 200, {"ok": True})
        with db() as session, session.begin():
            session.execute(update(User).where(User.id == user_id).values(email_verified_at=None))
    elif kind == "email-mismatch":
        token = seed_token(db, user_id, email="previous@example.com")
    else:
        token = seed_token(db, user_id)
        with db() as session, session.begin():
            session.execute(update(User).where(User.id == user_id).values(is_active=False))
    before = user_row(db, user_id)["email_verified_at"]
    assert_response(confirm(client, token), 400, {"error": "invalid_or_expired_token"})
    assert user_row(db, user_id)["email_verified_at"] == before


@pytest.mark.parametrize("token", [None, 12, "", "bad", "A" * 42 + "B", "A" * 44, "!" * 43, ["x"]])
def test_malformed_token_rejected_before_database_work(db, gateway, token):
    def factory():
        raise AssertionError("database used")
    from fastapi.testclient import TestClient
    from kalillac_accounts.email_verification_router import build_email_verification_router
    app = FastAPI()
    app.include_router(build_email_verification_router(
        session_factory=factory, gateway=gateway, settings=SETTINGS, environ=ENV,
    ))
    with TestClient(app, base_url="https://testserver") as client:
        assert_response(confirm(client, token), 400, {"error": "invalid_or_expired_token"})


def test_me_exposes_only_a_boolean_and_only_when_enabled(db, gateway):
    user_id = add_user(db, verified_at=NOW)
    cookie = add_session(db, user_id)
    with client_for(db, gateway) as client:
        client.cookies.set(COOKIE, cookie)
        user = client.get("/api/account/me").json()["user"]
        assert user == {"id": str(user_id), "email": EMAIL, "created_at": user["created_at"],
                        "email_verified": True}
        assert "verified_at" not in json.dumps(user) and "2030" not in json.dumps(user)
    disabled = AccountSettings(cookie_secure=True, trust_cloudflare_client_ip=True)
    with client_for(db, gateway, settings=disabled) as client:
        client.cookies.set(COOKIE, cookie)
        assert set(client.get("/api/account/me").json()["user"]) == {"id", "email", "created_at"}


def test_registration_and_login_remain_open_to_unverified_accounts(db, client):
    response = client.post("/api/account/register", json={"email": "new@example.com", "password": PASSWORD})
    assert response.status_code == 201 and "email_verified" not in response.json()["user"]
    login = client.post("/api/account/login", json={"email": "new@example.com", "password": PASSWORD})
    assert login.status_code == 200 and "email_verified" not in login.json()["user"]
    assert user_row(db, uuid.UUID(login.json()["user"]["id"]))["email_verified_at"] is None


# --- privacy and failure containment --------------------------------------------------------


@pytest.mark.parametrize("stage", ["authenticate", "lock", "issuance", "confirm", "limiter"])
def test_ordinary_failures_never_leak_detail_or_traceback(db, gateway, capsys, caplog, monkeypatch, stage):
    import kalillac_accounts.email_verification as service_module
    import kalillac_accounts.email_verification_router as router
    def failing(*args, **kwargs):
        raise RuntimeError(SENSITIVE)
    user_id, cookie = signed_in(db)
    with client_for(db, gateway) as client:
        token = request_link(client, gateway, cookie)
        capsys.readouterr()
        if stage == "authenticate":
            monkeypatch.setattr(router, "get_signed_in_user", failing)
            assert_response(request_verification(client, cookie, peer="192.0.2.20"), 503,
                            {"error": "verification_unavailable"})
            expected = "ACCOUNT_EMAIL_VERIFICATION_REQUEST_FAILED\n"
        elif stage in ("lock", "issuance"):
            target = (service_module, "lock_user") if stage == "lock" else (service_module.repository, "issue_token")
            monkeypatch.setattr(*target, failing)
            assert_response(request_verification(client, cookie, peer="192.0.2.20"), 202, {"ok": True})
            expected = "ACCOUNT_EMAIL_VERIFICATION_REQUEST_FAILED\n"
        elif stage == "confirm":
            monkeypatch.setattr(service_module.repository, "confirm_token", failing)
            assert_response(confirm(client, token), 503, {"error": "verification_unavailable"})
            expected = "ACCOUNT_EMAIL_VERIFICATION_CONFIRM_FAILED\n"
        else:
            monkeypatch.setattr(router.VerificationRateLimiter, "finish_confirm", failing)
            assert_response(confirm(client, token), 200, {"ok": True})
            expected = "ACCOUNT_EMAIL_VERIFICATION_LIMITER_FAILED\n"
    output = capsys.readouterr()
    assert output.out == expected and output.err == "" and caplog.text == ""
    for forbidden in (SENSITIVE, "RuntimeError", "Traceback", EMAIL, token, hash_verification_token(token),
                      hashlib.sha256(EMAIL.encode()).hexdigest(), str(user_id), cookie,
                      ENV["KALILLAC_ACCOUNT_EMAIL_VERIFICATION_URL"], "192.0.2"):
        assert forbidden not in output.out


def test_database_error_parameters_never_escape(db, gateway, monkeypatch, capsys, caplog):
    from sqlalchemy.exc import OperationalError
    import kalillac_accounts.email_verification as service_module
    user_id, cookie = signed_in(db)
    def failing(*args, **kwargs):
        raise OperationalError("UPDATE users SET secret=?", {"email": EMAIL}, Exception(SENSITIVE))
    with client_for(db, gateway) as client:
        token = request_link(client, gateway, cookie)
        capsys.readouterr()
        monkeypatch.setattr(service_module.repository, "confirm_token", failing)
        assert_response(confirm(client, token), 503, {"error": "verification_unavailable"})
    output = capsys.readouterr()
    assert output.out == "ACCOUNT_EMAIL_VERIFICATION_CONFIRM_FAILED\n"
    assert "UPDATE" not in output.out and SENSITIVE not in output.out and caplog.text == ""


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("operation", ["request", "confirm"])
def test_base_exceptions_are_not_ordinary_failures(db, gateway, exception, operation, monkeypatch, capsys):
    import kalillac_accounts.email_verification as service_module
    user_id = add_user(db)
    token = seed_token(db, user_id)
    def stop(*args):
        raise exception()
    monkeypatch.setattr(service_module, "lock_user", stop)
    monkeypatch.setattr(service_module.repository, "confirm_token", stop)
    service = VerificationService(lambda: db, gateway, load_verification_config(gateway, ENV), lambda: NOW)
    with pytest.raises(exception):
        if operation == "request":
            service.request_verification(user_id)
        else:
            service.confirm_verification(token)
    assert capsys.readouterr().out == ""


def test_route_handlers_catch_only_ordinary_exceptions():
    source = (Path(__file__).resolve().parents[1] / "kalillac_accounts" / "email_verification_router.py").read_text()
    service = (Path(__file__).resolve().parents[1] / "kalillac_accounts" / "email_verification.py").read_text()
    for text in (source, service):
        assert "except BaseException" not in text and "except:" not in text


# --- Checkout enforcement -------------------------------------------------------------------


@pytest.fixture
def billing(db, gateway):
    from kalillac_billing.router import build_billing_router
    from test_billing import CONFIG, FakeGateway

    def make(settings):
        stripe = FakeGateway()
        router = build_billing_router(config=CONFIG, settings=settings, gateway=stripe,
                                      session_factory=lambda: db)
        return stripe, client_for(db, gateway, settings=settings, extra_routers=[router])
    return make


def test_unverified_checkout_rejected_before_stripe_or_billing_writes(db, billing):
    user_id, cookie = signed_in(db)
    stripe, client = billing(SETTINGS)
    seen = []
    @event.listens_for(db.kw["bind"], "before_cursor_execute")
    def queries(connection, cursor, statement, params, context, many):
        seen.append(statement)
    with client:
        client.cookies.set(COOKIE, cookie)
        response = client.post("/api/account/billing/checkout")
    event.remove(db.kw["bind"], "before_cursor_execute", queries)
    assert_response(response, 403, {"error": "email_verification_required"})
    assert stripe.checkout_calls == [] and stripe.retrieve_calls == []
    assert rows(db, AccountBilling) == []
    assert all(sql.lstrip().upper().startswith(("SELECT", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"))
               for sql in seen)


def test_verified_checkout_proceeds_when_enabled(db, billing):
    _, cookie = signed_in(db, verified_at=NOW)
    stripe, client = billing(SETTINGS)
    with client:
        client.cookies.set(COOKIE, cookie)
        response = client.post("/api/account/billing/checkout")
    assert response.status_code == 200 and "checkout_url" in response.json()
    assert len(stripe.checkout_calls) == 1


def test_checkout_unchanged_when_disabled(db, billing):
    _, cookie = signed_in(db)
    disabled = AccountSettings(cookie_secure=True, trust_cloudflare_client_ip=True)
    stripe, client = billing(disabled)
    with client:
        client.cookies.set(COOKIE, cookie)
        response = client.post("/api/account/billing/checkout")
        assert response.status_code == 200 and set(response.json()) == {"checkout_url"}
        assert response.headers["cache-control"] == "no-store"
        client.cookies.clear()
        assert_response(client.post("/api/account/billing/checkout"), 401, {"error": "not_authenticated"})
    assert len(stripe.checkout_calls) == 1


def test_portal_and_billing_status_stay_available_to_unverified_accounts(db, billing):
    user_id, cookie = signed_in(db)
    with db() as session, session.begin():
        session.add(AccountBilling(user_id=user_id, stripe_customer_id="cus_1",
                                   stripe_subscription_id="sub_1", subscription_status="active"))
    stripe, client = billing(SETTINGS)
    with client:
        client.cookies.set(COOKIE, cookie)
        assert client.post("/api/account/billing/portal").status_code == 200
        assert client.get("/api/account/billing").status_code == 200
    assert len(stripe.portal_calls) == 1


def test_webhooks_still_grant_paid_access_to_unverified_accounts(db, billing):
    from test_billing import _subscription
    user_id, _ = signed_in(db)
    stripe, client = billing(SETTINGS)
    stripe.subscriptions["sub_1"] = _subscription(user_id)
    with client:
        response = client.post(
            "/api/billing/stripe/webhook",
            content=json.dumps({"id": "evt_1", "type": "customer.subscription.updated",
                                "subscription": "sub_1", "customer": "cus_1", "user": str(user_id)}).encode(),
            headers={"Stripe-Signature": "valid", "Content-Type": "application/json"},
        )
    assert response.status_code == 200
    with db() as session:
        assert session.get(AccountEntitlement, user_id).tier == "paid"
        assert session.get(User, user_id).email_verified_at is None


def test_reconciliation_is_unaffected_by_verification(db):
    from kalillac_billing.service import reconcile_account
    from test_billing import ALLOWED_PRICES, FakeGateway, _subscription
    user_id = add_user(db)
    with db() as session, session.begin():
        session.add(AccountEntitlement(user_id=user_id, tier="free", source="registration"))
        session.add(AccountBilling(user_id=user_id, stripe_customer_id="cus_1",
                                   stripe_subscription_id="sub_1", subscription_status="incomplete"))
    stripe = FakeGateway()
    stripe.subscriptions["sub_1"] = _subscription(user_id)
    outcome = reconcile_account(user_id, gateway=stripe, session_factory=db, allowed_price_ids=ALLOWED_PRICES)
    assert outcome.result == "subscription_reconciled"
    with db() as session:
        assert session.get(AccountEntitlement, user_id).tier == "paid"
