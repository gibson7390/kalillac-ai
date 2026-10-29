"""Recovery API, service privacy, configuration and account-preservation contracts."""
from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import event, select, update

from account_test_db import make_session_factory, make_sqlite_engine
from recovery_test_support import (
    COOKIE, ENV, NEW_PASSWORD, NOW, PASSWORD, SETTINGS, SENSITIVE,
    FakeEmail, add_sessions, add_user, application, client_for, confirm, request_link, rows,
)
from kalillac_accounts.email_gateway import EmailDeliveryError
from kalillac_accounts.passwords import verify_password
from kalillac_accounts.recovery import RecoveryService
from kalillac_accounts.recovery_config import RecoveryConfigError, load_recovery_config
from kalillac_accounts.recovery_tokens import (
    hash_recovery_token, new_recovery_token, valid_recovery_token,
)
from kalillac_db.models import (
    AccountBilling, AccountEntitlement, AccountRecoveryToken, AccountSession,
    AccountUsageDaily, Base, User,
)


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def gateway():
    return FakeEmail()


@pytest.fixture
def client(db, gateway):
    with client_for(db, gateway) as client:
        yield client


def assert_response(response, status, value):
    assert response.status_code == status
    assert response.json() == value
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("flag", [None, "", "false", "0", "off"])
def test_recovery_disabled_has_no_routes(flag, db, gateway):
    env = {**ENV, "KALILLAC_ACCOUNT_RECOVERY_URL": SENSITIVE}
    if flag is None:
        del env["KALILLAC_ACCOUNT_RECOVERY_ENABLED"]
    else:
        env["KALILLAC_ACCOUNT_RECOVERY_ENABLED"] = flag
    with client_for(db, None, environ=env) as client:
        for path in ("request", "confirm"):
            assert client.post("/api/account/recovery/" + path, json={}).status_code == 404
    assert gateway.reset == gateway.changed == []


@pytest.mark.parametrize("missing", [
    "KALILLAC_ACCOUNTS_ENABLED", "KALILLAC_DB_ENABLED",
    "KALILLAC_ACCOUNT_RECOVERY_URL", "KALILLAC_EMAIL_FROM",
])
def test_configuration_requires_every_prerequisite(missing, gateway):
    env = dict(ENV)
    env.pop(missing)
    with pytest.raises(RecoveryConfigError) as caught:
        load_recovery_config(gateway, env)
    assert SENSITIVE not in str(caught.value)


@pytest.mark.parametrize("gateway", [None, object(), type("Partial", (), {"send_password_reset": lambda *a: None})()])
def test_configuration_requires_explicit_complete_gateway(gateway):
    with pytest.raises(RecoveryConfigError, match="EmailGateway"):
        load_recovery_config(gateway, ENV)


@pytest.mark.parametrize("url", [
    "", "/reset", "http://kalillac.ai/reset", "ftp://localhost/reset",
    "https://user:password@kalillac.ai/reset", "https://@kalillac.ai/reset",
    "https://kalillac.ai/reset?existing=1", "https://kalillac.ai/reset?",
    "https://kalillac.ai/reset#existing", "https://kalillac.ai/reset#",
    "https://kalillac.ai:bad/reset", "https://kalillac.ai:65536/reset",
    "https://[broken/reset", "https://kalillac.ai/\nreset", "https://kalillac.ai\\@evil.invalid",
])
def test_recovery_url_rejected_without_echoing_value(url, gateway):
    with pytest.raises(RecoveryConfigError) as caught:
        load_recovery_config(gateway, {**ENV, "KALILLAC_ACCOUNT_RECOVERY_URL": url})
    assert str(caught.value) == "KALILLAC_ACCOUNT_RECOVERY_URL is invalid."


@pytest.mark.parametrize("url", [
    "https://kalillac.ai/reset", "http://localhost/reset", "http://127.0.0.1:8080/reset",
    "http://127.10.20.30/reset", "http://[::1]:8080/reset",
])
def test_https_or_loopback_development_urls(url, gateway):
    assert load_recovery_config(gateway, {**ENV, "KALILLAC_ACCOUNT_RECOVERY_URL": url}).recovery_url == url


