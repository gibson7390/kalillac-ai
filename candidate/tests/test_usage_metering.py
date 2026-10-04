"""Aggregate usage metering.

Pinned product rules: metering is off by default; anonymous Private Session
is unaffected; signed-in accounts get numeric daily totals only; nothing
about a conversation's content is persisted; no quota is enforced; a meter
failure never fails a chat; and existing /api/chat concurrency and
cancellation behavior is preserved.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import os
import subprocess
import sys
import threading
import uuid

import pytest

os.environ.setdefault("GROQ_API_KEY", "test-not-real")
for _flag in (
    "KALILLAC_ACCOUNTS_ENABLED",
    "KALILLAC_USAGE_METERING_ENABLED",
):
    os.environ.pop(_flag, None)

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, inspect, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError, OperationalError

import app_fastapi_candidate as app
from kalillac_accounts.router import AccountSettings, build_account_router
from kalillac_accounts.usage import (
    UsageMeter,
    build_usage_router,
    request_chars_for,
    usage_metering_enabled,
)
from kalillac_db.models import (
    AccountEntitlement,
    AccountSession,
    AccountUsageDaily,
    Base,
    User,
)
from kalillac_db.repositories.entitlements import set_entitlement_tier
from kalillac_db.repositories.usage import (
    build_usage_increment,
    increment_daily_usage,
)

from account_test_db import (
    make_session_factory,
    make_sqlite_engine,
    make_sqlite_file_engine,
)


PASSWORD = "correct horse battery staple"
EMAIL = "person@example.com"
SETTINGS = AccountSettings(cookie_secure=True, session_days=14)
COOKIE = SETTINGS.cookie_name
CANDIDATE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- fixtures ----------------------------------------------------------------------


@pytest.fixture
def db():
    engine = make_sqlite_engine()
    Base.metadata.create_all(engine)

    yield make_session_factory(engine)

    engine.dispose()


@pytest.fixture
def now():
    return datetime.now(timezone.utc)


@pytest.fixture
def fake_chat(monkeypatch):
    """Controllable replacement for chat(); records what it received."""

    state = {"reply": "A fixed reply.", "raise": None, "calls": []}

    def chat(message, history, request=None, session_id=None):
        state["calls"].append(
            {"message": message, "history": history, "session_id": session_id}
        )

        if state["raise"] is not None:
            raise state["raise"]

        return state["reply"]

    monkeypatch.setattr(app, "chat", chat)
    return state


@pytest.fixture
def meter(db, now, monkeypatch):
    usage_meter = UsageMeter(
        session_factory=lambda: db,
        settings=SETTINGS,
        clock=lambda: now,
    )
    monkeypatch.setattr(app, "_usage_meter", usage_meter)
    return usage_meter


@pytest.fixture
def client(db, now, meter, fake_chat):
    """Real app.api with account + usage routes and metering switched on."""

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


def _register_and_login(client, email=EMAIL):
    assert client.post(
        "/api/account/register",
        json={"email": email, "password": PASSWORD},
    ).status_code == 201

    response = client.post(
        "/api/account/login",
        json={"email": email, "password": PASSWORD},
    )
    assert response.status_code == 200
    return response.cookies[COOKIE]


def _chat(client, message="hello there", history=None, token=None):
    headers = {"Cookie": f"{COOKIE}={token}"} if token else {}

    if token:
        client.cookies.clear()

    return client.post(
        "/api/chat",
        json={"message": message, "history": history or []},
        headers=headers,
    )


def _usage_rows(db):
    with db() as session:
        return session.scalars(select(AccountUsageDaily)).all()


def _user_id(db, email=EMAIL):
    with db() as session:
        return session.scalar(select(User.id).where(User.email == email))


# --- flag and startup ----------------------------------------------------------------


def test_flag_defaults_off():
    assert usage_metering_enabled() is False
    assert app._usage_meter is None


def _import_app_with(env_overrides):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KALILLAC_")
    }
    env.update(env_overrides)
    env.setdefault("GROQ_API_KEY", "test-not-real")

    script = (
        "import app_fastapi_candidate as a;"
        "from fastapi.testclient import TestClient;"
        "r = TestClient(a.api, base_url='https://testserver')"
        ".get('/api/account/usage');"
        "print(a._usage_meter is not None, r.status_code)"
    )

    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=CANDIDATE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


DB_ENV = {
    "KALILLAC_DB_ENABLED": "true",
    "KALILLAC_DATABASE_URL": (
        "postgresql+psycopg://user:pw@unreachable.invalid/kalillac"
    ),
}


def test_metering_without_accounts_fails_at_startup():
    result = _import_app_with({"KALILLAC_USAGE_METERING_ENABLED": "true"})

    assert result.returncode != 0
    assert (
        "KALILLAC_USAGE_METERING_ENABLED requires KALILLAC_ACCOUNTS_ENABLED"
        in result.stderr
    )


def test_metering_with_accounts_but_no_database_fails_at_startup():
    result = _import_app_with(
        {
            "KALILLAC_USAGE_METERING_ENABLED": "true",
            "KALILLAC_ACCOUNTS_ENABLED": "true",
        }
    )

    assert result.returncode != 0
    assert "requires KALILLAC_DB_ENABLED" in result.stderr


def test_metering_enabled_mounts_usage_endpoint():
    result = _import_app_with(
        {
            **DB_ENV,
            "KALILLAC_ACCOUNTS_ENABLED": "true",
            "KALILLAC_USAGE_METERING_ENABLED": "true",
        }
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "True 401"


def test_usage_endpoint_absent_when_metering_off():
    result = _import_app_with({**DB_ENV, "KALILLAC_ACCOUNTS_ENABLED": "true"})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "False 404"


def test_metering_off_chat_never_reads_cookie_or_database(
    monkeypatch,
    db,
    fake_chat,
):
    from starlette.requests import HTTPConnection

    # FastAPI itself parses cookies for every route while resolving
    # parameters; what must not happen is Kalillac code reading them.
    original_cookies = HTTPConnection.cookies
    readers = []

    def spying(self):
        readers.append(sys._getframe(1).f_code.co_filename)
        return original_cookies.fget(self)

    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(HTTPConnection, "cookies", property(spying))

    opened = []
    original_call = type(db).__call__
    monkeypatch.setattr(
        type(db),
        "__call__",
        lambda self, *a, **k: opened.append(1) or original_call(self, *a, **k),
    )

    with TestClient(app.api, base_url="https://testserver") as c:
        response = c.post(
            "/api/chat",
            json={"message": "hi", "history": []},
            headers={"Cookie": f"{COOKIE}=anything"},
        )

    assert response.status_code == 200
    assert opened == []

    # The spy did observe cookie parsing, but none from Kalillac code.
    assert readers
    assert not any(
        "app_fastapi_candidate" in reader or "kalillac_accounts" in reader
        for reader in readers
    )


def test_cookie_spy_detects_kalillac_reads(client, db, monkeypatch):
    # Control for the test above: with metering on, the same spy does see
    # Kalillac code reading the cookie.
    from starlette.requests import HTTPConnection

    original_cookies = HTTPConnection.cookies
    readers = []

    def spying(self):
        readers.append(sys._getframe(1).f_code.co_filename)
        return original_cookies.fget(self)

    monkeypatch.setattr(HTTPConnection, "cookies", property(spying))

    assert _chat(client, token="any-token").status_code == 200
    assert any("kalillac_accounts" in reader for reader in readers)


# --- what is metered -------------------------------------------------------------------


def test_anonymous_chat_creates_no_usage_rows(client, db):
    response = _chat(client)

    assert response.status_code == 200
    assert set(response.json()) == {"reply", "session_id"}
    assert _usage_rows(db) == []


def test_signed_in_chat_increments_and_counts_characters(client, db, now, fake_chat):
    token = _register_and_login(client)
    fake_chat["reply"] = "Ünïcödé reply ✓"
    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "system", "content": "dropped by normalization"},
        {"role": "user", "content": "   "},
    ]
    message = "what about this?"

    response = _chat(client, message=message, history=history, token=token)

    assert response.status_code == 200
    rows = _usage_rows(db)
    assert len(rows) == 1

    row = rows[0]
    assert row.user_id == _user_id(db)
    assert row.usage_date == now.date()
    assert row.successful_chats == 1
    # Message plus normalized user/assistant history only (system and blank
    # entries are dropped by normalization).
    assert row.request_chars == len(message) + len("first question") + len(
        "first answer"
    )
    assert row.response_chars == len("Ünïcödé reply ✓")


def test_request_chars_helper():
    history = [
        {"role": "user", "content": "abc"},
        {"role": "assistant", "content": "de"},
    ]

    assert request_chars_for("xyz!", history) == 4 + 3 + 2
    assert request_chars_for("", []) == 0


def test_same_day_chats_aggregate_into_one_row(client, db, fake_chat):
    token = _register_and_login(client)
    fake_chat["reply"] = "12345"

    for message in ("a", "bb", "ccc"):
        assert _chat(client, message=message, token=token).status_code == 200

    rows = _usage_rows(db)
    assert len(rows) == 1
    assert rows[0].successful_chats == 3
    assert rows[0].request_chars == 1 + 2 + 3
    assert rows[0].response_chars == 5 * 3


def test_free_and_paid_accounts_are_metered_identically(client, db):
    free_token = _register_and_login(client, "free@example.com")
    paid_token = _register_and_login(client, "paid@example.com")

    with db() as session, session.begin():
        set_entitlement_tier(
            session,
            _user_id(db, "paid@example.com"),
            "paid",
            source="test_internal",
        )

    for token in (free_token, paid_token):
        assert _chat(client, message="same", token=token).status_code == 200

    rows = {row.user_id: row for row in _usage_rows(db)}
    free_row = rows[_user_id(db, "free@example.com")]
    paid_row = rows[_user_id(db, "paid@example.com")]

    assert (
        free_row.successful_chats,
        free_row.request_chars,
        free_row.response_chars,
    ) == (
        paid_row.successful_chats,
        paid_row.request_chars,
        paid_row.response_chars,
    )


def test_no_plan_enforcement(client, db):
    token = _register_and_login(client)

    for _ in range(30):
        response = _chat(client, token=token)
        assert response.status_code == 200

    assert _usage_rows(db)[0].successful_chats == 30


# --- account resolution falls back to anonymous ------------------------------------


def test_forged_cookie_is_anonymous_not_a_failure(client, db):
    _register_and_login(client)

    response = _chat(client, token="forged-token-value")

    assert response.status_code == 200
    assert _usage_rows(db) == []


def test_expired_session_is_anonymous(client, db):
    token = _register_and_login(client)

    with db() as session, session.begin():
        session.execute(
            update(AccountSession).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )

    assert _chat(client, token=token).status_code == 200
    assert _usage_rows(db) == []


def test_revoked_session_is_anonymous(client, db):
    token = _register_and_login(client)

    client.post(
        "/api/account/logout",
        headers={"Cookie": f"{COOKIE}={token}"},
    )

    assert _chat(client, token=token).status_code == 200
    assert _usage_rows(db) == []


def test_inactive_user_is_anonymous(client, db):
    token = _register_and_login(client)

    with db() as session, session.begin():
        session.execute(update(User).values(is_active=False))

    assert _chat(client, token=token).status_code == 200
    assert _usage_rows(db) == []


# --- what is not metered -----------------------------------------------------------


def _signed_in_failure(client, db, request_kwargs):
    token = _register_and_login(client)
    client.cookies.clear()

    response = client.post(
        "/api/chat",
        headers={"Cookie": f"{COOKIE}={token}"},
        **request_kwargs,
    )

    assert _usage_rows(db) == []
    return response


@pytest.mark.parametrize(
    "request_kwargs, status",
    [
        ({"content": b"{not json"}, 400),
        ({"json": ["not", "an", "object"]}, 400),
        ({"json": {"message": "   ", "history": []}}, 422),
        ({"json": {"message": "x" * 5000, "history": []}}, 422),
        ({"json": {"message": "hi", "history": [{}] * 500}}, 422),
    ],
)
def test_invalid_requests_do_not_meter(client, db, request_kwargs, status):
    assert _signed_in_failure(client, db, request_kwargs).status_code == status


def test_busy_429_does_not_meter(client, db, monkeypatch):
    monkeypatch.setattr(app, "MAX_QUEUED_CHATS", 0)
    monkeypatch.setattr(app, "_chat_semaphore", asyncio.Semaphore(0))

    response = _signed_in_failure(
        client,
        db,
        {"json": {"message": "hi", "history": []}},
    )

    assert response.status_code == 429
    assert response.json() == {"error": "busy"}


def test_provider_503_does_not_meter(client, db, fake_chat):
    fake_chat["raise"] = app.ModelProviderUnavailable()

    response = _signed_in_failure(
        client,
        db,
        {"json": {"message": "hi", "history": []}},
    )

    assert response.status_code == 503


def test_internal_500_does_not_meter(client, db, fake_chat):
    fake_chat["raise"] = RuntimeError("boom")

    response = _signed_in_failure(
        client,
        db,
        {"json": {"message": "hi", "history": []}},
    )

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}


# --- meter failure never fails chat -----------------------------------------------------


def _secret_db_error():
    return OperationalError(
        "INSERT INTO kalillac.account_usage_daily secret-sql",
        {"param": "secret-param"},
        Exception("database unavailable at db.internal:5432"),
    )


def _assert_safe_diagnostic(output, expected, token):
    assert expected in output
    for leaked in (
        "secret-sql", "secret-param", "db.internal", token,
        "private words", EMAIL,
    ):
        assert leaked not in output


def test_failed_account_lookup_keeps_chat_and_records_nothing(
    client,
    db,
    meter,
    capsys,
):
    token = _register_and_login(client)

    def broken_factory():
        raise _secret_db_error()

    meter._session_factory = broken_factory

    response = _chat(client, message="private words", token=token)

    # The lookup failed, so the request is anonymous: chat still succeeds.
    assert response.status_code == 200
    assert response.json()["reply"] == "A fixed reply."
    assert _usage_rows(db) == []
    _assert_safe_diagnostic(
        capsys.readouterr().out,
        "WARN: USAGE_METER_IDENTITY_FAILED OperationalError",
        token,
    )


def test_failed_meter_write_keeps_successful_reply(
    client,
    db,
    meter,
    capsys,
    monkeypatch,
):
    import kalillac_accounts.usage as usage_module

    token = _register_and_login(client)

    def failing_increment(*args, **kwargs):
        raise _secret_db_error()

    # The account lookup works; only the aggregate write fails.
    monkeypatch.setattr(usage_module, "increment_daily_usage", failing_increment)

    response = _chat(client, message="private words", token=token)

    assert response.status_code == 200
    assert response.json()["reply"] == "A fixed reply."
    assert _usage_rows(db) == []
    _assert_safe_diagnostic(
        capsys.readouterr().out,
        "WARN: USAGE_METER_WRITE_FAILED OperationalError",
        token,
    )


# --- ordering and cancellation ----------------------------------------------------------


def test_meter_runs_after_chat_resources_are_released(client, db, meter, monkeypatch):
    token = _register_and_login(client)
    observed = []
    original = meter.record_usage

    def checking(*args):
        sem = app._get_chat_semaphore()
        observed.append(
            (dict(app._session_locks), sem._value, app._chat_waiting)
        )
        original(*args)

    monkeypatch.setattr(meter, "record_usage", checking)

    assert _chat(client, token=token).status_code == 200

    assert observed == [({}, app.MAX_CONCURRENT_CHATS, 0)]
    assert _usage_rows(db)[0].successful_chats == 1


class _FakeRequest:
    def __init__(self, body, cookies):
        self._body = body
        self.cookies = cookies

    async def json(self):
        return self._body


def test_cancelled_request_is_never_metered(client, db, meter, monkeypatch):
    token = _register_and_login(client)
    recorded = []
    monkeypatch.setattr(
        meter,
        "record_usage",
        lambda *args: recorded.append(args),
    )

    started = threading.Event()
    release = threading.Event()

    def slow_chat(message, history, request=None, session_id=None):
        started.set()
        assert release.wait(timeout=10)
        return "late reply"

    monkeypatch.setattr(app, "chat", slow_chat)
    monkeypatch.setattr(app, "_chat_semaphore", None)

    async def scenario():
        request = _FakeRequest(
            {"message": "hi", "history": []},
            {COOKIE: token},
        )
        task = asyncio.ensure_future(app.api_chat(request))

        while not started.is_set():
            await asyncio.sleep(0.01)

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # The orphaned worker still holds its slot and session lock.
        held_while_running = (
            app._get_chat_semaphore()._value == app.MAX_CONCURRENT_CHATS - 1
            and len(app._session_locks) == 1
        )

        release.set()

        for _ in range(500):
            if (
                not app._session_locks
                and app._get_chat_semaphore()._value
                == app.MAX_CONCURRENT_CHATS
            ):
                break
            await asyncio.sleep(0.01)

        return held_while_running

    held_while_running = asyncio.run(scenario())

    assert held_while_running
    assert not app._session_locks
    assert recorded == []
    assert _usage_rows(db) == []


def test_direct_success_attaches_background_meter_only(client, db, meter):
    token = _register_and_login(client)
    request = _FakeRequest({"message": "hi", "history": []}, {COOKIE: token})

    response = asyncio.run(app.api_chat(request))

    assert response.status_code == 200
    assert response.background is not None
    # Nothing is written until the response has been sent.
    assert _usage_rows(db) == []


# --- usage API -----------------------------------------------------------------------------


def test_usage_endpoint_signed_out(client):
    response = client.get("/api/account/usage")

    assert response.status_code == 401
    assert response.json() == {"error": "not_authenticated"}


def test_usage_endpoint_forged_session(client):
    client.cookies.clear()
    response = client.get(
        "/api/account/usage",
        headers={"Cookie": f"{COOKIE}=forged"},
    )

    assert response.status_code == 401


def test_usage_endpoint_zero_before_first_use(client, now):
    _register_and_login(client)

    response = client.get("/api/account/usage")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "usage": {
            "date": now.date().isoformat(),
            "successful_chats": 0,
            "request_chars": 0,
            "response_chars": 0,
        }
    }


def test_usage_endpoint_totals_after_chats(client, now, fake_chat):
    token = _register_and_login(client)
    fake_chat["reply"] = "abcd"

    _chat(client, message="12345", token=token)
    _chat(client, message="678", token=token)

    client.cookies.clear()
    response = client.get(
        "/api/account/usage",
        headers={"Cookie": f"{COOKIE}={token}"},
    )

    assert response.json() == {
        "usage": {
            "date": now.date().isoformat(),
            "successful_chats": 2,
            "request_chars": 8,
            "response_chars": 8,
        }
    }


def test_usage_contract_has_no_limits_tokens_or_money(client):
    _register_and_login(client)

    usage = client.get("/api/account/usage").json()["usage"]

    assert set(usage) == {
        "date",
        "successful_chats",
        "request_chars",
        "response_chars",
    }


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_no_http_route_modifies_counters(client, db, method):
    token = _register_and_login(client)
    _chat(client, token=token)

    response = client.request(
        method,
        "/api/account/usage",
        json={"successful_chats": 0, "request_chars": 999999},
        headers={"Cookie": f"{COOKIE}={token}"},
    )

    assert response.status_code == 405
    assert _usage_rows(db)[0].successful_chats == 1


def test_chat_body_cannot_inject_counter_values(client, db, fake_chat):
    token = _register_and_login(client)
    fake_chat["reply"] = "ok"

    client.cookies.clear()
    client.post(
        "/api/chat",
        json={
            "message": "hi",
            "history": [],
            "successful_chats": 1000,
            "request_chars": -5,
            "response_chars": 10**9,
        },
        headers={"Cookie": f"{COOKIE}={token}"},
    )

    row = _usage_rows(db)[0]
    assert (row.successful_chats, row.request_chars, row.response_chars) == (
        1,
        2,
        2,
    )


# --- existing account behavior with metering on -----------------------------------------


def test_account_and_entitlement_endpoints_unchanged(client):
    _register_and_login(client)

    assert set(client.get("/api/account/me").json()["user"]) == {
        "id",
        "email",
        "created_at",
    }
    assert client.get("/api/account/entitlements").json() == {
        "entitlements": {
            "tier": "free",
            "saved_mode_access": False,
            "higher_usage_access": False,
            "persistent_memory_access": False,
        }
    }


def test_chat_inputs_unchanged_by_metering(client, fake_chat):
    token = _register_and_login(client)

    _chat(client, message="same input", token=token)
    _chat(client, message="same input")

    signed_in, anonymous = fake_chat["calls"]
    assert signed_in["message"] == anonymous["message"]
    assert signed_in["history"] == anonymous["history"] == []
    # Each request is still its own temporary Private Session.
    assert signed_in["session_id"] != anonymous["session_id"]


def test_private_session_content_never_reaches_the_database(client, db, fake_chat):
    token = _register_and_login(client)
    fake_chat["reply"] = "MARKER-REPLY-7f3a"
    history = [
        {"role": "user", "content": "MARKER-HISTORY-USER-7f3a"},
        {"role": "assistant", "content": "MARKER-HISTORY-ASSISTANT-7f3a"},
    ]

    response = _chat(
        client,
        message="MARKER-MESSAGE-7f3a",
        history=history,
        token=token,
    )
    chat_session_id = response.json()["session_id"]

    with db() as session:
        dumped = []
        for table in Base.metadata.sorted_tables:
            for row in session.execute(select(table)).all():
                dumped.append(repr(tuple(row)))

    everything = "\n".join(dumped)
    assert "MARKER" not in everything
    assert chat_session_id not in everything
    assert _usage_rows(db)[0].successful_chats == 1


# --- database boundary ------------------------------------------------------------------


def test_usage_table_columns_are_aggregates_only():
    columns = {column.name for column in AccountUsageDaily.__table__.columns}

    assert columns == {
        "user_id",
        "usage_date",
        "successful_chats",
        "request_chars",
        "response_chars",
        "created_at",
        "updated_at",
    }
    assert [c.name for c in AccountUsageDaily.__table__.primary_key] == [
        "user_id",
        "usage_date",
    ]


def test_account_deletion_cascades_usage(client, db):
    token = _register_and_login(client)
    _chat(client, token=token)
    assert len(_usage_rows(db)) == 1

    with db() as session, session.begin():
        session.execute(delete(User))

    assert _usage_rows(db) == []


@pytest.mark.parametrize(
    "counter",
    ["successful_chats", "request_chars", "response_chars"],
)
def test_database_rejects_negative_counters(client, db, counter):
    _register_and_login(client)

    with pytest.raises(IntegrityError):
        with db() as session, session.begin():
            session.execute(
                insert(AccountUsageDaily).values(
                    user_id=_user_id(db),
                    usage_date=date(2026, 1, 1),
                    **{counter: -1},
                )
            )


def test_increment_rejects_negative_counts():
    with pytest.raises(ValueError):
        build_usage_increment(
            "postgresql",
            uuid.uuid4(),
            date(2026, 1, 1),
            request_chars=-1,
            response_chars=0,
        )


def test_postgresql_upsert_is_a_single_atomic_increment():
    statement = build_usage_increment(
        "postgresql",
        uuid.uuid4(),
        date(2026, 1, 1),
        request_chars=3,
        response_chars=4,
    )
    sql = " ".join(str(statement.compile(dialect=postgresql.dialect())).split())

    assert sql.startswith("INSERT INTO kalillac.account_usage_daily")
    assert "ON CONFLICT (user_id, usage_date) DO UPDATE SET" in sql
    # Increments are computed by the database from the stored row, so
    # concurrent upserts cannot overwrite each other.
    for counter in ("successful_chats", "request_chars", "response_chars"):
        assert (
            f"{counter} = (kalillac.account_usage_daily.{counter} "
            f"+ excluded.{counter})"
        ) in sql
    assert "SELECT" not in sql


def test_concurrent_increments_are_not_lost(tmp_path):
    engine = make_sqlite_file_engine(str(tmp_path))
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    user_id = uuid.uuid4()

    with factory() as session, session.begin():
        session.add(User(id=user_id, email="c@example.com", password_hash="h"))

    day = date(2026, 1, 1)
    threads_count, per_thread = 8, 25
    barrier = threading.Barrier(threads_count)
    errors = []

    def worker():
        try:
            barrier.wait()
            for _ in range(per_thread):
                with factory() as session, session.begin():
                    increment_daily_usage(
                        session,
                        user_id,
                        day,
                        request_chars=2,
                        response_chars=3,
                    )
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(threads_count)]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(timeout=60)

    with factory() as session:
        row = session.scalar(select(AccountUsageDaily))

    engine.dispose()

    total = threads_count * per_thread
    assert errors == []
    assert (row.successful_chats, row.request_chars, row.response_chars) == (
        total,
        2 * total,
        3 * total,
    )


# --- identity is fixed at request acceptance -------------------------------------


def _change_during_generation(monkeypatch, db, fake_chat, change):
    """Run `change` (a DB mutation) while the chat is generating."""

    def chat(message, history, request=None, session_id=None):
        with db() as session, session.begin():
            change(session)
        return fake_chat["reply"]

    monkeypatch.setattr(app, "chat", chat)


def test_logout_during_generation_still_meters_original_user(
    client, db, fake_chat, monkeypatch
):
    token = _register_and_login(client)

    def logout(session):
        session.execute(
            update(AccountSession).values(revoked_at=datetime.now(timezone.utc))
        )

    _change_during_generation(monkeypatch, db, fake_chat, logout)

    assert _chat(client, token=token).status_code == 200

    rows = _usage_rows(db)
    assert len(rows) == 1
    assert rows[0].user_id == _user_id(db)
    assert rows[0].successful_chats == 1


def test_http_logout_during_generation_still_meters_original_user(
    client, db, fake_chat, monkeypatch
):
    token = _register_and_login(client)
    logout_status = []

    def chat(message, history, request=None, session_id=None):
        # A real logout request made while the chat is still generating.
        response = client.post(
            "/api/account/logout",
            headers={"Cookie": f"{COOKIE}={token}"},
        )
        logout_status.append(response.status_code)
        return fake_chat["reply"]

    monkeypatch.setattr(app, "chat", chat)

    assert _chat(client, token=token).status_code == 200
    assert logout_status == [200]

    with db() as session:
        assert session.scalar(select(AccountSession)).revoked_at is not None

    assert _usage_rows(db)[0].successful_chats == 1


def test_expiry_during_generation_still_meters_original_user(
    client, db, fake_chat, monkeypatch
):
    token = _register_and_login(client)

    def expire(session):
        session.execute(
            update(AccountSession).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        )

    _change_during_generation(monkeypatch, db, fake_chat, expire)

    assert _chat(client, token=token).status_code == 200
    assert _usage_rows(db)[0].user_id == _user_id(db)


def test_login_during_anonymous_generation_stays_anonymous(
    client, db, fake_chat, monkeypatch, now
):
    from kalillac_accounts.tokens import hash_session_token

    _register_and_login(client)
    later_token = "token-issued-by-a-login-during-generation"

    def login(session):
        # The browser's later login creates exactly the session this cookie
        # names, but only after the request was accepted as anonymous.
        session.add(
            AccountSession(
                user_id=session.scalar(select(User.id)),
                token_hash=hash_session_token(later_token),
                expires_at=now + timedelta(days=1),
            )
        )

    _change_during_generation(monkeypatch, db, fake_chat, login)

    assert _chat(client, token=later_token).status_code == 200
    assert _usage_rows(db) == []

    # Control: the same cookie is valid for the next request, which meters.
    monkeypatch.setattr(app, "chat", lambda *a, **k: "next reply")
    assert _chat(client, token=later_token).status_code == 200
    assert _usage_rows(db)[0].successful_chats == 1


def test_identity_lookup_happens_before_chat_starts(
    client, db, fake_chat, meter, monkeypatch
):
    token = _register_and_login(client)
    order = []
    original_resolve = meter.resolve_request_account

    async def tracking(request):
        order.append("resolve")
        return await original_resolve(request)

    def chat(message, history, request=None, session_id=None):
        order.append("chat")
        return fake_chat["reply"]

    monkeypatch.setattr(meter, "resolve_request_account", tracking)
    monkeypatch.setattr(app, "chat", chat)

    assert _chat(client, token=token).status_code == 200
    assert order == ["resolve", "chat"]


def test_invalid_requests_skip_identity_lookup(client, db, meter, monkeypatch):
    token = _register_and_login(client)
    lookups = []
    monkeypatch.setattr(
        meter,
        "resolve_request_account",
        lambda request: lookups.append(1),
    )

    client.cookies.clear()
    response = client.post(
        "/api/chat",
        json={"message": "   ", "history": []},
        headers={"Cookie": f"{COOKIE}={token}"},
    )

    assert response.status_code == 422
    assert lookups == []


# --- background payload carries aggregates only -------------------------------


MARKERS = {
    "message": "PAYLOAD-MESSAGE-91c2",
    "history": "PAYLOAD-HISTORY-91c2",
    "reply": "PAYLOAD-REPLY-91c2",
}


def _reachable(root, max_depth=4):
    """Objects reachable from root through instance state, containers, and
    bound-method/closure references (module globals are not followed)."""

    import types

    seen = {}
    frontier = [root]

    for _ in range(max_depth):
        following = []

        for obj in frontier:
            if id(obj) in seen or isinstance(obj, types.ModuleType):
                continue

            seen[id(obj)] = obj

            if isinstance(obj, dict):
                following.extend(obj.keys())
                following.extend(obj.values())
            elif isinstance(obj, (list, tuple, set, frozenset)):
                following.extend(obj)
            elif isinstance(obj, types.MethodType):
                following.extend([obj.__self__, obj.__func__])
            elif isinstance(obj, types.FunctionType):
                following.extend(
                    cell.cell_contents
                    for cell in (obj.__closure__ or ())
                    if cell.cell_contents is not None
                )
                following.extend(obj.__defaults__ or ())
            elif hasattr(obj, "__dict__"):
                following.append(vars(obj))

        frontier = following

    return list(seen.values())


def test_background_task_holds_only_user_id_date_and_counts(
    client, db, meter, now, monkeypatch
):
    from starlette.requests import HTTPConnection

    from kalillac_accounts.tokens import hash_session_token

    token = _register_and_login(client)
    user_id = _user_id(db)
    captured = {}

    def capture_background_for_chat(*args):
        task = original_background_for_chat(*args)
        captured["task"] = task
        return task

    original_background_for_chat = meter.background_for_chat
    monkeypatch.setattr(meter, "background_for_chat", capture_background_for_chat)
    monkeypatch.setattr(app, "chat", lambda *a, **k: MARKERS["reply"])

    response = _chat(
        client,
        message=MARKERS["message"],
        history=[{"role": "user", "content": MARKERS["history"]}],
        token=token,
    )
    chat_session_id = response.json()["session_id"]
    task = captured["task"]

    # Exactly the bound write method plus (user_id, UTC date, counts).
    assert task.func == meter.record_usage
    assert task.args == (
        user_id,
        now.date(),
        len(MARKERS["message"]) + len(MARKERS["history"]),
        len(MARKERS["reply"]),
    )
    assert task.kwargs == {}

    forbidden_strings = {
        token,
        hash_session_token(token),
        chat_session_id,
        *MARKERS.values(),
    }

    reachable = _reachable(task)

    # Control: the walk does reach the task's real state.
    assert any(obj is meter for obj in reachable)
    assert any(obj == user_id for obj in reachable)

    for obj in reachable:
        assert not isinstance(obj, HTTPConnection), obj
        if isinstance(obj, str):
            assert obj not in forbidden_strings, obj
            assert not any(marker in obj for marker in MARKERS.values()), obj


def test_only_aggregates_reach_the_usage_write(client, db, monkeypatch):
    import kalillac_accounts.usage as usage_module

    token = _register_and_login(client)
    received = []
    original_increment = usage_module.increment_daily_usage

    def spying_increment(session, user_id, usage_date, **counts):
        received.append((user_id, usage_date, counts))
        return original_increment(session, user_id, usage_date, **counts)

    monkeypatch.setattr(usage_module, "increment_daily_usage", spying_increment)

    _chat(client, message="abc", token=token)

    [(user_id, usage_date, counts)] = received
    assert isinstance(user_id, uuid.UUID) and user_id == _user_id(db)
    assert isinstance(usage_date, date) and not isinstance(usage_date, datetime)
    assert counts == {"request_chars": 3, "response_chars": len("A fixed reply.")}


def test_anonymous_request_attaches_no_background_task(client, db, meter):
    request = _FakeRequest({"message": "hi", "history": []}, {})

    response = asyncio.run(app.api_chat(request))

    assert response.status_code == 200
    assert response.background is None


def test_invalid_cookie_at_acceptance_attaches_no_background_task(
    client, db, meter
):
    _register_and_login(client)
    request = _FakeRequest(
        {"message": "hi", "history": []},
        {COOKIE: "forged-token"},
    )

    response = asyncio.run(app.api_chat(request))

    assert response.status_code == 200
    assert response.background is None
