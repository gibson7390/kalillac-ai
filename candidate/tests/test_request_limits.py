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
    QUEUE_WAIT_SECONDS,
    RequestLimitConfigError,
    RequestLimits,
    load_request_limits,
)


VALID = {
    ENABLED_FLAG: "true",
    DEADLINE_SECONDS: "30",
    QUEUE_WAIT_SECONDS: "2.5",
    MAX_MODEL_ATTEMPTS: "6",
    MAX_SEARCH_ATTEMPTS: "4",
}
SECONDS = [DEADLINE_SECONDS, QUEUE_WAIT_SECONDS]
COUNTS = [MAX_MODEL_ATTEMPTS, MAX_SEARCH_ATTEMPTS]


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
    )


def test_no_builtin_values():
    assert all(
        field.default is dataclasses.MISSING
        and field.default_factory is dataclasses.MISSING
        for field in dataclasses.fields(RequestLimits)
    )


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


def test_disabled_startup_ignores_leftover_settings():
    result = _import_app({DEADLINE_SECONDS: "garbage-987"})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "None"
