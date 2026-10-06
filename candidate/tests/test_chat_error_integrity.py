"""Chat error integrity: internal failures and fallback truncation.

- An unexpected exception inside the real chat() pipeline is HTTP 500
  internal_error, never a 200 assistant-style reply, and is never metered
  as a successful chat.
- A Groq or Cloudflare reply cut off at the output limit (finish_reason
  "length") carries the same incomplete contract as OpenAI's, so it is never
  presented as a complete answer.

Every provider here is fake; no network access occurs.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select

import app_fastapi_candidate as app
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_accounts.usage import UsageMeter, build_usage_router
from kalillac_db.models import AccountUsageDaily, Base

from account_test_db import make_session_factory, make_sqlite_engine


SETTINGS = AccountSettings(cookie_secure=True, session_days=14)
COOKIE = SETTINGS.cookie_name
EMAIL = "person@example.com"
PASSWORD = "correct horse battery staple"

PRIVATE_MESSAGE = "private words about my-secret-project"
EXCEPTION_DETAIL = "internal-detail token=sk-not-a-real-key at db.internal"

MESSAGES = [
    SystemMessage(content="system"),
    HumanMessage(content="write something long"),
]


# --- fixtures ----------------------------------------------------------------------


class _Boom(RuntimeError):
    pass


@pytest.fixture
def pipeline_failure(monkeypatch):
    """Make the real chat() pipeline fail after it has started."""

    def classify_request(message, history):
        raise _Boom(EXCEPTION_DETAIL)

    monkeypatch.setattr(app, "classify_request", classify_request)


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)

    yield make_session_factory(engine)

    engine.dispose()


@pytest.fixture
def client(db, monkeypatch):
    """Real app.api and real chat(), with accounts and metering switched on."""

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        app,
        "_usage_meter",
        UsageMeter(session_factory=lambda: db, settings=SETTINGS, clock=lambda: now),
    )

    original_routes = list(app.api.router.routes)
    app.api.include_router(
        build_account_router(session_factory=lambda: db, settings=SETTINGS)
    )
    app.api.include_router(
        build_usage_router(
            session_factory=lambda: db,
            settings=SETTINGS,
            clock=lambda: now,
        )
    )

    with TestClient(app.api, base_url="https://testserver") as c:
        yield c

    app.api.router.routes[:] = original_routes


def _signed_in_token(client):
    assert client.post(
        "/api/account/register",
        json={"email": EMAIL, "password": PASSWORD},
    ).status_code == 201

    response = client.post(
        "/api/account/login",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert response.status_code == 200

    token = response.cookies[COOKIE]
    client.cookies.clear()
    return token


def _chat(client, message, token=None):
    headers = {"Cookie": f"{COOKIE}={token}"} if token else {}
    return client.post(
        "/api/chat",
        json={"message": message, "history": []},
        headers=headers,
    )


def _usage_rows(db):
    with db() as session:
        return session.scalars(select(AccountUsageDaily)).all()


# --- internal chat failure ------------------------------------------------------------


def test_chat_raises_typed_error_carrying_nothing(pipeline_failure):
    with pytest.raises(app.ChatInternalError) as caught:
        app.chat(PRIVATE_MESSAGE, [], session_id="error-integrity-direct")

    error = caught.value
    assert str(error) == ""
    assert error.args == ()
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_internal_exception_returns_500_internal_error(client, pipeline_failure):
    response = _chat(client, PRIVATE_MESSAGE)

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert response.headers["cache-control"] == "no-store"


def test_internal_error_response_has_no_assistant_reply(client, pipeline_failure):
    body = _chat(client, PRIVATE_MESSAGE).json()

    assert "reply" not in body
    assert "session_id" not in body


def test_internal_error_response_leaks_no_details(client, pipeline_failure):
    text = _chat(client, PRIVATE_MESSAGE).text

    for leaked in (
        "Something went wrong",
        "Traceback",
        "_Boom",
        "internal-detail",
        "sk-not-a-real-key",
        "db.internal",
        "my-secret-project",
        PRIVATE_MESSAGE,
    ):
        assert leaked not in text


def test_internal_failure_logs_class_name_only(client, pipeline_failure, capsys):
    capsys.readouterr()
    _chat(client, PRIVATE_MESSAGE)
    output = capsys.readouterr()
    logged = output.out + output.err

    assert "ERROR: _Boom" in logged
    for leaked in ("internal-detail", "sk-not-a-real-key", "my-secret-project"):
        assert leaked not in logged


def test_internal_failure_is_not_metered(client, db, pipeline_failure):
    token = _signed_in_token(client)

    response = _chat(client, PRIVATE_MESSAGE, token=token)

    assert response.status_code == 500
    assert _usage_rows(db) == []


def test_successful_chat_is_still_metered(client, db):
    # Control for the test above: the same real pipeline and meter do record
    # a normal reply (the calculator route makes no model call).
    token = _signed_in_token(client)

    response = _chat(client, "2 + 2", token=token)

    assert response.status_code == 200
    assert response.json()["reply"] == "4"
    [row] = _usage_rows(db)
    assert row.successful_chats == 1


# --- OpenAI-only provider policy: truncation and failure ----------------------------------


def _typed(text, incomplete=False):
    return SimpleNamespace(
        content=text,
        incomplete=incomplete,
        incomplete_reason=app.OUTPUT_TOKEN_LIMIT_REASON if incomplete else None,
    )


@pytest.fixture
def no_other_network(monkeypatch):
    """Any outbound urllib request (another provider) fails the test."""

    def refuse(*args, **kwargs):
        raise AssertionError("unexpected outbound request")

    monkeypatch.setattr(app.urllib.request, "urlopen", refuse)


@pytest.fixture
def openai_down(monkeypatch, no_other_network):
    calls = []

    def fail(messages, max_tokens=None):
        calls.append(1)
        raise RuntimeError("openai unavailable")

    monkeypatch.setattr(app, "_invoke_openai", fail)
    return calls


def test_openai_failure_is_provider_unavailable_with_no_other_provider(openai_down):
    with pytest.raises(app.ModelProviderUnavailable) as caught:
        app.invoke_llm(MESSAGES)

    assert openai_down == [1]
    assert not isinstance(caught.value, app.LocalModelServiceUnavailable)


def test_openai_truncated_reply_stays_typed_incomplete(monkeypatch, no_other_network):
    monkeypatch.setattr(app, "_invoke_openai", lambda m, max_tokens=None: _typed("Partial ans", True))

    response = app.invoke_llm(MESSAGES)

    assert app.is_incomplete_model_response(response)
    assert response.incomplete_reason == app.OUTPUT_TOKEN_LIMIT_REASON
    assert response.content == "Partial ans"


def test_truncated_reply_is_marked_cut_off(monkeypatch, no_other_network):
    monkeypatch.setattr(app, "classify_request", lambda message, history: "general")
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)
    monkeypatch.setattr(
        app, "_invoke_openai", lambda m, max_tokens=None: _typed("Rivers flow because", True)
    )

    reply = app.chat("Explain rivers.", [], session_id="error-integrity-truncated")

    assert reply.startswith("Rivers flow because")
    assert app.has_incomplete_notice(reply)


def test_complete_reply_has_no_cut_off_notice(monkeypatch, no_other_network):
    monkeypatch.setattr(app, "classify_request", lambda message, history: "general")
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)
    monkeypatch.setattr(
        app, "_invoke_openai", lambda m, max_tokens=None: _typed("Rivers flow downhill.")
    )

    reply = app.chat("Explain rivers.", [], session_id="error-integrity-complete")

    assert reply == "Rivers flow downhill."
    assert not app.has_incomplete_notice(reply)


def test_openai_failure_in_chat_is_provider_unavailable(monkeypatch, openai_down):
    monkeypatch.setattr(app, "classify_request", lambda message, history: "general")
    monkeypatch.setattr(app, "requires_web_verification", lambda message, history: False)

    with pytest.raises(app.ModelProviderUnavailable):
        app.chat("Explain rivers.", [], session_id="error-integrity-openai-down")

    assert openai_down == [1]