@pytest.mark.parametrize("sender", ["", "missing", "a b@example.com", None, 123])
def test_sender_format_fails_closed(sender, gateway):
    with pytest.raises(RecoveryConfigError):
        load_recovery_config(gateway, {**ENV, "KALILLAC_EMAIL_FROM": sender})


def test_config_repr_does_not_disclose_url_or_email(gateway):
    config = load_recovery_config(gateway, ENV)
    assert repr(config) == "RecoveryConfig()"
    assert str(EmailDeliveryError()) == "EMAIL_DELIVERY_FAILED"


@pytest.mark.parametrize("enabled", [False, True])
def test_application_startup_flag_is_off_and_requires_explicit_email_provider(enabled):
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("KALILLAC_") or name == "CREDENTIALS_DIRECTORY":
            env.pop(name)
    env.update({"OPENAI_API_KEY": "not-real", "RUN_LIVE_TESTS": "false", "HF_HUB_OFFLINE": "1"})
    if enabled:
        env.update(ENV)
    code = (
        "import app_fastapi_candidate as app; "
        "print('RECOVERY_ROUTES='+str(any('/recovery/' in route.path for route in app.api.routes)))"
    )
    result = subprocess.run([sys.executable, "-B", "-c", code],
                            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True)
    if enabled:
        # No KALILLAC_EMAIL_PROVIDER: startup fails before any route is installed.
        assert result.returncode != 0
        assert "KALILLAC_EMAIL_PROVIDER must be one of: resend." in result.stderr
        assert ENV["KALILLAC_ACCOUNT_RECOVERY_URL"] not in result.stderr
    else:
        assert result.returncode == 0 and "RECOVERY_ROUTES=False" in result.stdout


@pytest.mark.parametrize("path", ["request", "confirm"])
@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "application/jsonp", ""])
def test_requires_json(client, path, content_type):
    assert_response(client.post("/api/account/recovery/" + path, content=b"{}",
                                headers={"content-type": content_type}), 415, {"error": "json_required"})


@pytest.mark.parametrize("path", ["request", "confirm"])
@pytest.mark.parametrize("payload,code", [
    (b"{", "invalid_json"), (b"\xff", "invalid_json"), (b"null", "invalid_body"),
    (b"[]", "invalid_body"), (b"{}", "invalid_body"),
    (b'{"email":"a@example.com","token":"unwanted"}', "invalid_body"),
])
def test_invalid_json_and_shape(client, path, payload, code):
    assert_response(client.post("/api/account/recovery/" + path, content=payload,
                                headers={"content-type": "application/json"}), 400, {"error": code})


@pytest.mark.parametrize("path", ["request", "confirm"])
def test_body_over_limit_rejected(client, path):
    assert_response(client.post("/api/account/recovery/" + path, content=b" " * 4097,
                                headers={"content-type": "application/json"}), 413, {"error": "body_too_large"})


def test_body_exact_limit_accepted(client):
    body = b'{"email":"nobody@example.com"}'
    body += b" " * (4096 - len(body))
    assert_response(client.post("/api/account/recovery/request", content=body,
                                headers={"content-type": "Application/JSON; charset=utf-8"}),
                    202, {"ok": True})


@pytest.mark.parametrize("email", [None, 123, "", "missing", "a b@example.com", "x" * 321])
def test_invalid_email(client, email):
    assert_response(client.post("/api/account/recovery/request", json={"email": email}),
                    422, {"error": "invalid_email"})


def test_known_unknown_inactive_have_identical_public_responses(db, client, gateway, monkeypatch):
    add_user(db)
    add_user(db, "inactive@example.com", active=False)
    responses = [client.post("/api/account/recovery/request", json={"email": email})
                 for email in (" Person@Example.com ", "unknown@example.com", "inactive@example.com")]
    for response in responses:
        assert_response(response, 202, {"ok": True})
        assert "set-cookie" not in response.headers
    assert [item[0] for item in gateway.reset] == ["person@example.com"]
    # A concurrent identity update must not deliver to the pre-lock address.
    from types import SimpleNamespace
    from kalillac_db.repositories import recovery as repository
    real_lock = repository.lock_user
    def renamed(session, user_id):
        row = real_lock(session, user_id)
        return SimpleNamespace(id=row.id, email="renamed@example.com", is_active=True)
    monkeypatch.setattr(repository, "lock_user", renamed)
    response = client.post("/api/account/recovery/request", json={"email": "person@example.com"})
    assert_response(response, 202, {"ok": True})
    assert len(gateway.reset) == 1 and len(rows(db, AccountRecoveryToken)) == 1


