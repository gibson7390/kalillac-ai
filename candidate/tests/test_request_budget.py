"""Request-scoped time and call budget.

Every test uses a fake clock; concurrency tests use barriers, never sleeps.
"""

from pathlib import Path
import math
import sys
import threading

import pytest


CANDIDATE_DIR = Path(__file__).resolve().parents[1]

if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))


from kalillac_routing.request_budget import (
    MODEL_ATTEMPT,
    SEARCH_ATTEMPT,
    CallBudgetExhausted,
    RequestBudget,
    RequestBudgetError,
    RequestCancelled,
    RequestDeadlineExceeded,
)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _budget(clock, duration=30.0, models=3, searches=2):
    return RequestBudget(
        duration_seconds=duration,
        max_model_attempts=models,
        max_search_attempts=searches,
        clock=clock,
    )


# --- construction --------------------------------------------------------------------


def test_valid_construction_sets_absolute_deadline():
    clock = FakeClock(start=500.0)
    budget = _budget(clock, duration=45)

    assert budget.deadline == 545.0
    assert budget.model_attempts == 0
    assert budget.search_attempts == 0
    assert budget.cancelled is False


def test_integer_and_fractional_durations_are_accepted():
    clock = FakeClock()

    assert _budget(clock, duration=1).deadline == clock.now + 1
    assert _budget(clock, duration=0.25).deadline == clock.now + 0.25


@pytest.mark.parametrize("duration", [True, False, "30", None, [30]])
def test_non_numeric_or_bool_duration_is_a_type_error(duration):
    with pytest.raises(TypeError) as caught:
        _budget(FakeClock(), duration=duration)

    assert str(caught.value) == "duration_seconds must be a number of seconds."


@pytest.mark.parametrize(
    "duration",
    [0, 0.0, -1, -0.5, math.inf, -math.inf, math.nan],
)
def test_non_positive_or_nonfinite_duration_is_a_value_error(duration):
    with pytest.raises(ValueError) as caught:
        _budget(FakeClock(), duration=duration)

    assert str(caught.value) == "duration_seconds must be a positive finite number."


@pytest.mark.parametrize("field", ["models", "searches"])
@pytest.mark.parametrize("cap", [True, False, 1.0, 2.5, "3", None])
def test_non_integer_or_bool_cap_is_a_type_error(field, cap):
    name = "max_model_attempts" if field == "models" else "max_search_attempts"

    with pytest.raises(TypeError) as caught:
        _budget(FakeClock(), **{field: cap})

    assert str(caught.value) == f"{name} must be an integer."


@pytest.mark.parametrize("field", ["models", "searches"])
@pytest.mark.parametrize("cap", [0, -1, -100])
def test_non_positive_cap_is_a_value_error(field, cap):
    name = "max_model_attempts" if field == "models" else "max_search_attempts"

    with pytest.raises(ValueError) as caught:
        _budget(FakeClock(), **{field: cap})

    assert str(caught.value) == f"{name} must be a positive integer."


def test_construction_has_no_defaults():
    with pytest.raises(TypeError):
        RequestBudget()

    with pytest.raises(TypeError):
        RequestBudget(duration_seconds=30, max_model_attempts=3)

    with pytest.raises(TypeError):
        RequestBudget(30, 3, 2)  # keyword-only


def test_validation_messages_never_contain_the_value():
    for kwargs in (
        {"duration": -987654},
        {"duration": "987654-secret"},
        {"models": -987654},
        {"searches": 987654.5},
    ):
        with pytest.raises((TypeError, ValueError)) as caught:
            _budget(FakeClock(), **kwargs)

        assert "987654" not in str(caught.value)


# --- deadline and remaining time --------------------------------------------------------


def test_call_timeout_decreases_with_remaining_time():
    clock = FakeClock()
    budget = _budget(clock, duration=30)

    assert budget.call_timeout(100) == 30
    clock.advance(10)
    assert budget.call_timeout(100) == 20
    clock.advance(19.5)
    assert budget.call_timeout(100) == pytest.approx(0.5)


def test_call_timeout_is_capped_by_per_call_timeout():
    clock = FakeClock()
    budget = _budget(clock, duration=30)

    assert budget.call_timeout(5) == 5
    clock.advance(28)
    assert budget.call_timeout(5) == 2


def test_deadline_boundary_admits_just_before_and_refuses_at():
    clock = FakeClock()
    budget = _budget(clock, duration=10, models=5)

    # Binary-exact steps, so the boundary does not depend on float rounding.
    clock.advance(9.75)
    budget.admit_model_attempt()
    assert budget.call_timeout(60) == 0.25

    clock.advance(0.25)  # exactly at the deadline
    assert clock() == budget.deadline

    with pytest.raises(RequestDeadlineExceeded):
        budget.admit_model_attempt()

    with pytest.raises(RequestDeadlineExceeded):
        budget.call_timeout(60)

    assert budget.model_attempts == 1


