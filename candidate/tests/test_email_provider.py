"""Explicit provider selection, fail-closed startup wiring, and unchanged transaction
semantics with the Resend adapter. Injected fake transports only; nothing is contacted."""
from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from account_test_db import make_session_factory, make_sqlite_engine
from kalillac_accounts import email_provider, resend_gateway
from kalillac_accounts.email_provider import EmailProviderConfigError, build_email_gateway
from kalillac_accounts.resend_gateway import ResendGateway
from kalillac_db.models import AccountEmailVerificationToken, AccountRecoveryToken, Base, User

KEY = "re_FAKE_TEST_KEY_NOT_REAL_0000"
PROVIDER_ENV = {
    "KALILLAC_EMAIL_PROVIDER": "resend",
    "KALILLAC_RESEND_API_KEY": KEY,
    "KALILLAC_EMAIL_FROM": "help@kalillac.ai",
}
CANDIDATE = Path(__file__).resolve().parents[1]


# --- configuration -------------------------------------------------------------------


def test_valid_configuration_builds_without_any_client_or_request(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network client constructed")
    monkeypatch.setattr(resend_gateway.httpx, "Client", forbidden)
    monkeypatch.setattr(resend_gateway.httpx, "HTTPTransport", forbidden)
    gateway = build_email_gateway(PROVIDER_ENV)
    assert isinstance(gateway, ResendGateway) and repr(gateway) == "ResendGateway()"
    assert KEY not in repr(vars(gateway).keys())


@pytest.mark.parametrize("provider", [None, "", " resend", "Resend", "RESEND", "sendgrid", "smtp", "ses", "none"])
def test_provider_must_be_exactly_resend(provider):
    env = {**PROVIDER_ENV}
    env.pop("KALILLAC_EMAIL_PROVIDER")
    if provider is not None:
        env["KALILLAC_EMAIL_PROVIDER"] = provider
    with pytest.raises(EmailProviderConfigError) as error:
        build_email_gateway(env)
    assert str(error.value) == "KALILLAC_EMAIL_PROVIDER must be one of: resend."


@pytest.mark.parametrize("key", [
    KEY,                                    # a normal fake key
    "re_x",                                 # short but Bearer-safe: no entropy rule is enforced
    "re_Ab-c.d~e+f/g_h==",                  # RFC 6750 b64token punctuation beyond underscore
    "re_" + "a" * 253,                      # exactly the 256-character limit
])
def test_api_key_accepts_any_bearer_safe_re_value(key):
    assert isinstance(build_email_gateway({**PROVIDER_ENV, "KALILLAC_RESEND_API_KEY": key}), ResendGateway)


@pytest.mark.parametrize("key", [
    None, "",                               # missing, blank
    "re_",                                  # blank suffix
    "re_has space", "re_tab\there",         # whitespace
    "re_line\r\nX-Injected: 1", KEY + "\n",  # CR/LF
    "re_nul\x00byte", "re_del\x7fchar",     # control characters
    "re_" + "a" * 254,                      # 257 characters: over the limit
    "sk_live_abc", "RE_abc", "Bearer " + KEY,  # wrong prefix
    're_quote"', "re_comma,x",              # header-unsafe punctuation
])
def test_api_key_missing_or_unsafe_is_refused_without_echo(key):
    env = {**PROVIDER_ENV}
    env.pop("KALILLAC_RESEND_API_KEY")
    if key is not None:
        env["KALILLAC_RESEND_API_KEY"] = key
    with pytest.raises(EmailProviderConfigError) as error:
        build_email_gateway(env)
    assert str(error.value) == "KALILLAC_RESEND_API_KEY is missing or invalid."
    assert error.value.__cause__ is None and error.value.__context__ is None
    if key:
        assert key not in repr(error.value)


@pytest.mark.parametrize("sender", [None, "", "missing", "Kalillac <help@kalillac.ai>", "help,x@kalillac.ai",
                                    "help@kalillac", '"help"@kalillac.ai'])
def test_sender_is_refused_without_echo(sender):
    env = {**PROVIDER_ENV}
    env.pop("KALILLAC_EMAIL_FROM")
    if sender is not None:
        env["KALILLAC_EMAIL_FROM"] = sender
    with pytest.raises(EmailProviderConfigError) as error:
        build_email_gateway(env)
    assert str(error.value) == "KALILLAC_EMAIL_FROM is invalid."


def test_endpoint_is_not_configurable():
    env = {**PROVIDER_ENV, "KALILLAC_RESEND_API_URL": "https://attacker.invalid",
           "KALILLAC_RESEND_ENDPOINT": "https://attacker.invalid", "RESEND_BASE_URL": "https://attacker.invalid"}
    build_email_gateway(env)
    source = Path(email_provider.__file__).read_text() + Path(resend_gateway.__file__).read_text()
    assert "attacker" not in source and "RESEND_API_URL" not in source and "BASE_URL" not in source
    assert resend_gateway.RESEND_ENDPOINT == "https://api.resend.com/emails"


# --- application startup wiring (subprocesses, network forbidden) ------------------------

RECOVERY_ENV = {
    "KALILLAC_ACCOUNTS_ENABLED": "true",
    "KALILLAC_DB_ENABLED": "true",
    "KALILLAC_ACCOUNT_RECOVERY_URL": "https://kalillac.ai/reset",
    "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_URL": "https://kalillac.ai/verify-email",
}
PROBE = """
import sys, httpx
def forbidden(*args, **kwargs):
    raise SystemExit("PROVIDER_CONTACTED")
httpx.Client.send = forbidden
httpx.HTTPTransport.handle_request = forbidden
import app_fastapi_candidate as app
def walk(routes):
    # FastAPI 0.139 keeps included routers as nested _IncludedRouter entries.
    for route in routes:
        if hasattr(route, "original_router"):
            yield from walk(route.original_router.routes)
        else:
            yield getattr(route, "path", "")
paths = list(walk(app.api.routes))
print("RECOVERY=" + str(any("/recovery/" in path for path in paths)))
print("VERIFICATION=" + str(any("/email-verification/" in path for path in paths)))
print("PROVIDER_IMPORTED=" + str("kalillac_accounts.email_provider" in sys.modules))
print("RESEND_IMPORTED=" + str("kalillac_accounts.resend_gateway" in sys.modules))
"""


def start_app(extra):
    env = dict(os.environ)
    for name in list(env):
        if name.startswith("KALILLAC_") or name == "CREDENTIALS_DIRECTORY":
            env.pop(name)
    env.update({"OPENAI_API_KEY": "not-real", "RUN_LIVE_TESTS": "false", "HF_HUB_OFFLINE": "1", **extra})
    return subprocess.run([sys.executable, "-B", "-c", PROBE], cwd=CANDIDATE, env=env,
                          capture_output=True, text=True, timeout=180)


def flags(result):
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def test_both_features_disabled_ignore_provider_settings_and_import_nothing():
    result = start_app({"KALILLAC_EMAIL_PROVIDER": "unknown-provider", "KALILLAC_RESEND_API_KEY": "bad"})
    assert result.returncode == 0, result.stderr[-500:]
    assert flags(result) == {"RECOVERY": "False", "VERIFICATION": "False",
                             "PROVIDER_IMPORTED": "False", "RESEND_IMPORTED": "False"}


@pytest.mark.parametrize("recovery,verification", [(True, False), (False, True), (True, True)])
def test_enabled_features_mount_with_resend_and_contact_nothing(recovery, verification):
    extra = {**RECOVERY_ENV, **PROVIDER_ENV}
    if recovery:
        extra["KALILLAC_ACCOUNT_RECOVERY_ENABLED"] = "true"
    if verification:
        extra["KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED"] = "true"
    result = start_app(extra)
    assert result.returncode == 0, result.stderr[-500:]
    assert "PROVIDER_CONTACTED" not in result.stdout + result.stderr
    assert flags(result) == {"RECOVERY": str(recovery), "VERIFICATION": str(verification),
                             "PROVIDER_IMPORTED": "True", "RESEND_IMPORTED": "True"}
    assert KEY not in result.stdout + result.stderr


@pytest.mark.parametrize("feature", ["KALILLAC_ACCOUNT_RECOVERY_ENABLED", "KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED"])
@pytest.mark.parametrize("override,message", [
    ({"KALILLAC_EMAIL_PROVIDER": "sendgrid"}, "KALILLAC_EMAIL_PROVIDER must be one of: resend."),
    ({"KALILLAC_EMAIL_PROVIDER": ""}, "KALILLAC_EMAIL_PROVIDER must be one of: resend."),
    ({"KALILLAC_RESEND_API_KEY": ""}, "KALILLAC_RESEND_API_KEY is missing or invalid."),
    ({"KALILLAC_RESEND_API_KEY": "sk_live_not_a_resend_key_value"}, "KALILLAC_RESEND_API_KEY is missing or invalid."),
    ({"KALILLAC_EMAIL_FROM": "Kalillac <help@kalillac.ai>"}, "KALILLAC_EMAIL_FROM is invalid."),
])
def test_invalid_provider_settings_fail_startup_before_routes(feature, override, message):
    result = start_app({**RECOVERY_ENV, **PROVIDER_ENV, feature: "true", **override})
    assert result.returncode != 0
    assert message in result.stderr
    assert "RECOVERY=" not in result.stdout and "VERIFICATION=" not in result.stdout
    for value in override.values():
        if value:
            assert value not in result.stderr
    assert KEY not in result.stderr


# --- transaction semantics with the real adapter and a fake transport ---------------------


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


class Provider:
    """A fake Resend API: records requests; fails while `failing` is set."""

    def __init__(self):
        self.requests = []
        self.failing = False

    def __call__(self, request):
        self.requests.append(request)
        if self.failing:
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(200, json={"id": "4ef9a417-02e9-4d39-ad75-9611e0fcc33c"})

    def gateway(self):
        return ResendGateway(KEY, "help@kalillac.ai", transport_factory=lambda: httpx.MockTransport(self))

    def last_link(self):
        import json
        text = json.loads(self.requests[-1].content)["text"]
        return next(line for line in text.splitlines() if "#token=" in line)


def test_recovery_end_to_end_and_reset_rollback_on_delivery_failure(db, capsys):
    from recovery_test_support import NEW_PASSWORD, add_sessions, add_user, client_for, confirm, rows
    from kalillac_accounts.passwords import verify_password
    provider = Provider()
    user_id = add_user(db)
    add_sessions(db, user_id)
    with client_for(db, provider.gateway()) as client:
        provider.failing = True
        assert client.post("/api/account/recovery/request", json={"email": "person@example.com"}).status_code == 202
        assert rows(db, AccountRecoveryToken) == [] and len(provider.requests) == 1
        assert capsys.readouterr().out == "ACCOUNT_RECOVERY_DELIVERY_FAILED\n"
        provider.failing = False
        assert client.post("/api/account/recovery/request", json={"email": "person@example.com"}).status_code == 202
        token = provider.last_link().split("#token=", 1)[1]
        assert confirm(client, token).status_code == 200
    with db() as session:
        assert verify_password(session.get(User, user_id).password_hash, NEW_PASSWORD)
    assert len(provider.requests) == 3  # failed reset, reset, password-changed notice


def test_password_change_stays_committed_when_the_notification_fails(db, capsys):
    from recovery_test_support import NEW_PASSWORD, add_sessions, add_user, client_for, confirm, rows
    from kalillac_accounts.passwords import verify_password
    from kalillac_db.models import AccountSession
    provider = Provider()
    user_id = add_user(db)
    add_sessions(db, user_id)
    with client_for(db, provider.gateway()) as client:
        assert client.post("/api/account/recovery/request", json={"email": "person@example.com"}).status_code == 202
        token = provider.last_link().split("#token=", 1)[1]
        capsys.readouterr()
        provider.failing = True
        assert confirm(client, token).status_code == 200
    assert capsys.readouterr().out == "ACCOUNT_RECOVERY_NOTIFICATION_FAILED\n"
    with db() as session:
        assert verify_password(session.get(User, user_id).password_hash, NEW_PASSWORD)
    assert all(row["revoked_at"] is not None for row in rows(db, AccountSession))
    assert all(row["consumed_at"] is not None for row in rows(db, AccountRecoveryToken))
    assert len(provider.requests) == 2


def test_verification_end_to_end_and_issuance_rollback_on_delivery_failure(db, capsys):
    from verification_test_support import add_session, add_user, client_for, confirm, request_verification, rows
    provider = Provider()
    user_id = add_user(db)
    cookie = add_session(db, user_id)
    with client_for(db, provider.gateway()) as client:
        provider.failing = True
        assert request_verification(client, cookie).status_code == 202
        assert rows(db, AccountEmailVerificationToken) == [] and len(provider.requests) == 1
        assert capsys.readouterr().out == "ACCOUNT_EMAIL_VERIFICATION_DELIVERY_FAILED\n"
        provider.failing = False
        assert request_verification(client, cookie).status_code == 202
        token = provider.last_link().split("#token=", 1)[1]
        assert confirm(client, token).status_code == 200
    with db() as session:
        assert session.get(User, user_id).email_verified_at is not None
    assert len(provider.requests) == 2


def test_fake_gateways_remain_usable():
    from recovery_test_support import FakeEmail
    from verification_test_support import FakeVerificationEmail
    from kalillac_accounts.email_gateway import EmailGateway, EmailVerificationGateway
    assert isinstance(FakeEmail(), EmailGateway)
    assert isinstance(FakeVerificationEmail(), EmailVerificationGateway)