def test_lookup_and_delivery_only_after_public_response(db, gateway):
    add_user(db)
    sent = []
    def factory():
        assert sent and sent[-1]["type"] == "http.response.body" and not sent[-1].get("more_body", False)
        return db
    from fastapi import FastAPI
    from kalillac_accounts.recovery_router import build_recovery_router
    app = FastAPI()
    app.include_router(build_recovery_router(session_factory=factory, gateway=gateway, environ=ENV))
    scope = dict(type="http", asgi={"version": "3.0"}, http_version="1.1",
                 method="POST", scheme="https", path="/api/account/recovery/request",
                 raw_path=b"/api/account/recovery/request", query_string=b"",
                 root_path="", headers=[(b"content-type", b"application/json")],
                 server=("testserver", 443), client=("192.0.2.10", 123))
    async def receive():
        return {"type": "http.request", "body": b'{"email":"person@example.com"}', "more_body": False}
    async def send(message):
        sent.append(message)
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 202 and len(gateway.reset) == 1


def test_storage_and_reissuance(db, client, gateway):
    user_id = add_user(db)
    first = request_link(client, gateway)
    second = request_link(client, gateway)
    stored = rows(db, AccountRecoveryToken)
    assert len(stored) == 2 and first != second
    assert {row["user_id"] for row in stored} == {user_id}
    assert [row["token_hash"] for row in stored] == [hash_recovery_token(first), hash_recovery_token(second)]
    assert stored[0]["consumed_at"] is not None and stored[1]["consumed_at"] is None
    assert all(row["expires_at"] - row["created_at"] == timedelta(minutes=30) for row in stored)
    assert first not in repr(stored) and second not in repr(stored) and "#token=" not in repr(stored)
    assert_response(confirm(client, first), 400, {"error": "invalid_or_expired_token"})


@pytest.mark.parametrize("token", [None, 12, "", "bad", "A" * 42 + "B", "A" * 44, "!" * 43])
def test_malformed_token_before_database_work(db, gateway, token):
    def refused():
        raise AssertionError("database must not be accessed")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from kalillac_accounts.recovery_router import build_recovery_router
    app = FastAPI()
    app.include_router(build_recovery_router(session_factory=refused, gateway=gateway, environ=ENV))
    with TestClient(app) as client:
        assert_response(confirm(client, token), 400, {"error": "invalid_or_expired_token"})


@pytest.mark.parametrize("kind", ["unknown", "expired", "consumed", "replayed", "wrong-purpose", "inactive"])
def test_unusable_tokens_share_public_error(db, client, gateway, kind):
    user_id = add_user(db)
    token = request_link(client, gateway)
    if kind == "unknown":
        token = new_recovery_token()
    elif kind == "replayed":
        assert confirm(client, token).status_code == 200
    else:
        with db() as session, session.begin():
            if kind == "expired":
                session.execute(update(AccountRecoveryToken).values(
                    created_at=NOW - timedelta(hours=1), expires_at=NOW))
            elif kind == "consumed":
                session.execute(update(AccountRecoveryToken).values(consumed_at=NOW))
            elif kind == "wrong-purpose":
                session.connection().exec_driver_sql("PRAGMA ignore_check_constraints=ON")
                session.execute(update(AccountRecoveryToken).values(purpose="not-password-reset"))
            else:
                session.execute(update(User).where(User.id == user_id).values(is_active=False))
    assert_response(confirm(client, token), 400, {"error": "invalid_or_expired_token"})
    if kind != "replayed":
        with db() as session:
            assert verify_password(session.get(User, user_id).password_hash, PASSWORD)