def test_never_grants_zero_or_negative_timeout():
    clock = FakeClock()
    budget = _budget(clock, duration=1)

    for step in (0.5, 0.5, 0.5, 100):
        clock.advance(step)

        try:
            granted = budget.call_timeout(10)
        except RequestDeadlineExceeded:
            continue

        assert granted > 0


@pytest.mark.parametrize("timeout", [0, -1, math.inf, math.nan, True, "5", None])
def test_invalid_per_call_timeout_is_rejected(timeout):
    budget = _budget(FakeClock())

    with pytest.raises((TypeError, ValueError)):
        budget.call_timeout(timeout)


def test_refused_admission_after_deadline_consumes_nothing():
    clock = FakeClock()
    budget = _budget(clock, duration=5, models=2, searches=2)
    clock.advance(5)

    for admit in (budget.admit_model_attempt, budget.admit_search_attempt):
        with pytest.raises(RequestDeadlineExceeded):
            admit()

    assert budget.model_attempts == 0
    assert budget.search_attempts == 0


# --- call caps -------------------------------------------------------------------------


def test_model_and_search_caps_are_independent():
    budget = _budget(FakeClock(), models=2, searches=1)

    budget.admit_search_attempt()

    with pytest.raises(CallBudgetExhausted) as caught:
        budget.admit_search_attempt()

    assert caught.value.kind == SEARCH_ATTEMPT

    # Exhausted search allowance does not affect model attempts.
    budget.admit_model_attempt()
    budget.admit_model_attempt()

    with pytest.raises(CallBudgetExhausted) as caught:
        budget.admit_model_attempt()

    assert caught.value.kind == MODEL_ATTEMPT
    assert budget.model_attempts == 2
    assert budget.search_attempts == 1


def test_failed_admitted_attempts_keep_their_allowance():
    budget = _budget(FakeClock(), models=2)

    def provider_call():
        budget.admit_model_attempt()
        raise ConnectionError("provider failed")

    for _ in range(2):
        with pytest.raises(ConnectionError):
            provider_call()

    assert budget.model_attempts == 2

    with pytest.raises(CallBudgetExhausted):
        budget.admit_model_attempt()

    assert budget.model_attempts == 2


# --- cancellation ----------------------------------------------------------------------


def test_cancellation_blocks_admission_and_remaining_time():
    budget = _budget(FakeClock())
    budget.admit_model_attempt()

    budget.cancel()

    assert budget.cancelled is True

    for call in (
        budget.admit_model_attempt,
        budget.admit_search_attempt,
        lambda: budget.call_timeout(10),
    ):
        with pytest.raises(RequestCancelled):
            call()

    assert budget.model_attempts == 1
    assert budget.search_attempts == 0


def test_cancellation_is_checked_before_deadline_and_allowance():
    clock = FakeClock()
    budget = _budget(clock, duration=5, models=1)
    budget.admit_model_attempt()
    clock.advance(10)
    budget.cancel()

    with pytest.raises(RequestCancelled):
        budget.admit_model_attempt()


def test_deadline_is_checked_before_allowance():
    clock = FakeClock()
    budget = _budget(clock, duration=5, models=1)
    budget.admit_model_attempt()
    clock.advance(5)

    with pytest.raises(RequestDeadlineExceeded):
        budget.admit_model_attempt()


def test_cancel_from_another_thread_is_seen():
    budget = _budget(FakeClock())
    canceller = threading.Thread(target=budget.cancel)
    canceller.start()
    canceller.join()

    with pytest.raises(RequestCancelled):
        budget.admit_model_attempt()


# --- concurrency -----------------------------------------------------------------------


@pytest.mark.parametrize("kind", [MODEL_ATTEMPT, SEARCH_ATTEMPT])
def test_concurrent_attempts_admit_exactly_the_cap(kind):
    cap = 5
    threads = 40
    budget = _budget(FakeClock(), models=cap, searches=cap)
    admit = (
        budget.admit_model_attempt
        if kind == MODEL_ATTEMPT
        else budget.admit_search_attempt
    )
    start = threading.Barrier(threads)
    outcomes = []
    outcomes_lock = threading.Lock()

    def worker():
        start.wait()

        try:
            admit()
            result = "admitted"
        except CallBudgetExhausted:
            result = "refused"

        with outcomes_lock:
            outcomes.append(result)

    workers = [threading.Thread(target=worker) for _ in range(threads)]

    for thread in workers:
        thread.start()

    for thread in workers:
        thread.join()

    assert outcomes.count("admitted") == cap
    assert outcomes.count("refused") == threads - cap
    assert budget.model_attempts + budget.search_attempts == cap


