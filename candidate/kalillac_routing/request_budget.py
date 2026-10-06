"""Request-scoped time and call budget for one /api/chat request.

This module performs no network calls and reads no environment settings.

A RequestBudget is created per request with an explicit duration and
explicit call caps chosen by the caller; there are no built-in defaults.
It provides:

- an absolute deadline on the injected monotonic clock;
- a thread-safe cancellation flag (for example, set when the client
  request is cancelled while the worker thread keeps running);
- separate model-attempt and search-attempt allowances;
- atomic admission: before each attempt the caller asks for admission,
  which checks cancellation, then the deadline, then the remaining
  allowance, and consumes one attempt only if all three pass. An admitted
  attempt stays consumed whether it later succeeds or fails;
- the time a single call may use: the remaining time, capped by a per-call
  timeout.

Important limits:
- This object controls ADMISSION and REMAINING TIME only. It does not
  interrupt blocking I/O, cancel an in-flight provider call, or enforce a
  hard transport deadline. A call admitted just before the deadline can
  still run for as long as its own transport timeout allows; callers must
  pass call_timeout() to the transport and check the budget again before
  any further attempt.
- Cancellation is cooperative: it prevents later admissions; it does not
  stop work already running.

Nothing about the request's content is stored: no prompts, responses,
search queries, credentials, session ids, or account identifiers. The only
state is a deadline, a flag, and two counters.

Request-local propagation: budget_scope() makes a budget the current one
for the calling context, and current_budget() reads it. A context copied
while a scope is active (asyncio tasks, AnyIO/Starlette worker threads)
keeps that budget; other requests never see it.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import math
import threading
import time
from typing import Callable, Iterator


MODEL_ATTEMPT = "model"
SEARCH_ATTEMPT = "search"


class RequestBudgetError(RuntimeError):
    """Base class: the request may not start another attempt."""

    MESSAGE = "Request budget unavailable."

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class RequestCancelled(RequestBudgetError):
    """The request was cancelled; no further attempts are admitted."""

    MESSAGE = "Request was cancelled."


class RequestDeadlineExceeded(RequestBudgetError):
    """The request's deadline has been reached."""

    MESSAGE = "Request deadline exceeded."


class CallBudgetExhausted(RequestBudgetError):
    """Every allowed attempt of this kind has already been admitted."""

    MESSAGE = "Request call budget exhausted."

    def __init__(self, kind: str) -> None:
        super().__init__()
        # One of MODEL_ATTEMPT or SEARCH_ATTEMPT; never request content.
        self.kind = kind


def _positive_seconds(value: object, name: str) -> float:
    # bool is an int subclass; True must not mean "one second".
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number of seconds.")

    seconds = float(value)

    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be a positive finite number.")

    return seconds


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")

    if value <= 0:
        raise ValueError(f"{name} must be a positive integer.")

    return value


class RequestBudget:
    """Deadline, cancellation, and attempt allowances for one request.

    Safe to share between the event loop and the request's worker thread.
    """

    def __init__(
        self,
        *,
        duration_seconds: float,
        max_model_attempts: int,
        max_search_attempts: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        duration = _positive_seconds(duration_seconds, "duration_seconds")
        self._max = {
            MODEL_ATTEMPT: _positive_int(
                max_model_attempts,
                "max_model_attempts",
            ),
            SEARCH_ATTEMPT: _positive_int(
                max_search_attempts,
                "max_search_attempts",
            ),
        }
        self._used = {MODEL_ATTEMPT: 0, SEARCH_ATTEMPT: 0}
        self._clock = clock
        self._deadline = clock() + duration
        self._cancelled = threading.Event()
        self._lock = threading.Lock()

    @property
    def deadline(self) -> float:
        """Absolute deadline on the budget's monotonic clock."""

        return self._deadline

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def model_attempts(self) -> int:
        with self._lock:
            return self._used[MODEL_ATTEMPT]

    @property
    def search_attempts(self) -> int:
        with self._lock:
            return self._used[SEARCH_ATTEMPT]

    def cancel(self) -> None:
        """Refuse every later admission. Running work is not interrupted."""

        self._cancelled.set()

    def _check_open(self) -> float:
        """Raise if no attempt may start; otherwise return seconds left."""

        if self._cancelled.is_set():
            raise RequestCancelled()

        remaining = self._deadline - self._clock()

        if remaining <= 0:
            raise RequestDeadlineExceeded()

        return remaining

    def _admit(self, kind: str) -> None:
        # Check and consume under one lock so concurrent callers can never
        # admit more than the cap.
        with self._lock:
            self._check_open()

            if self._used[kind] >= self._max[kind]:
                raise CallBudgetExhausted(kind)

            self._used[kind] += 1

    def admit_model_attempt(self) -> None:
        """Consume one model attempt, or raise without consuming one."""

        self._admit(MODEL_ATTEMPT)

    def admit_search_attempt(self) -> None:
        """Consume one search attempt, or raise without consuming one."""

        self._admit(SEARCH_ATTEMPT)

    def ensure_open(self) -> None:
        """Raise RequestCancelled or RequestDeadlineExceeded if no further
        work may happen; consumes nothing. For checks after work finishes."""

        self._check_open()

    def select_call_timeout(self, per_call_timeout: float) -> tuple[float, bool]:
        """(timeout, request_deadline_selected) for the next call.

        The remaining time is observed exactly once. timeout is that
        remaining time capped by per_call_timeout; request_deadline_selected
        is True when the request deadline, not the cap, set it (a tie counts
        as the request deadline). Raises instead of returning zero or a
        negative value.
        """

        cap = _positive_seconds(per_call_timeout, "per_call_timeout")
        remaining = self._check_open()

        if remaining <= cap:
            return remaining, True

        return cap, False

    def call_timeout(self, per_call_timeout: float) -> float:
        """Seconds the next call may use: remaining time, capped by
        per_call_timeout. Always positive; raises instead of returning
        zero or a negative value.
        """

        return self.select_call_timeout(per_call_timeout)[0]


_CURRENT_BUDGET: ContextVar[RequestBudget | None] = ContextVar(
    "kalillac_request_budget",
    default=None,
)


def current_budget() -> RequestBudget | None:
    """The budget of the request this code is running for, if any."""

    return _CURRENT_BUDGET.get()


@contextmanager
def budget_scope(budget: RequestBudget) -> Iterator[RequestBudget]:
    """Make `budget` current for this context until the block exits."""

    if not isinstance(budget, RequestBudget):
        raise TypeError("budget must be a RequestBudget.")

    token = _CURRENT_BUDGET.set(budget)

    try:
        yield budget
    finally:
        _CURRENT_BUDGET.reset(token)