@pytest.mark.parametrize("password,code", [(None, "invalid_password"), (123, "invalid_password"),
                                           ("short", "password_length"), ("x" * 257, "password_length")])
def test_password_rules(client, password, code):
    assert_response(confirm(client, new_recovery_token(), password), 422, {"error": code})


def test_password_hashing_is_outside_database_transaction(db, client, gateway, monkeypatch):
    import kalillac_accounts.recovery_router as router
    add_user(db)
    token = request_link(client, gateway)
    active = [False]
    @event.listens_for(db.kw["bind"], "begin")
    def began(connection):
        active[0] = True
    original = router.hash_password
    def checked(password):
        assert not active[0]
        return original(password)
    monkeypatch.setattr(router, "hash_password", checked)
    assert confirm(client, token).status_code == 200


def test_success_preserves_paid_identity_billing_entitlement_usage_and_memory(db, client, gateway, monkeypatch):
    import app_fastapi_candidate as app
    import stripe
    user_id = add_user(db)
    other_id = add_user(db, "other@example.com")
    tokens = add_sessions(db, user_id, 3)
    other_tokens = add_sessions(db, other_id, 1)
    with db() as session, session.begin():
        session.add(AccountBilling(user_id=user_id, stripe_customer_id="cus_local",
                                   stripe_subscription_id="sub_local", stripe_price_id="price_local",
                                   subscription_status="active"))
        session.add(AccountEntitlement(user_id=user_id, tier="paid", source="billing"))
        session.add(AccountUsageDaily(user_id=user_id, usage_date=NOW.date(), successful_chats=4))
    saved = {model: rows(db, model) for model in (AccountBilling, AccountEntitlement, AccountUsageDaily)}
    identity = next(row for row in rows(db, User) if row["id"] == user_id)
    monkeypatch.setattr(app, "SESSION_STATE", {"recovery-proof": {"memory": ["private-chat-value"], "searches": []}})
    memory = copy.deepcopy(app.SESSION_STATE)
    def forbidden(*a, **k):
        raise AssertionError("Stripe must not be called")
    monkeypatch.setattr(stripe, "StripeClient", forbidden)
    token = request_link(client, gateway)
    seen = []
    @event.listens_for(db.kw["bind"], "before_cursor_execute")
    def queries(connection, cursor, statement, params, context, many):
        seen.append(statement)
    client.cookies.set(COOKIE, "invalid-session")
    response = confirm(client, token)
    assert_response(response, 200, {"ok": True})
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"] and "Secure" in response.headers["set-cookie"]
    assert "Domain=" not in response.headers["set-cookie"]
    assert client.get("/api/account/me").status_code == 401
    event.remove(db.kw["bind"], "before_cursor_execute", queries)
    assert not any(table in sql for table in ("account_billing", "account_entitlements", "account_usage", "anonymous_usage")
                   for sql in seen)
    for model, before in saved.items():
        assert rows(db, model) == before
    after = next(row for row in rows(db, User) if row["id"] == user_id)
    assert {key: value for key, value in after.items() if key != "password_hash"} == {
        key: value for key, value in identity.items() if key != "password_hash"
    }
    assert verify_password(after["password_hash"], NEW_PASSWORD)
    assert not verify_password(after["password_hash"], PASSWORD)
    assert app.SESSION_STATE == memory
    sessions = rows(db, AccountSession)
    assert all(row["revoked_at"] is not None for row in sessions if row["user_id"] == user_id)
    assert all(row["revoked_at"] is None for row in sessions if row["user_id"] == other_id)
    assert client.post("/api/account/login", json={"email": "person@example.com", "password": PASSWORD}).status_code == 401
    login = client.post("/api/account/login", json={"email": "person@example.com", "password": NEW_PASSWORD})
    assert login.status_code == 200 and login.json()["user"]["id"] == str(user_id)
    assert client.get("/api/account/entitlements").json()["entitlements"]["tier"] == "paid"
    assert gateway.changed == ["person@example.com"]