# --- exceptions ------------------------------------------------------------------------


def test_exceptions_are_distinct_and_typed():
    assert issubclass(RequestCancelled, RequestBudgetError)
    assert issubclass(RequestDeadlineExceeded, RequestBudgetError)
    assert issubclass(CallBudgetExhausted, RequestBudgetError)
    assert len({RequestCancelled, RequestDeadlineExceeded, CallBudgetExhausted}) == 3
    assert not issubclass(RequestCancelled, RequestDeadlineExceeded)
    assert not issubclass(RequestDeadlineExceeded, CallBudgetExhausted)


def test_exception_messages_are_fixed_and_content_free():
    clock = FakeClock()
    budget = _budget(clock, duration=5, models=1, searches=1)
    raised = []

    budget.admit_model_attempt()

    for trigger in (
        budget.admit_model_attempt,
        lambda: (clock.advance(5), budget.admit_search_attempt()),
        lambda: (budget.cancel(), budget.admit_search_attempt()),
    ):
        with pytest.raises(RequestBudgetError) as caught:
            trigger()

        raised.append(caught.value)

    assert [str(error) for error in raised] == [
        "Request call budget exhausted.",
        "Request deadline exceeded.",
        "Request was cancelled.",
    ]

    for error in raised:
        assert error.__cause__ is None
        assert error.__context__ is None


def test_budget_holds_only_deadline_flag_and_counters():
    budget = _budget(FakeClock())

    assert set(vars(budget)) == {
        "_max",
        "_used",
        "_clock",
        "_deadline",
        "_cancelled",
        "_lock",
    }


# --- timeout selection with provenance (bounded provider transport) -----------------


class CountingClock(FakeClock):
    """A fake clock that counts how often it is read."""

    def __init__(self, start=1000.0):
        super().__init__(start)
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.now


def test_select_call_timeout_observes_remaining_time_exactly_once():
    clock = CountingClock()
    budget = _budget(clock, duration=30.0)
    before = clock.reads

    budget.select_call_timeout(90.0)

    assert clock.reads - before == 1


def test_select_call_timeout_cap_selected():
    clock = CountingClock()
    budget = _budget(clock, duration=30.0)
    clock.advance(5.0)                      # 25 s remain

    assert budget.select_call_timeout(10.0) == (10.0, False)


def test_select_call_timeout_request_selected():
    clock = CountingClock()
    budget = _budget(clock, duration=30.0)
    clock.advance(25.0)                     # 5 s remain

    assert budget.select_call_timeout(10.0) == (5.0, True)


def test_select_call_timeout_exact_tie_is_request_selected():
    clock = CountingClock()
    budget = _budget(clock, duration=30.0)
    clock.advance(20.0)                     # exactly 10 s remain

    assert budget.select_call_timeout(10.0) == (10.0, True)


def test_select_call_timeout_cancelled_raises_without_selecting():
    clock = CountingClock()
    budget = _budget(clock, duration=30.0)
    budget.cancel()

    with pytest.raises(RequestCancelled):
        budget.select_call_timeout(10.0)


@pytest.mark.parametrize("elapsed", [30.0, 31.0])
def test_select_call_timeout_expired_raises(elapsed):
    clock = CountingClock()
    budget = _budget(clock, duration=30.0)
    clock.advance(elapsed)

    with pytest.raises(RequestDeadlineExceeded):
        budget.select_call_timeout(10.0)


@pytest.mark.parametrize(
    "cap, error",
    [(0, ValueError), (-1.0, ValueError), (math.inf, ValueError),
     (math.nan, ValueError), (True, TypeError), ("10", TypeError), (None, TypeError)],
)
def test_select_call_timeout_validates_the_cap_before_reading_the_clock(cap, error):
    clock = CountingClock()
    budget = _budget(clock, duration=30.0)
    before = clock.reads

    with pytest.raises(error):
        budget.select_call_timeout(cap)

    assert clock.reads == before


@pytest.mark.parametrize("elapsed, cap", [(5.0, 10.0), (25.0, 10.0), (20.0, 10.0)])
def test_call_timeout_matches_select_call_timeout_with_one_read(elapsed, cap):
    clock = CountingClock()
    budget = _budget(clock, duration=30.0)
    clock.advance(elapsed)
    before = clock.reads

    timeout = budget.call_timeout(cap)

    assert clock.reads - before == 1
    assert timeout == budget.select_call_timeout(cap)[0]
