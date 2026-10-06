"""Request-budget settings: off by default, explicit and validated when on."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import subprocess
import sys

import pytest


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))


from kalillac_routing import request_limits
from kalillac_routing.request_limits import (
    DEADLINE_SECONDS,
    ENABLED_FLAG,
    MAX_MODEL_ATTEMPTS,
    MAX_SEARCH_ATTEMPTS,
    OPENAI_MAX_BYTES,
    QUEUE_WAIT_SECONDS,
    TAVILY_EXTRACT_MAX_BYTES,
    TAVILY_SEARCH_MAX_BYTES,
    TAVILY_TRANSPORT_BACKSTOP_GRACE_SECONDS,
    TAVILY_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS,
    TAVILY_TRANSPORT_CLEANUP_GRACE_SECONDS,
    TAVILY_TRANSPORT_CLOSE_TIMEOUT_SECONDS,
    TAVILY_TRANSPORT_DNS_THREADS,
    TAVILY_TRANSPORT_MAX_OUTSTANDING,
    TAVILY_TRANSPORT_MAX_PENDING_DNS,
    TRANSPORT_BACKSTOP_GRACE_SECONDS,
    TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS,
    TRANSPORT_CLEANUP_GRACE_SECONDS,
    TRANSPORT_CLOSE_TIMEOUT_SECONDS,
    TRANSPORT_DNS_THREADS,
    TRANSPORT_MAX_OUTSTANDING,
    TRANSPORT_MAX_PENDING_DNS,
    RequestLimitConfigError,
    RequestLimits,
    TransportLimits,
    load_request_limits,
)


VALID = {
    ENABLED_FLAG: "true",
    DEADLINE_SECONDS: "30",
    QUEUE_WAIT_SECONDS: "2.5",
    MAX_MODEL_ATTEMPTS: "6",
    MAX_SEARCH_ATTEMPTS: "4",
    TRANSPORT_MAX_OUTSTANDING: "8",
    TRANSPORT_DNS_THREADS: "2",
    TRANSPORT_MAX_PENDING_DNS: "16",
    TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS: "0.05",
    TRANSPORT_BACKSTOP_GRACE_SECONDS: "2",
    TRANSPORT_CLEANUP_GRACE_SECONDS: "1.5",
    TRANSPORT_CLOSE_TIMEOUT_SECONDS: "5",
    OPENAI_MAX_BYTES: "2097152",
    # Test values, distinct from OpenAI's so a mix-up cannot pass.
    TAVILY_TRANSPORT_MAX_OUTSTANDING: "3",
    TAVILY_TRANSPORT_DNS_THREADS: "1",
    TAVILY_TRANSPORT_MAX_PENDING_DNS: "6",
    TAVILY_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS: "0.04",
    TAVILY_TRANSPORT_BACKSTOP_GRACE_SECONDS: "1",
    TAVILY_TRANSPORT_CLEANUP_GRACE_SECONDS: "0.75",
    TAVILY_TRANSPORT_CLOSE_TIMEOUT_SECONDS: "4",
    # The selected ceilings.
    TAVILY_SEARCH_MAX_BYTES: "262144",
    TAVILY_EXTRACT_MAX_BYTES: "524288",
}
SECONDS = [
    DEADLINE_SECONDS,
    QUEUE_WAIT_SECONDS,
    TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS,
    TRANSPORT_BACKSTOP_GRACE_SECONDS,
    TRANSPORT_CLEANUP_GRACE_SECONDS,
    TRANSPORT_CLOSE_TIMEOUT_SECONDS,
    TAVILY_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS,
    TAVILY_TRANSPORT_BACKSTOP_GRACE_SECONDS,
    TAVILY_TRANSPORT_CLEANUP_GRACE_SECONDS,
    TAVILY_TRANSPORT_CLOSE_TIMEOUT_SECONDS,
]
COUNTS = [
    MAX_MODEL_ATTEMPTS,
    MAX_SEARCH_ATTEMPTS,
    TRANSPORT_MAX_OUTSTANDING,
    TRANSPORT_DNS_THREADS,
    TRANSPORT_MAX_PENDING_DNS,
    OPENAI_MAX_BYTES,
    TAVILY_TRANSPORT_MAX_OUTSTANDING,
    TAVILY_TRANSPORT_DNS_THREADS,
    TAVILY_TRANSPORT_MAX_PENDING_DNS,
    TAVILY_SEARCH_MAX_BYTES,
    TAVILY_EXTRACT_MAX_BYTES,
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("KALILLAC_"):
            monkeypatch.delenv(key)


def _set(monkeypatch, values):
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _error(monkeypatch, values):
    _set(monkeypatch, values)

    with pytest.raises(RequestLimitConfigError) as caught:
        load_request_limits()

    return caught.value


def test_disabled_by_default():
    assert load_request_limits() is None


@pytest.mark.parametrize("flag", ["", "false", "0", "off", "enabled"])
def test_disabled_flag_ignores_every_other_setting(monkeypatch, flag):
    _set(monkeypatch, {
        ENABLED_FLAG: flag,
        DEADLINE_SECONDS: "garbage-987",
        QUEUE_WAIT_SECONDS: "-1",
        MAX_MODEL_ATTEMPTS: "0",
        TRANSPORT_MAX_OUTSTANDING: "garbage-987",
        TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS: "-1",
        OPENAI_MAX_BYTES: "0",
        TAVILY_TRANSPORT_MAX_OUTSTANDING: "garbage-987",
        TAVILY_SEARCH_MAX_BYTES: "0",
        TAVILY_EXTRACT_MAX_BYTES: "-1",
    })

    assert load_request_limits() is None


def test_disabled_flag_reads_no_limit_settings(monkeypatch):
    read = []
    real_getenv = os.getenv

    def spy(name, default=None):
        read.append(name)
        return real_getenv(name, default)

    monkeypatch.setattr(request_limits.os, "getenv", spy)

    assert load_request_limits() is None
    assert read == [ENABLED_FLAG]


def test_valid_settings(monkeypatch):
    _set(monkeypatch, VALID)

    assert load_request_limits() == RequestLimits(
        deadline_seconds=30.0,
        queue_wait_seconds=2.5,
        max_model_attempts=6,
        max_search_attempts=4,
        transport=TransportLimits(
            max_outstanding=8,
            dns_threads=2,
            max_pending_dns=16,
            cancel_poll_interval_seconds=0.05,
            backstop_grace_seconds=2.0,
            cleanup_grace_seconds=1.5,
            close_timeout_seconds=5.0,
        ),
        openai_max_bytes=2097152,
        tavily_transport=TransportLimits(
            max_outstanding=3,
            dns_threads=1,
            max_pending_dns=6,
            cancel_poll_interval_seconds=0.04,
            backstop_grace_seconds=1.0,
            cleanup_grace_seconds=0.75,
            close_timeout_seconds=4.0,
        ),
        tavily_search_max_bytes=262144,
        tavily_extract_max_bytes=524288,
    )


def test_tavily_settings_never_reuse_openai_values(monkeypatch):
    _set(monkeypatch, VALID)
    limits = load_request_limits()

    assert limits.tavily_transport != limits.transport
    assert limits.tavily_search_max_bytes != limits.openai_max_bytes
    assert limits.tavily_extract_max_bytes != limits.openai_max_bytes
    assert limits.tavily_search_max_bytes != limits.tavily_extract_max_bytes


@pytest.mark.parametrize("cls", [RequestLimits, TransportLimits])
def test_no_builtin_values(cls):
    assert all(
        field.default is dataclasses.MISSING
        and field.default_factory is dataclasses.MISSING
        for field in dataclasses.fields(cls)
    )


def test_every_transport_setting_is_named_and_distinct():
    names = [
        TRANSPORT_MAX_OUTSTANDING,
        TRANSPORT_DNS_THREADS,
        TRANSPORT_MAX_PENDING_DNS,
        TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS,
        TRANSPORT_BACKSTOP_GRACE_SECONDS,
        TRANSPORT_CLEANUP_GRACE_SECONDS,
        TRANSPORT_CLOSE_TIMEOUT_SECONDS,
        OPENAI_MAX_BYTES,
    ]

    assert names == [
        "KALILLAC_TRANSPORT_MAX_OUTSTANDING",
        "KALILLAC_TRANSPORT_DNS_THREADS",
        "KALILLAC_TRANSPORT_MAX_PENDING_DNS",
        "KALILLAC_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS",
        "KALILLAC_TRANSPORT_BACKSTOP_GRACE_SECONDS",
        "KALILLAC_TRANSPORT_CLEANUP_GRACE_SECONDS",
        "KALILLAC_TRANSPORT_CLOSE_TIMEOUT_SECONDS",
        "KALILLAC_OPENAI_MAX_BYTES",
    ]
    assert set(names) <= set(SECONDS + COUNTS)


def test_every_tavily_setting_is_named_and_distinct():
    names = [
        TAVILY_TRANSPORT_MAX_OUTSTANDING,
        TAVILY_TRANSPORT_DNS_THREADS,
        TAVILY_TRANSPORT_MAX_PENDING_DNS,
        TAVILY_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS,
        TAVILY_TRANSPORT_BACKSTOP_GRACE_SECONDS,
        TAVILY_TRANSPORT_CLEANUP_GRACE_SECONDS,
        TAVILY_TRANSPORT_CLOSE_TIMEOUT_SECONDS,
        TAVILY_SEARCH_MAX_BYTES,
        TAVILY_EXTRACT_MAX_BYTES,
    ]

    assert names == [
        "KALILLAC_TAVILY_TRANSPORT_MAX_OUTSTANDING",
        "KALILLAC_TAVILY_TRANSPORT_DNS_THREADS",
        "KALILLAC_TAVILY_TRANSPORT_MAX_PENDING_DNS",
        "KALILLAC_TAVILY_TRANSPORT_CANCEL_POLL_INTERVAL_SECONDS",
        "KALILLAC_TAVILY_TRANSPORT_BACKSTOP_GRACE_SECONDS",
        "KALILLAC_TAVILY_TRANSPORT_CLEANUP_GRACE_SECONDS",
        "KALILLAC_TAVILY_TRANSPORT_CLOSE_TIMEOUT_SECONDS",
        "KALILLAC_TAVILY_SEARCH_MAX_BYTES",
        "KALILLAC_TAVILY_EXTRACT_MAX_BYTES",
    ]
    assert len(set(SECONDS + COUNTS)) == len(SECONDS + COUNTS)
    assert set(names) <= set(SECONDS + COUNTS)


@pytest.mark.parametrize("name", SECONDS + COUNTS)
def test_each_setting_is_required(monkeypatch, name):
    values = {k: v for k, v in VALID.items() if k != name}

    error = _error(monkeypatch, values)

    assert str(error) == f"{ENABLED_FLAG} is enabled but {name} is not set."


@pytest.mark.parametrize("name", SECONDS)
@pytest.mark.parametrize(
    "value",
    ["0", "0.0", "-5", "abc-987", "1e3", "inf", "nan", "+5", "5.", ".5", "1_0", "9" * 400],
)
def test_invalid_seconds_are_rejected_without_echo(monkeypatch, name, value):
    error = _error(monkeypatch, {**VALID, name: value})

    assert str(error) == f"{name} must be a positive number of seconds."
    assert value not in str(error)


@pytest.mark.parametrize("name", COUNTS)
@pytest.mark.parametrize(
    "value",
    ["0", "-1", "2.5", "abc-987", "1e3", "+3", "1_0", "٣", "9" * 5000],
)
def test_invalid_counts_are_rejected_without_echo(monkeypatch, name, value):
    error = _error(monkeypatch, {**VALID, name: value})

    assert str(error) == f"{name} must be a positive integer."
    assert value not in str(error)
    assert error.__cause__ is None


@pytest.mark.parametrize("queue_wait", ["30", "45"])
def test_queue_wait_must_be_less_than_deadline(monkeypatch, queue_wait):
    error = _error(monkeypatch, {**VALID, QUEUE_WAIT_SECONDS: queue_wait})

    assert str(error) == f"{QUEUE_WAIT_SECONDS} must be less than {DEADLINE_SECONDS}."


def _import_app(env_overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith("KALILLAC_")}
    env.update(env_overrides)
    env["GROQ_API_KEY"] = "test-not-real"

    return subprocess.run(
        [sys.executable, "-c",
         "import app_fastapi_candidate as a; print(a._request_limits)"],
        cwd=str(CANDIDATE_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_invalid_enabled_settings_refuse_startup_naming_the_setting():
    result = _import_app({**VALID, MAX_MODEL_ATTEMPTS: "abc-987"})

    assert result.returncode != 0
    assert f"{MAX_MODEL_ATTEMPTS} must be a positive integer." in result.stderr
    assert "abc-987" not in result.stderr


@pytest.mark.parametrize("name", [TAVILY_SEARCH_MAX_BYTES, TAVILY_TRANSPORT_MAX_OUTSTANDING])
def test_missing_enabled_tavily_setting_refuses_startup_naming_it(name):
    result = _import_app({k: v for k, v in VALID.items() if k != name})

    assert result.returncode != 0
    assert f"{ENABLED_FLAG} is enabled but {name} is not set." in result.stderr


def test_invalid_enabled_tavily_setting_refuses_startup_without_echo():
    result = _import_app({**VALID, TAVILY_EXTRACT_MAX_BYTES: "abc-987"})

    assert result.returncode != 0
    assert f"{TAVILY_EXTRACT_MAX_BYTES} must be a positive integer." in result.stderr
    assert "abc-987" not in result.stderr


def test_disabled_startup_ignores_leftover_settings():
    result = _import_app({DEADLINE_SECONDS: "garbage-987"})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "None"