def test_delivery_failure_invalidates_and_logs_only_fixed_code(db, client, gateway, capsys, caplog):
    add_user(db)
    gateway.fail_reset = True
    response = client.post("/api/account/recovery/request", json={"email": "person@example.com"})
    assert_response(response, 202, {"ok": True})
    token = gateway.reset[-1][1].split("#token=")[1]
    assert rows(db, AccountRecoveryToken) == []
    assert_response(confirm(client, token), 400, {"error": "invalid_or_expired_token"})
    assert capsys.readouterr().out == "ACCOUNT_RECOVERY_DELIVERY_FAILED\n"
    assert caplog.text == ""


def test_notification_failure_keeps_reset_successful(db, client, gateway, capsys, caplog):
    user_id = add_user(db)
    token = request_link(client, gateway)
    gateway.fail_changed = True
    assert_response(confirm(client, token), 200, {"ok": True})
    with db() as session:
        assert verify_password(session.get(User, user_id).password_hash, NEW_PASSWORD)
    assert capsys.readouterr().out == "ACCOUNT_RECOVERY_NOTIFICATION_FAILED\n"
    assert caplog.text == ""


@pytest.mark.parametrize("stage", ["lookup", "hash", "confirm", "issuance", "limiter"])
def test_ordinary_failures_never_leak_detail_or_traceback(db, gateway, capsys, caplog, monkeypatch, stage):
    import kalillac_accounts.recovery as service_module
    import kalillac_accounts.recovery_router as router
    def failing(*args, **kwargs):
        raise RuntimeError(SENSITIVE)
    add_user(db)
    with client_for(db, gateway) as client:
        token = request_link(client, gateway)
        if stage == "lookup":
            monkeypatch.setattr(service_module.repository, "lock_user", failing)
            response = client.post("/api/account/recovery/request", json={"email": "person@example.com"})
            assert response.status_code == 202
        elif stage == "issuance":
            monkeypatch.setattr(service_module.repository, "issue_token", failing)
            response = client.post("/api/account/recovery/request", json={"email": "person@example.com"})
            assert response.status_code == 202
        elif stage == "hash":
            monkeypatch.setattr(router, "hash_password", failing)
            assert confirm(client, token).status_code == 503
        elif stage == "limiter":
            monkeypatch.setattr(router.RecoveryRateLimiter, "finish_confirm", failing)
            assert confirm(client, token).status_code == 200
        else:
            monkeypatch.setattr(service_module.repository, "confirm_token", failing)
            assert confirm(client, token).status_code == 503
    output = capsys.readouterr()
    assert output.err == "" and caplog.text == ""
    assert output.out and all(repr(line).startswith("'ACCOUNT_RECOVERY_") for line in output.out.splitlines())
    for forbidden in (SENSITIVE, "RuntimeError", "Traceback", "person@example.com", token,
                      hash_recovery_token(token), ENV["KALILLAC_ACCOUNT_RECOVERY_URL"], PASSWORD, NEW_PASSWORD):
        assert forbidden not in output.out


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_background_does_not_swallow_base_exceptions(db, gateway, exception, monkeypatch):
    import kalillac_accounts.recovery as service_module
    add_user(db)
    def stop(*args):
        raise exception()
    monkeypatch.setattr(service_module.repository, "lock_user", stop)
    service = RecoveryService(lambda: db, gateway, load_recovery_config(gateway, ENV), lambda: NOW)
    with pytest.raises(exception):
        service.request_reset("person@example.com")


def test_failed_delivery_rolls_back_prior_invalidation(db, client, gateway, capsys):
    add_user(db)
    previous = request_link(client, gateway)
    gateway.fail_reset = True
    failed = request_link(client, gateway)
    stored = rows(db, AccountRecoveryToken)
    assert len(stored) == 1 and stored[0]["token_hash"] == hash_recovery_token(previous)
    assert stored[0]["consumed_at"] is None
    assert_response(confirm(client, failed), 400, {"error": "invalid_or_expired_token"})
    gateway.fail_reset = False
    assert_response(confirm(client, previous), 200, {"ok": True})
    assert capsys.readouterr().out == "ACCOUNT_RECOVERY_DELIVERY_FAILED\n"


