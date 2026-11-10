"""The final precommit sequence on the PostgreSQL path.

SQLite skips the per-phase SET LOCAL statements, so these tests drive the
PostgreSQL code path with test doubles:

* FakePgSession: a session that reports the postgresql dialect, records
  every statement, and can cancel or expire the budget from inside any
  session.execute() call;
* PgPathSession: a proxy around a real SQLite session that reports the
  postgresql dialect, records and intercepts only the SET statements, and
  passes everything else to SQLite -- so a real Space send, with its quota
  settlement, runs the PostgreSQL branch end to end.

The required sequence at the end of every Spaces transaction is

    check + remaining -> SET LOCAL lock_timeout -> check
    -> remaining -> SET LOCAL statement_timeout -> check -> COMMIT

with nothing between the last check and COMMIT. A cancellation or expiry
during either final SET LOCAL rolls the transaction back (no exchange, no
charge); one that arrives after COMMIT has begun is the uncertain outcome
the idempotency key reconciles. This is simulated PostgreSQL evidence; real
PostgreSQL behaviour is CI-only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app_fastapi_candidate as app
from kalillac_db.models import Message
from kalillac_routing.request_budget import RequestBudget, RequestCancelled, RequestDeadlineExceeded
from kalillac_spaces import repository as repo
from kalillac_spaces import router as spaces_router
from spaces_test_support import COOKIE, SETTINGS, build_app, client, headers, new_request_id
from test_spaces_chat import LEASE_SECONDS, REPLY, lease_of, new_conversation, new_space  # noqa: F401
from test_spaces_chat import provider, world  # noqa: F401  (pytest fixtures)
from test_spaces_quota import charged, slots  # noqa: F401


# --- a pure PostgreSQL-path session -----------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeTransaction:
    def __init__(self, log, on_commit=None):
        self.log, self.on_commit, self.is_active = log, on_commit, True

    def commit(self):
        self.log.append("COMMIT")
        if self.on_commit:
            self.on_commit()
        self.is_active = False

    def rollback(self):
        self.log.append("ROLLBACK")
        self.is_active = False


class FakePgSession:
    def __init__(self, log, on_execute, on_commit=None):
        self.log, self.on_execute, self.on_commit = log, on_execute, on_commit

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def begin(self):
        return FakeTransaction(self.log, self.on_commit)

    def execute(self, statement):
        sql = str(statement)
        self.log.append(sql)
        self.on_execute(sql)


def stopper(budget, clock, how):
    def fire():
        if how == "cancel":
            budget.cancel()
        else:
            clock.now += 3600
    return fire


def _run_and_log(how, which, phases):
    clock = Clock()
    budget = RequestBudget(duration_seconds=30, max_model_attempts=1, max_search_attempts=1, clock=clock)
    final = 1 + phases + 1
    log = []
    seen = []

    def on_execute(sql):
        if sql.startswith(f"SET LOCAL {which}"):
            seen.append(sql)
            if len(seen) == final:
                stopper(budget, clock, how)()

    session = FakePgSession(log, on_execute)

    def work(s, phase_bound=None):
        for _ in range(phases):
            phase_bound("phase")
        return "result"

    expected = RequestCancelled if how == "cancel" else RequestDeadlineExceeded
    error = None
    try:
        spaces_router.run_transaction(lambda: (lambda: session), work, budget, 3.0, phased=phases > 0)
    except (RequestCancelled, RequestDeadlineExceeded) as stopped:
        assert isinstance(stopped, expected)
        error = stopped
    return error, log


@pytest.mark.parametrize("phases", [0, 3])
@pytest.mark.parametrize("which", ["lock_timeout", "statement_timeout"])
@pytest.mark.parametrize("how", ["cancel", "expire"])
def test_commit_is_never_invoked_after_a_final_set_local_stop(how, which, phases):
    error, log = _run_and_log(how, which, phases)
    assert error is not None
    assert "COMMIT" not in log and log[-1] == "ROLLBACK"
    # The stop happened inside the final SET LOCAL, which was the last statement.
    assert log[-2].startswith(f"SET LOCAL {which}")


def test_the_final_sequence_is_set_check_set_check_commit():
    clock = Clock()
    events = []

    class Recording(RequestBudget):
        def ensure_open(self):
            events.append("check")
            return super().ensure_open()

        def select_call_timeout(self, cap):
            events.append("check+remaining")
            return super().select_call_timeout(cap)

    budget = Recording(duration_seconds=30, max_model_attempts=1, max_search_attempts=1, clock=clock)
    log = []

    def on_execute(sql):
        events.append(sql.split(" =")[0])

    session = FakePgSession(log, on_execute, on_commit=lambda: events.append("COMMIT"))
    assert spaces_router.run_transaction(lambda: (lambda: session), lambda s: "ok", budget, 3.0) == "ok"
    # The final bound, then COMMIT with nothing in between.
    assert events[-7:] == ["check+remaining", "SET LOCAL lock_timeout", "check", "check+remaining",
                           "SET LOCAL statement_timeout", "check", "COMMIT"]


def test_a_stop_after_commit_has_begun_is_the_uncertain_outcome():
    clock = Clock()
    budget = RequestBudget(duration_seconds=30, max_model_attempts=1, max_search_attempts=1, clock=clock)
    log = []
    session = FakePgSession(log, lambda sql: None, on_commit=budget.cancel)
    # The commit proceeds and the result is returned; the client may never
    # see it -- the same idempotency key then replays the saved exchange
    # (test_spaces_finalize.py::test_an_uncertain_commit_acknowledgement...).
    assert spaces_router.run_transaction(lambda: (lambda: session), lambda s: "ok", budget, 3.0) == "ok"
    assert log[-1] == "COMMIT" and "ROLLBACK" not in log and budget.cancelled


def test_cleanup_without_a_budget_uses_only_its_cap():
    log = []
    session = FakePgSession(log, lambda sql: None)
    assert spaces_router.run_transaction(lambda: (lambda: session), lambda s: "ok", None, 2.0) == "ok"
    assert log == ["SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
                   "SET LOCAL lock_timeout = '2000ms'", "SET LOCAL statement_timeout = '2000ms'",
                   "SET LOCAL lock_timeout = '2000ms'", "SET LOCAL statement_timeout = '2000ms'", "COMMIT"]


def test_the_router_uses_this_transaction_boundary():
    import inspect
    assert "run_transaction" in inspect.getsource(spaces_router.build_spaces_router)


# --- a real Space send on the PostgreSQL path ------------------------------------------------


class PgPathSession:
    """A real SQLite session that reports the postgresql dialect; SET
    statements are recorded and intercepted, everything else runs on SQLite."""

    def __init__(self, real, recorder):
        self._real, self._recorder = real, recorder

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def get_bind(self, *args, **kwargs):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        if sql.startswith("SET "):
            self._recorder.on_set(sql)
            return None
        return self._real.execute(statement, *args, **kwargs)

    def begin(self):
        recorder, real = self._recorder, self._real.begin()

        class Transaction:
            @property
            def is_active(self):
                return real.is_active

            def commit(self):
                recorder.commits.append(recorder.in_save)
                recorder.end_transaction()
                real.commit()

            def rollback(self):
                recorder.rollbacks.append(recorder.in_save)
                recorder.end_transaction()
                real.rollback()
        return Transaction()

    def __getattr__(self, name):
        return getattr(self._real, name)


class Recorder:
    def __init__(self):
        self.commits, self.rollbacks, self.sets = [], [], []
        self.in_save = False
        self.save_work_done = False
        self.trigger = None            # ("lock_timeout" | "statement_timeout", stop)

    def end_transaction(self):
        # Only the save transaction is marked; the next one starts clean.
        self.in_save = self.save_work_done = False

    def on_set(self, sql):
        self.sets.append((self.in_save, self.save_work_done, sql))
        if self.trigger and self.save_work_done and sql.startswith(f"SET LOCAL {self.trigger[0]}"):
            trigger, self.trigger = self.trigger, None
            trigger[1]()


@pytest.fixture
def pg_app(world, provider, monkeypatch):  # noqa: F811
    from kalillac_accounts.quota import UsageQuota
    from kalillac_accounts.usage_limits import UsageLimits
    recorder = Recorder()
    budgets = []
    monkeypatch.setattr(app, "_usage_quota", UsageQuota(
        UsageLimits(20, 40), session_factory=lambda: world.factory, settings=SETTINGS, clock=world.clock))

    original = repo.finish_send

    def finish(*args, **kwargs):
        recorder.in_save = True
        try:
            return original(*args, **kwargs)
        finally:
            recorder.save_work_done = True
    monkeypatch.setattr(repo, "finish_send", finish)

    def new_budget():
        budget = RequestBudget(duration_seconds=30, max_model_attempts=6, max_search_attempts=6)
        budgets.append(budget)
        return budget

    application = build_app(world, generate=app._space_generate, lease_seconds=LEASE_SECONDS, new_budget=new_budget)
    router = application.router.routes  # noqa: F841
    # Every Spaces transaction runs through the PostgreSQL-path proxy.
    from fastapi import FastAPI
    pg = FastAPI()
    pg.include_router(spaces_router.build_spaces_router(
        config=world.config(), cookie_name=COOKIE, generate=app._space_generate, lease_seconds=LEASE_SECONDS,
        session_factory=lambda: (lambda: PgPathSession(world.factory(), recorder)), clock=world.clock,
        new_budget=new_budget))
    return pg, recorder, budgets


def send(api, space_id, conversation_id, key=None):
    return api.post(f"/api/spaces/{space_id}/conversations/{conversation_id}/messages",
                    json={"message": "Explain how encryption uses keys.", "client_request_id": key or new_request_id()},
                    headers=headers("a"))


@pytest.mark.parametrize("which", ["lock_timeout", "statement_timeout"])
@pytest.mark.parametrize("how", ["cancel", "expire"])
def test_a_real_save_stopped_inside_a_final_set_local_saves_and_charges_nothing(world, pg_app, how, which):  # noqa: F811  (pytest fixtures)
    application, recorder, budgets = pg_app
    with client(application) as api:
        space_id = new_space(api)
        conversation_id = new_conversation(api, space_id)

        def stop():
            if how == "cancel":
                budgets[-1].cancel()
            else:
                budgets[-1]._deadline = 0.0
        recorder.trigger = (which, stop)
        response = send(api, space_id, conversation_id)
        # Checked while the app is running, so the background quota release finishes.
        assert world.count(Message) == 0 and charged(world) == 0 and set(slots(world)) <= {"released"}
    assert (response.status_code, response.json()) == (
        (499, {"error": "request_cancelled"}) if how == "cancel" else (504, {"error": "request_timeout"}))
    assert True not in recorder.commits                 # the save transaction never committed
    assert True in recorder.rollbacks                   # ... it rolled back
    assert lease_of(world, conversation_id) == (None, None)
    # The stop happened in a final SET LOCAL, after all of the save's work.
    assert any(done and sql.startswith(f"SET LOCAL {which}") for in_save, done, sql in recorder.sets)


def test_a_real_save_on_the_postgresql_path_commits_once_and_charges_once(world, pg_app):  # noqa: F811  (pytest fixtures)
    application, recorder, _ = pg_app
    with client(application) as api:
        space_id = new_space(api)
        conversation_id = new_conversation(api, space_id)
        key = new_request_id()
        assert send(api, space_id, conversation_id, key).status_code == 200
        replay = send(api, space_id, conversation_id, key)
        assert slots(world) == ["completed"]
    assert replay.status_code == 200 and replay.json()["assistant_message"]["content"] == REPLY
    assert recorder.commits.count(True) == 1
    assert world.count(Message) == 2 and charged(world) == 1
    # Every save phase re-applied fresh PostgreSQL bounds, in pairs.
    save_sets = [sql for in_save, _, sql in recorder.sets if in_save]
    kinds = [sql.split(" =")[0] for sql in save_sets]
    assert kinds[:2] == ["SET LOCAL lock_timeout", "SET LOCAL statement_timeout"]
    assert kinds == ["SET LOCAL lock_timeout", "SET LOCAL statement_timeout"] * (len(kinds) // 2)
    # finish_send's nine phase bounds (account, Space and conversation locks, authorization,
    # settlement, after settlement, after the message flush, after the Space update, before
    # commit) plus run_transaction's final bound: ten fresh pairs.
    assert len(kinds) // 2 == 10