def test_issuance_commit_failure_leaves_emailed_token_unusable(db, client, gateway, capsys, caplog):
    add_user(db)
    def failed_commit(session):
        raise RuntimeError(SENSITIVE)
    event.listen(db.class_, "before_commit", failed_commit, once=True)
    try:
        token = request_link(client, gateway)
        assert rows(db, AccountRecoveryToken) == []
        assert_response(confirm(client, token), 400, {"error": "invalid_or_expired_token"})
    finally:
        event.remove(db.class_, "before_commit", failed_commit)
    output = capsys.readouterr()
    assert output.out == "ACCOUNT_RECOVERY_REQUEST_FAILED\n" and output.err == "" and caplog.text == ""


@pytest.mark.parametrize("length", [12, 256])
def test_password_boundaries_and_cookie_clear_without_prior_session(db, client, gateway, length):
    user_id = add_user(db)
    token = request_link(client, gateway)
    response = confirm(client, token, "x" * length)
    assert_response(response, 200, {"ok": True})
    assert "Max-Age=0" in response.headers["set-cookie"]
    with db() as session:
        assert verify_password(session.get(User, user_id).password_hash, "x" * length)
        assert session.scalar(select(AccountSession).where(AccountSession.user_id == user_id)) is None


def test_generated_token_has_32_random_bytes_and_sha256_storage_digest(monkeypatch):
    import hashlib
    import kalillac_accounts.recovery_tokens as module
    calls = []
    def random(size):
        calls.append(size)
        return bytes(range(32))
    monkeypatch.setattr(module.secrets, "token_bytes", random)
    token = module.new_recovery_token()
    assert calls == [32] and valid_recovery_token(token)
    assert len(token) == 43 and "?" not in token and "=" not in token and "/" not in token
    assert hash_recovery_token(token) == hashlib.sha256(token.encode("ascii")).hexdigest()


def test_streamed_body_limit_stops_before_reading_excess_chunks():
    from kalillac_accounts.recovery_router import _read_body
    seen = []
    class Request:
        headers = {"content-type": "application/json"}
        async def stream(self):
            for index, chunk in enumerate([b" " * 2048, b" " * 2049, b"never-read"]):
                seen.append(index)
                yield chunk
    response = asyncio.run(_read_body(Request(), {"email"}))
    assert response.status_code == 413 and response.headers["cache-control"] == "no-store"
    assert seen == [0, 1]


@pytest.mark.parametrize("length", ["invalid", "-1", "9" * 5000])
def test_declared_bad_or_oversized_content_length_has_safe_response(length):
    from kalillac_accounts.recovery_router import _read_body
    class Request:
        headers = {"content-type": "application/json", "content-length": length}
        async def stream(self):
            raise AssertionError("must reject before reading")
            yield b""
    response = asyncio.run(_read_body(Request(), {"email"}))
    assert response.status_code == (413 if length.startswith("9") else 400)
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("kind", ["request", "confirm"])
def test_database_error_parameters_never_escape_route_or_background(db, gateway, monkeypatch, capsys, caplog, kind):
    from sqlalchemy.exc import DBAPIError
    import kalillac_accounts.recovery as service_module
    add_user(db)
    with client_for(db, gateway) as client:
        token = request_link(client, gateway)
        def failing(*args):
            raise DBAPIError(
                "SELECT private-email private-token private-password", {"email": "person@example.com"},
                RuntimeError(SENSITIVE),
            )
        monkeypatch.setattr(service_module.repository, "lock_user", failing)
        if kind == "request":
            response = client.post("/api/account/recovery/request", json={"email": "person@example.com"})
            assert response.status_code == 202
            expected = "ACCOUNT_RECOVERY_REQUEST_FAILED\n"
        else:
            response = confirm(client, token)
            assert response.status_code == 503
            expected = "ACCOUNT_RECOVERY_CONFIRM_FAILED\n"
        assert response.headers["cache-control"] == "no-store"
    output = capsys.readouterr()
    assert output.out == expected and output.err == "" and caplog.text == ""
