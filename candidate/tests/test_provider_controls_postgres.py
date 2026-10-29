"""Provider ceilings on real PostgreSQL (disposable GitHub Actions job only).

Runs only in .github/workflows/provider-ceilings-postgres.yml, against the
job's throwaway PostgreSQL 16 service container. Run decision (made at
import, see authorization_decision):
  - outside GitHub Actions without the marker: the PostgreSQL tests skip;
  - GitHub Actions with the exact marker, a GitHub-hosted Linux runner and
    this workflow: they run;
  - anything else (GitHub Actions without or with a wrong marker, a marker
    outside the expected environment): collection FAILS, so a CI run can
    never pass by skipping.

Every connection (admin, migration, application, worker) is built with
explicit host=hostaddr=127.0.0.1, port 5432, database, user, password,
bounded timeouts, sslmode/gssencmode disabled and passfile=/dev/null; any
inherited libpq environment variable (PG*) fails the run before the first
connection. After connecting, the client-side address/port and the
server-side port, database, session/current user and a per-run database
comment are verified before any database work.

It drives the committed code: the Alembic chain (as a NOINHERIT migration
login assuming the owner role), the models, the provider_usage repository
and ProviderControls with protect -> reserve -> dispatch -> release, wrapped
as the application wraps them, and the anonymous daily allowance's
AnonymousQuota service and anonymous_usage repository. Provider terminals
are content-free local mocks; nothing leaves the runner and no application
server starts.
Concurrency uses separate spawned processes, each with its own engine and
connection; every worker group is owned by a try/finally from before its
first child exists, and every child is terminated, killed if needed and
reaped with bounded waits.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import multiprocessing
import os
from pathlib import Path
import queue
import secrets
import signal
import time

import pytest

MARKER = "github-actions-service-container"
WORKFLOW_FILE = ".github/workflows/provider-ceilings-postgres.yml"
CANDIDATE_DIR = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
PORT = 5432
HEAD = "0008_account_recovery_tokens"
CHAIN = (
    "0008_account_recovery_tokens",
    "0007_anonymous_usage",
    "0006_provider_usage_pools",
    "0005_account_usage_slots",
    "0004_account_billing",
    "0003_account_usage_daily",
    "0002_account_entitlements",
    "0001_account_tables",
)
DATABASE = "ceiling_verify"
OWNER, MIGRATE, APP = "ceiling_owner", "ceiling_migrate", "ceiling_app"
ANONYMOUS_TABLES = ("anonymous_usage_days", "anonymous_usage_slots")
PROVIDERS = ("openai", "brave", "tavily")
PERIOD = 2_000_000_000  # One accounting period for the whole job: no rollover.
RACERS = 8
CHILD_SECONDS = 60
INSUFFICIENT_PRIVILEGE = "42501"
SETTINGS = ("CEILING_PG_HOST", "CEILING_PG_PORT", "CEILING_PG_ADMIN_USER",
            "CEILING_PG_ADMIN_PASSWORD", "CEILING_PG_MIGRATE_PASSWORD", "CEILING_PG_APP_PASSWORD")
STATE = {"nonce": None}  # Per-run database comment; workers receive it in their spec.
ANONYMOUS_LIMIT = 10  # The approved KALILLAC_ANONYMOUS_DAILY_LIMIT.
ANONYMOUS_RACERS = 16
ANONYMOUS_BARRIER_SECONDS = 50  # Anonymous races only; provider workers keep 30.
# One fixed UTC instant, far from midnight, for both anonymous races: the
# parent's seeding, every worker and the row assertions share one usage date.
RACE_NOW = datetime(2030, 6, 15, 12, tzinfo=timezone.utc)
VISITOR = bytes(range(1, 33))  # Synthetic 32-byte visitor token; never a real browser's.


# --- run decision (finding 2) --------------------------------------------------------------

def authorization_decision(environ):
    """("run" | "skip" | "fail", reason). Pure, unit-tested below."""
    in_actions = environ.get("GITHUB_ACTIONS") == "true"
    marker = environ.get("CEILING_PG_DISPOSABLE")
    if not in_actions:
        if marker is not None:
            return "fail", "the disposable-PostgreSQL marker is set outside GitHub Actions"
        return "skip", "runs only in the disposable GitHub Actions PostgreSQL job"
    if marker != MARKER:
        return "fail", "GitHub Actions without the exact disposable-PostgreSQL marker"
    if environ.get("RUNNER_ENVIRONMENT") != "github-hosted" or environ.get("RUNNER_OS") != "Linux":
        return "fail", "the marker is set outside a GitHub-hosted Linux runner"
    if f"/{WORKFLOW_FILE}@" not in environ.get("GITHUB_WORKFLOW_REF", ""):
        return "fail", "the marker is set outside " + WORKFLOW_FILE
    return "run", "disposable GitHub Actions PostgreSQL job"


DECISION, DECISION_REASON = authorization_decision(os.environ)
if DECISION == "fail":
    pytest.fail("provider-ceiling PostgreSQL verification refused: " + DECISION_REASON, pytrace=False)


def test_authorization_decision_logic():
    good = {"GITHUB_ACTIONS": "true", "CEILING_PG_DISPOSABLE": MARKER, "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_OS": "Linux", "GITHUB_WORKFLOW_REF": f"owner/repo/{WORKFLOW_FILE}@refs/heads/x"}
    assert authorization_decision(good)[0] == "run"
    assert authorization_decision({})[0] == "skip"
    for environ in (
            {"CEILING_PG_DISPOSABLE": MARKER},                       # Marker outside GitHub Actions.
            {"CEILING_PG_DISPOSABLE": "anything"},
            {"GITHUB_ACTIONS": "true"},                              # CI without the marker.
            dict(good, CEILING_PG_DISPOSABLE="wrong"),
            dict(good, RUNNER_ENVIRONMENT="self-hosted"),
            dict(good, RUNNER_OS="Windows"),
            dict(good, GITHUB_WORKFLOW_REF="owner/repo/.github/workflows/other.yml@refs/heads/x"),
    ):
        assert authorization_decision(environ)[0] == "fail", environ


# --- libpq constraint (finding 1) ----------------------------------------------------------

def libpq_environment_overrides(environ):
    """Every libpq environment variable starts with PG (PGHOST, PGHOSTADDR,
    PGPORT, PGDATABASE, PGUSER, PGPASSWORD, PGPASSFILE, PGSERVICE,
    PGSERVICEFILE, PGSYSCONFDIR, PGOPTIONS, PGSSL*, PGREQUIRESSL, PGGSS*,
    PGTARGETSESSIONATTRS, ...); any of them is refused."""
    return sorted(name for name in environ if name.upper().startswith("PG"))


def test_libpq_environment_overrides_are_detected():
    assert libpq_environment_overrides({"PATH": "/usr/bin", "CEILING_PG_HOST": HOST}) == []
    found = libpq_environment_overrides({n: "x" for n in (
        "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGPASSFILE", "PGPORT", "PGDATABASE", "PGUSER",
        "PGOPTIONS", "PGSSLMODE", "PGSYSCONFDIR", "pghost")})
    assert len(found) == 11


def _settings():
    missing = [name for name in SETTINGS if not os.environ.get(name)]
    if missing:
        pytest.fail("disposable PostgreSQL settings missing: " + ", ".join(missing))
    if os.environ["CEILING_PG_HOST"] != HOST or os.environ["CEILING_PG_PORT"] != str(PORT):
        pytest.fail("the disposable service container must be reached on 127.0.0.1:5432")
    overrides = libpq_environment_overrides(os.environ)
    if overrides:
        pytest.fail("inherited libpq settings refused: " + ", ".join(overrides))
    return {name: os.environ[name] for name in SETTINGS}


def _password(role):
    return {MIGRATE: os.environ["CEILING_PG_MIGRATE_PASSWORD"],
            APP: os.environ["CEILING_PG_APP_PASSWORD"]}.get(role, os.environ["CEILING_PG_ADMIN_PASSWORD"])


def _verify_connection(conn, user, dbname, current=None):
    """Client- and server-side identity before any database work. (Behind the
    service container's port mapping the server sees its own container
    address, so the address is checked on the client side; the server must
    report a TCP connection on port 5432.)"""
    info = conn.info
    assert (info.host, info.hostaddr, info.port, info.dbname, info.user) == (HOST, HOST, PORT, dbname, user), \
        "connection is not exactly 127.0.0.1:5432/" + dbname
    row = conn.execute(
        "SELECT inet_server_addr() IS NOT NULL, inet_server_port(), current_database()::text, "
        "session_user::text, current_user::text, current_setting('server_version_num')::int, "
        "(SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = current_database())"
    ).fetchone()
    assert row[:5] == (True, PORT, dbname, user, current or user), row[:5]
    assert row[5] >= 160000, "PostgreSQL 16 is required"
    if dbname == DATABASE:
        assert STATE["nonce"] and row[6] == "ceiling-verify " + STATE["nonce"], "database is not this run's"


def _connect(user, dbname=DATABASE, *, autocommit=True, application="ceiling-verify"):
    """The only way any connection is made (psycopg, SQLAlchemy, workers)."""
    import psycopg
    if libpq_environment_overrides(os.environ):
        raise AssertionError("inherited libpq settings refused")
    conn = psycopg.connect(host=HOST, hostaddr=HOST, port=PORT, dbname=dbname, user=user,
                           password=_password(user), connect_timeout=5, sslmode="disable",
                           gssencmode="disable", passfile=os.devnull, target_session_attrs="any",
                           application_name=application, autocommit=autocommit,
                           options="-c statement_timeout=10000 -c lock_timeout=8000")
    try:
        _verify_connection(conn, user, dbname)
        if not autocommit:
            conn.rollback()
    except BaseException:
        conn.close()
        raise
    return conn


def _engine(user, application="ceiling-verify", **pool):
    """SQLAlchemy engine whose every physical connection comes from _connect()."""
    from sqlalchemy import create_engine
    return create_engine("postgresql+psycopg://", hide_parameters=True,
                         creator=lambda: _connect(user, autocommit=False, application=application), **pool)


def _identity(conn):
    return tuple(conn.execute("SELECT session_user::text, current_user::text").fetchone())


def _expect_sqlstate(conn, statement, sqlstate=INSUFFICIENT_PRIVILEGE):
    try:
        with conn.transaction(force_rollback=True):
            conn.execute(statement)
    except Exception as error:
        assert getattr(error, "sqlstate", None) == sqlstate, (statement, error)
        return
    pytest.fail("unexpectedly succeeded: " + statement)


def _alembic_config(**attributes):
    from alembic.config import Config
    config = Config(str(CANDIDATE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(CANDIDATE_DIR / "migrations"))
    config.attributes.update(attributes)
    return config


# --- provisioning and exact migration ------------------------------------------------------

@pytest.fixture(scope="module")
def database():
    """Fresh roles and database in the throwaway server, then the real
    migration chain applied by the migration login assuming the owner."""
    if DECISION != "run":
        pytest.skip(DECISION_REASON)
    from psycopg import sql
    from sqlalchemy import pool
    from alembic import command
    from kalillac_db import migration_roles

    admin = _settings()["CEILING_PG_ADMIN_USER"]
    STATE["nonce"] = secrets.token_hex(16)
    ident, literal = sql.Identifier, sql.Literal
    with _connect(admin, "postgres") as conn:
        assert conn.execute("SELECT count(*) FROM pg_database WHERE datname = %s", [DATABASE]).fetchone() == (0,), \
            "expected a fresh disposable server"
        assert conn.execute("SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)",
                            [[OWNER, MIGRATE, APP]]).fetchone() == (0,), "expected a fresh disposable server"
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(ident(OWNER)))
        for role in (MIGRATE, APP):
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}").format(ident(role), literal(_password(role))))
        conn.execute(sql.SQL("GRANT {} TO {}").format(ident(OWNER), ident(MIGRATE)))
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(ident(DATABASE), ident(OWNER)))
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(ident(DATABASE)))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(ident(DATABASE), ident(MIGRATE), ident(APP)))
        conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            ident(DATABASE), literal("ceiling-verify " + STATE["nonce"])))
    with _connect(admin) as conn:
        conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        conn.execute(sql.SQL("CREATE SCHEMA kalillac AUTHORIZATION {}").format(ident(OWNER)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA kalillac TO {}").format(ident(APP)))

    saved = {name: os.environ.get(name) for name in (migration_roles.OWNER_ROLE_ENV, migration_roles.APP_ROLE_ENV)}
    os.environ[migration_roles.OWNER_ROLE_ENV] = OWNER
    os.environ[migration_roles.APP_ROLE_ENV] = APP
    migrator = _engine(MIGRATE, poolclass=pool.NullPool)  # Fresh physical connection, discarded after.
    try:
        with migrator.begin() as conn:
            raw = conn.connection.dbapi_connection
            assert _identity(raw) == (MIGRATE, MIGRATE)
            command.upgrade(_alembic_config(connection=conn), HEAD)
            assert _identity(raw) == (MIGRATE, OWNER)  # env.py assumed the owner role.
    finally:
        migrator.dispose()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    yield admin


@pytest.fixture(scope="module")
def app_engine(database):
    engine = _engine(APP, pool_size=4, max_overflow=0)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def empty_pools(request):
    if "app_engine" in request.fixturenames:
        engine = request.getfixturevalue("app_engine")
        with engine.begin() as conn:
            conn.exec_driver_sql("DELETE FROM kalillac.provider_usage_pools")
            # Slots before their visitor-days.
            conn.exec_driver_sql("DELETE FROM kalillac.anonymous_usage_slots")
            conn.exec_driver_sql("DELETE FROM kalillac.anonymous_usage_days")
    yield


# --- committed code under test, with content-free local provider mocks --------------------

class LocalReject(Exception):
    """A confirmed local rejection after admission, before dispatch."""


class ProviderFailure(Exception):
    """A provider failure after dispatch."""


def make_service(factory, total, anonymous, period=PERIOD):
    from kalillac_routing.provider_controls import ProviderControls
    from kalillac_routing.provider_limits import ControlConfig, ProviderLimit
    config = ControlConfig({p: ProviderLimit(total, anonymous, period) for p in PROVIDERS})
    return ProviderControls(config, session_factory=lambda: factory)


def mock_provider(behavior, ledger, on_event=None):
    """A content-free terminal; counts entry and dispatch only."""
    from kalillac_routing import provider_usage

    def provider(*_args, **_kwargs):
        ledger["entered"] += 1
        if on_event:
            on_event("entered")
        if behavior == "reject_before_dispatch":
            raise LocalReject()
        if behavior == "hang_before_dispatch":
            while True:
                time.sleep(1)
        provider_usage.dispatched()  # The real seam: provider_controls.dispatch().
        ledger["dispatched"] += 1
        if on_event:
            on_event("dispatched")
        if behavior == "hang_after_dispatch":
            while True:
                time.sleep(1)
        if behavior == "fail_after_dispatch":
            raise ProviderFailure()
        return {"status": "completed"}
    return provider


def _tavily_terminal(mock):
    """The committed terminal stack (app_fastapi_candidate.py L326-387):
    protect(observe_tavily(...), "tavily"); its body admits exactly one
    search attempt (L340) before the transport, here the local mock."""
    from kalillac_routing import provider_controls, provider_usage

    def terminal(budget, operation, arguments):
        budget.admit_search_attempt()
        if operation not in ("search", "extract"):
            raise ValueError("unsupported search operation")
        return mock(budget, operation, arguments)
    return provider_controls.protect(provider_usage.observe_tavily(terminal), "tavily")


def _single_search(protected_tavily):
    """The committed outer single-search stack (L441-474):
    protect(observe_single_search(...)) with provider=None. For "tavily" under
    a request budget, protect bypasses its own reservation, observe passes
    through, and the body (L444-447) delegates to the protected terminal."""
    from kalillac_routing import provider_controls, provider_usage
    from kalillac_routing.request_budget import current_budget

    def single_attempt(provider, arguments):
        budget = current_budget()
        budget.ensure_open()
        assert provider == "tavily"
        response = protected_tavily(budget, "search", arguments)
        budget.ensure_open()
        return response
    return provider_controls.protect(provider_usage.observe_single_search(single_attempt))


def attempt_detail(service, provider, anonymous, mock):
    """One request through the application's wrapper stacks:
    (outcome, search admissions, model admissions)."""
    from kalillac_routing import provider_controls, provider_usage
    from kalillac_routing.request_budget import RequestBudget, budget_scope
    previous = provider_controls.SERVICE
    provider_controls.SERVICE = service
    state, token = provider_controls.begin_request()
    budget = RequestBudget(duration_seconds=30, max_model_attempts=8, max_search_attempts=8)
    try:
        provider_controls.account_status(not anonymous)
        with budget_scope(budget):
            if provider == "openai":
                provider_controls.protect(provider_usage.observe_model(mock), "openai")({})
            elif provider == "brave":
                provider_controls.protect(provider_usage.observe_single_search(mock))("brave", {})
            elif provider == "tavily":
                _tavily_terminal(mock)(budget, "search", {})
            elif provider == "tavily-extract":
                _tavily_terminal(mock)(budget, "extract", {})
            elif provider == "tavily-delegated":
                _single_search(_tavily_terminal(mock))("tavily", {})
            else:
                raise ValueError(provider)
        outcome = "ok"
    except provider_controls.CeilingBusy:
        outcome = "busy"
    except provider_controls.CeilingUnavailable:
        outcome = "unavailable"
    except LocalReject:
        outcome = "local_reject"
    except ProviderFailure:
        outcome = "provider_failure"
    finally:
        provider_controls.end_request(state, token)
        provider_controls.SERVICE = previous
    return outcome, budget.search_attempts, budget.model_attempts


def attempt(service, provider, anonymous, mock):
    return attempt_detail(service, provider, anonymous, mock)[0]


def counts(engine, provider="openai"):
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT total_attempts, anonymous_attempts FROM kalillac.provider_usage_pools WHERE provider = %(p)s",
            {"p": provider}).first()
    return tuple(row) if row else (0, 0)


def pool_rows(engine):
    with engine.connect() as conn:
        return {r[0]: tuple(r[1:]) for r in conn.exec_driver_sql(
            "SELECT provider, period_start, period_seconds, total_limit, anonymous_limit, "
            "total_attempts, anonymous_attempts FROM kalillac.provider_usage_pools").all()}


def run_sequence(service, plan):
    """plan: (provider, anonymous, behavior, expected outcome)."""
    ledger = {"entered": 0, "dispatched": 0}
    for provider, anonymous, behavior, expected in plan:
        got = attempt(service, provider, anonymous, mock_provider(behavior, ledger))
        assert got == expected, (provider, anonymous, behavior, got)
    return ledger


def _factory(engine):
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(bind=engine, expire_on_commit=False)


# --- separate worker processes (findings 3 and 5) -----------------------------------------

def _worker(spec, barrier, results):
    """Child process: its own engine and connection, released by the barrier."""
    engine = None
    index = spec["index"]
    try:
        STATE["nonce"] = spec["nonce"]
        engine = _engine(APP, application=f"ceiling-worker-{index}", pool_size=1, max_overflow=0)
        service = make_service(_factory(engine), *spec["limits"])
        with engine.connect():
            pass  # The verified physical connection exists before the race starts.
        ledger = {"entered": 0, "dispatched": 0}
        on_event = (lambda name: results.put((index, "event", name))) if spec["events"] else None
        mock = mock_provider(spec["behavior"], ledger, on_event)
        barrier.wait(timeout=30)
        outcome = attempt(service, spec["provider"], spec["anonymous"], mock)
        results.put((index, "result", dict(ledger, outcome=outcome)))
    except BaseException as error:  # Reported to the parent, never swallowed.
        results.put((index, "error", f"{type(error).__name__}: {error}"))
        raise
    finally:
        if engine is not None:
            engine.dispose()


def _recheck_worker_environment():
    """A spawned worker repeats the parent's refusals against the environment
    it inherited: the disposable GitHub Actions decision, the disposable
    settings and the libpq override refusal (also enforced by _connect)."""
    decision, reason = authorization_decision(os.environ)
    if decision != "run":
        raise AssertionError("worker refused: " + reason)
    _settings()


def _anonymous_worker(spec, barrier, results):
    """Child process: one anonymous reservation through the committed
    AnonymousQuota, on its own engine and connection, released by the barrier."""
    engine = None
    index = spec["index"]
    try:
        STATE["nonce"] = spec["nonce"]
        _recheck_worker_environment()
        from kalillac_accounts.anonymous_quota import AnonymousQuota
        from kalillac_db.repositories.anonymous_usage import AnonymousLimitReached
        engine = _engine(APP, application=f"anonymous-worker-{index}", pool_size=1, max_overflow=0)
        factory = _factory(engine)
        now = spec["now"]
        request = AnonymousQuota(spec["limit"], session_factory=lambda: factory,
                                 clock=lambda: now).new_request(spec["token"])
        with engine.connect():
            pass  # The verified physical connection exists before the race starts.
        barrier.wait(timeout=ANONYMOUS_BARRIER_SECONDS)
        try:
            request.reserve_now()
            outcome = {"outcome": "reserved", "slot": request.lease.slot}
        except AnonymousLimitReached as refused:
            outcome = {"outcome": refused.code, "status": refused.status_code}
        results.put((index, "result", outcome))
    except BaseException as error:  # Reported to the parent, never swallowed.
        results.put((index, "error", f"{type(error).__name__}: {error}"))
        raise
    finally:
        if engine is not None:
            engine.dispose()


def _anonymous_spec(index):
    return {"index": index, "token": VISITOR, "limit": ANONYMOUS_LIMIT, "now": RACE_NOW, "nonce": STATE["nonce"]}


def _spec(index, behavior="ok", limits=(RACERS, RACERS), anonymous=False, provider="openai",
          period=PERIOD, events=False):
    return {"index": index, "behavior": behavior, "limits": (limits[0], limits[1], period),
            "anonymous": anonymous, "provider": provider, "events": events, "nonce": STATE["nonce"]}


class Workers:
    """A group of spawned children. Each child is appended BEFORE it is
    started, so close() owns every child that can exist."""

    def __init__(self):
        self.context = multiprocessing.get_context("spawn")
        self.results = self.context.Queue()
        self.barrier = None
        self.processes = []

    def start(self, specs, target=_worker):
        assert [s["index"] for s in specs] == list(range(len(specs)))
        # Owned by the group until close(): Process.start() drops its own
        # reference to args (bpo-30775), and a collected Barrier unlinks its
        # named semaphores before spawned children can unpickle them
        # (FileNotFoundError in SemLock._rebuild).
        self.barrier = self.context.Barrier(len(specs))
        for spec in specs:
            process = self.context.Process(target=target, args=(spec, self.barrier, self.results), daemon=True)
            self.processes.append(process)
            process.start()
        self.assert_sync_objects_owned()

    def assert_sync_objects_owned(self):
        """Structural guard: while any child may still unpickle or use them,
        the Barrier and the result Queue are held by this group."""
        assert self.barrier is not None and self.barrier.parties == len(self.processes), \
            "the worker barrier is not owned for the group lifetime"
        assert self.results is not None, "the worker result queue is not owned for the group lifetime"

    def next_message(self, deadline):
        self.assert_sync_objects_owned()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail("missing worker messages: time bound exceeded")
        try:
            message = self.results.get(timeout=remaining)
        except queue.Empty:
            pytest.fail("missing worker messages: time bound exceeded")
        if type(message) is not tuple or len(message) != 3:
            pytest.fail(f"malformed worker message: {message!r}")
        index, kind, payload = message
        if type(index) is not int or not 0 <= index < len(self.processes):
            pytest.fail(f"worker message with an unknown index: {message!r}")
        if kind == "error":
            pytest.fail(f"worker {index} failed: {payload}")
        if kind not in ("event", "result"):
            pytest.fail(f"unknown worker message type: {message!r}")
        return index, kind, payload

    def no_trailing_messages(self):
        try:
            message = self.results.get(timeout=0.5)
        except queue.Empty:
            return
        pytest.fail(f"trailing worker message: {message!r}")

    def results_all(self):
        """Exactly one result per worker, no events, no duplicates, no
        trailing messages, and every exit code zero."""
        deadline = time.monotonic() + CHILD_SECONDS
        collected = {}
        while len(collected) < len(self.processes):
            index, kind, payload = self.next_message(deadline)
            if kind != "result":
                pytest.fail(f"unexpected {kind} from worker {index}")
            if index in collected:
                pytest.fail(f"duplicate result from worker {index}")
            collected[index] = payload
        for process in self.processes:
            process.join(timeout=15)
            if process.exitcode != 0:
                pytest.fail(f"worker exited with {process.exitcode}")
        self.no_trailing_messages()
        return [collected[i] for i in range(len(self.processes))]

    def close(self):
        """Bounded terminate/kill/wait/reap for EVERY child, even if one step
        fails; failures are reported only after all children are handled."""
        failures = []
        for number, process in enumerate(self.processes):
            if process.pid is None:
                continue  # start() failed before the OS process existed.
            for step in ("terminate", "kill"):
                try:
                    if process.is_alive():
                        getattr(process, step)()
                        process.join(timeout=5)
                except Exception as error:
                    failures.append(f"worker {number} {step}: {type(error).__name__}: {error}")
            try:
                if process.is_alive():
                    failures.append(f"worker {number} is still alive after kill")
                else:
                    process.join(timeout=5)
                    process.close()
            except Exception as error:
                failures.append(f"worker {number} reap: {type(error).__name__}: {error}")
        try:
            self.results.cancel_join_thread()
            self.results.close()
        except Exception as error:
            failures.append(f"result queue: {type(error).__name__}: {error}")
        self.barrier = None  # Released only after every child was handled.
        if failures:
            raise AssertionError("worker cleanup failed: " + "; ".join(failures))


@contextmanager
def worker_group(specs, target=_worker):
    """Ownership starts before the first child can exist."""
    group = Workers()
    try:
        group.start(specs, target)
        yield group
    finally:
        group.close()


def race(specs, target=_worker):
    with worker_group(specs, target) as group:
        return group.results_all()


def tally(results):
    return Counter(r["outcome"] for r in results)


# --- 1 and 2: migration endpoint, identities and privileges --------------------------------

def test_alembic_has_one_head_and_migrated_exactly_to_head(database):
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(_alembic_config())
    assert script.get_heads() == [HEAD]
    assert tuple(r.revision for r in script.walk_revisions("base", "heads")) == CHAIN
    with _connect(database) as conn:
        assert conn.execute("SELECT version_num FROM kalillac.alembic_version").fetchall() == [(HEAD,)]
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'kalillac'").fetchall()}
    assert {"provider_usage_pools", *ANONYMOUS_TABLES, "account_recovery_tokens", "alembic_version"} <= tables


def test_roles_have_expected_identities_and_limited_privileges(database):
    with _connect(database) as conn:
        attributes = {r[0]: tuple(r[1:]) for r in conn.execute(
            "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolbypassrls "
            "FROM pg_roles WHERE rolname = ANY(%s)", [[OWNER, MIGRATE, APP]]).fetchall()}
        assert attributes == {OWNER: (False, False, False, False, False, False),
                              MIGRATE: (False, False, False, False, True, False),
                              APP: (False, False, False, False, True, False)}
        members = set(conn.execute(
            "SELECT g.rolname, m.rolname, a.admin_option, a.inherit_option, a.set_option FROM pg_auth_members a "
            "JOIN pg_roles g ON g.oid = a.roleid JOIN pg_roles m ON m.oid = a.member "
            "WHERE g.rolname = ANY(%s) OR m.rolname = ANY(%s)", [[OWNER, MIGRATE, APP]] * 2).fetchall())
        assert members == {(OWNER, MIGRATE, False, False, True)}
        owners = {r[0] for r in conn.execute(
            "SELECT pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'kalillac'").fetchall()}
        assert owners == {OWNER}
        table = "kalillac.provider_usage_pools"
        checks = [(APP, p, True) for p in ("SELECT", "INSERT", "UPDATE", "DELETE")]
        checks += [(APP, p + " WITH GRANT OPTION", False) for p in ("SELECT", "INSERT", "UPDATE", "DELETE")]
        checks += [(APP, p, False) for p in ("TRUNCATE", "REFERENCES", "TRIGGER")]
        checks += [(MIGRATE, p, False) for p in ("SELECT", "INSERT", "UPDATE", "DELETE")]
        for role, privilege, expected in checks:
            assert conn.execute("SELECT has_table_privilege(%s::name, %s::text, %s::text)",
                                [role, table, privilege]).fetchone() == (expected,), (role, privilege)
        # The anonymous allowance tables (0007) get the same DML-only grant.
        for name in ANONYMOUS_TABLES:
            assert conn.execute(
                "SELECT pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'kalillac' AND c.relname = %s", [name]).fetchall() == [(OWNER,)], name
            for role, privilege, expected in checks:
                assert conn.execute("SELECT has_table_privilege(%s::name, %s::text, %s::text)",
                                    [role, "kalillac." + name, privilege]).fetchone() == (expected,), (name, role, privilege)
        for role in (MIGRATE, APP):
            assert conn.execute("SELECT has_table_privilege(%s::name, 'kalillac.alembic_version'::text, 'SELECT'::text)",
                                [role]).fetchone() == (False,)
            for privilege in ("CREATE", "TEMPORARY", "CONNECT WITH GRANT OPTION"):
                assert conn.execute("SELECT has_database_privilege(%s::name, %s::text, %s::text)",
                                    [role, DATABASE, privilege]).fetchone() == (False,), (role, privilege)
            assert conn.execute("SELECT has_schema_privilege(%s::name, 'kalillac'::text, 'CREATE'::text)",
                                [role]).fetchone() == (False,)
    with _connect(APP) as conn:
        assert _identity(conn) == (APP, APP)
        for statement in ("TRUNCATE kalillac.provider_usage_pools", "CREATE TABLE kalillac.probe (x int)",
                          "CREATE TEMP TABLE probe (x int)", "SELECT version_num FROM kalillac.alembic_version",
                          f"SET ROLE {OWNER}"):
            _expect_sqlstate(conn, statement)
    with _connect(MIGRATE) as conn:
        assert _identity(conn) == (MIGRATE, MIGRATE)
        _expect_sqlstate(conn, "SELECT count(*) FROM kalillac.provider_usage_pools")  # No owner rights without SET ROLE.
        for name in ANONYMOUS_TABLES:
            _expect_sqlstate(conn, f"SELECT count(*) FROM kalillac.{name}")
        with conn.transaction(force_rollback=True):
            conn.execute(f"SET LOCAL ROLE {OWNER}")
            assert _identity(conn) == (MIGRATE, OWNER)
            assert conn.execute("SELECT version_num FROM kalillac.alembic_version").fetchone() == (HEAD,)
            for name in ANONYMOUS_TABLES:
                assert conn.execute(f"SELECT count(*) FROM kalillac.{name}").fetchone() == (0,), name
        assert _identity(conn) == (MIGRATE, MIGRATE)  # SET LOCAL ended with the rolled-back transaction.


# --- 3 to 6: concurrency, last slot, anonymous and total pools ----------------------------

def test_concurrent_first_row_creation(app_engine):
    results = race([_spec(i) for i in range(RACERS)])
    assert tally(results) == Counter(ok=RACERS)
    assert sum(r["entered"] for r in results) == sum(r["dispatched"] for r in results) == RACERS
    assert set(pool_rows(app_engine)) == {"openai"}  # Exactly one row, no duplicate-key failure.
    assert counts(app_engine) == (RACERS, 0)


def test_concurrent_first_row_creation_with_one_slot(app_engine):
    results = race([_spec(i, limits=(1, 1)) for i in range(RACERS)])
    assert tally(results) == Counter(ok=1, busy=RACERS - 1)
    assert sum(r["entered"] for r in results) == 1
    assert counts(app_engine) == (1, 0)


def test_last_slot_contention(app_engine):
    run_sequence(make_service(_factory(app_engine), 4, 4), [("openai", False, "ok", "ok")] * 3)
    results = race([_spec(i, limits=(4, 4)) for i in range(RACERS)])
    assert tally(results) == Counter(ok=1, busy=RACERS - 1)
    assert sum(r["entered"] for r in results) == sum(r["dispatched"] for r in results) == 1
    assert counts(app_engine) == (4, 0)


def test_anonymous_pool_and_total_pool(app_engine):
    service = make_service(_factory(app_engine), 3, 1)
    run_sequence(service, [("openai", True, "ok", "ok"), ("openai", True, "ok", "busy")])
    assert counts(app_engine) == (1, 1)
    run_sequence(service, [("openai", False, "ok", "ok"), ("openai", False, "ok", "ok")])
    assert counts(app_engine) == (3, 1)  # Authenticated reservations consumed only the total pool.
    run_sequence(service, [("openai", False, "ok", "busy"), ("openai", True, "ok", "busy")])
    assert counts(app_engine) == (3, 1)


def test_concurrent_mixed_anonymous_and_authenticated(app_engine):
    specs = [_spec(i, limits=(2, 1), anonymous=i % 2 == 0) for i in range(RACERS)]
    results = race(specs)
    assert tally(results) == Counter(ok=2, busy=RACERS - 2)
    anonymous_ok = sum(1 for s, r in zip(specs, results) if s["anonymous"] and r["outcome"] == "ok")
    assert anonymous_ok <= 1
    assert counts(app_engine) == (2, anonymous_ok)


# --- 7 to 9: refund, conservative charging and crashes ------------------------------------

def test_confirmed_local_rejection_before_dispatch_refunds(app_engine):
    service = make_service(_factory(app_engine), 1, 1)
    ledger = run_sequence(service, [("openai", True, "reject_before_dispatch", "local_reject"),
                                    ("openai", False, "reject_before_dispatch", "local_reject")])
    assert ledger == {"entered": 2, "dispatched": 0}
    assert counts(app_engine) == (0, 0)
    run_sequence(service, [("openai", True, "ok", "ok")])
    assert counts(app_engine) == (1, 1)


def test_dispatched_provider_failure_remains_charged(app_engine):
    service = make_service(_factory(app_engine), 2, 2)
    ledger = run_sequence(service, [("openai", True, "fail_after_dispatch", "provider_failure")])
    assert ledger == {"entered": 1, "dispatched": 1}
    assert counts(app_engine) == (1, 1)


@pytest.mark.parametrize("behavior, events", [
    ("hang_before_dispatch", ["entered"]),
    ("hang_after_dispatch", ["entered", "dispatched"]),
])
def test_crashed_worker_remains_charged(app_engine, behavior, events):
    with worker_group([_spec(0, behavior=behavior, limits=(2, 2), anonymous=True, events=True)]) as group:
        deadline = time.monotonic() + CHILD_SECONDS
        seen = []
        for _ in events:
            index, kind, payload = group.next_message(deadline)
            assert (index, kind) == (0, "event")
            seen.append(payload)
        assert seen == events
        process = group.processes[0]
        process.kill()  # A crash with unknown outcome: no release runs.
        process.join(timeout=10)
        assert process.exitcode == -signal.SIGKILL
        group.no_trailing_messages()
    end = time.monotonic() + 15
    with app_engine.connect() as conn:
        while conn.exec_driver_sql("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'ceiling-worker-0'"
                                   ).scalar_one():
            assert time.monotonic() < end, "the crashed worker's backend did not end"
            time.sleep(0.2)
            conn.rollback()
    assert counts(app_engine) == (1, 1)
    run_sequence(make_service(_factory(app_engine), 2, 2), [("openai", True, "ok", "ok"), ("openai", True, "ok", "busy")])


# --- 10 to 13: configuration, independence, Tavily delegation, persistence ----------------

MISMATCHES = [(4, 1, PERIOD), (3, 2, PERIOD), (3, 1, PERIOD - 1)]
MISMATCH_IDS = ["total_limit", "anonymous_limit", "period_seconds"]


@pytest.mark.parametrize("total, anonymous, period", MISMATCHES, ids=MISMATCH_IDS)
def test_configuration_mismatch_fails_closed_in_process(app_engine, total, anonymous, period):
    run_sequence(make_service(_factory(app_engine), 3, 1), [("openai", False, "ok", "ok")])
    before = pool_rows(app_engine)
    ledger = run_sequence(make_service(_factory(app_engine), total, anonymous, period),
                          [("openai", False, "ok", "unavailable"), ("openai", True, "ok", "unavailable")])
    assert ledger == {"entered": 0, "dispatched": 0}
    assert pool_rows(app_engine) == before


def test_configuration_mismatch_fails_closed_across_processes(app_engine):
    run_sequence(make_service(_factory(app_engine), 3, 1), [("openai", False, "ok", "ok")])  # Seeded in the parent.
    before = pool_rows(app_engine)
    assert before["openai"][4:] == (1, 0)
    specs = [_spec(i, limits=(total, anonymous), period=period, anonymous=i == 1)
             for i, (total, anonymous, period) in enumerate(MISMATCHES)]
    results = race(specs)
    assert [r["outcome"] for r in results] == ["unavailable"] * len(MISMATCHES)
    assert all((r["entered"], r["dispatched"]) == (0, 0) for r in results)
    assert pool_rows(app_engine) == before


def test_provider_pools_are_independent(app_engine):
    service = make_service(_factory(app_engine), 1, 1)
    run_sequence(service, [("openai", True, "ok", "ok"), ("openai", True, "ok", "busy"),
                           ("brave", True, "ok", "ok"), ("tavily", True, "ok", "ok"),
                           ("brave", False, "ok", "busy"), ("tavily", False, "ok", "busy")])
    assert {p: counts(app_engine, p) for p in PROVIDERS} == {p: (1, 1) for p in PROVIDERS}


@pytest.mark.parametrize("path", ["tavily-delegated", "tavily-extract"])
def test_tavily_delegation_reserves_exactly_once(app_engine, path):
    # One slot: a double reservation would be refused; a bypass would charge nothing.
    service = make_service(_factory(app_engine), 1, 1)
    ledger = {"entered": 0, "dispatched": 0}
    assert attempt_detail(service, path, True, mock_provider("ok", ledger)) == ("ok", 1, 0)
    assert ledger == {"entered": 1, "dispatched": 1}
    assert {p: counts(app_engine, p) for p in PROVIDERS} == {"openai": (0, 0), "brave": (0, 0), "tavily": (1, 1)}
    # Rejection after the pool is full: zero terminal entry, dispatch and admission.
    assert attempt_detail(service, path, True, mock_provider("ok", ledger)) == ("busy", 0, 0)
    assert ledger == {"entered": 1, "dispatched": 1}
    assert counts(app_engine, "tavily") == (1, 1)


@pytest.mark.parametrize("path", ["tavily-delegated", "tavily-extract"])
def test_tavily_delegation_refunds_and_keeps_charges(app_engine, path):
    service = make_service(_factory(app_engine), 2, 2)
    ledger = {"entered": 0, "dispatched": 0}
    assert attempt_detail(service, path, True, mock_provider("reject_before_dispatch", ledger)) \
        == ("local_reject", 1, 0)
    assert ledger == {"entered": 1, "dispatched": 0}
    assert counts(app_engine, "tavily") == (0, 0)  # Confirmed pre-dispatch refund.
    assert attempt_detail(service, path, True, mock_provider("fail_after_dispatch", ledger)) \
        == ("provider_failure", 1, 0)
    assert ledger == {"entered": 2, "dispatched": 1}
    assert counts(app_engine, "tavily") == (1, 1)  # Dispatched failure stays charged.
    assert attempt_detail(service, path, True, mock_provider("ok", ledger)) == ("ok", 1, 0)
    assert counts(app_engine, "tavily") == (2, 2)
    assert attempt_detail(service, path, True, mock_provider("ok", ledger)) == ("busy", 0, 0)
    assert ledger == {"entered": 3, "dispatched": 2}


def test_counters_persist_across_closed_connections_and_a_new_engine(app_engine):
    first = _engine(APP)
    try:
        run_sequence(make_service(_factory(first), 2, 1), [("openai", True, "ok", "ok"), ("brave", False, "ok", "ok")])
        before = pool_rows(first)
    finally:
        first.dispose()  # Every connection of that engine is closed.
    second = _engine(APP)
    try:
        assert pool_rows(second) == before
        assert before["openai"][4:] == (1, 1) and before["brave"][4:] == (1, 0)
        run_sequence(make_service(_factory(second), 2, 1),
                     [("openai", True, "ok", "busy"), ("openai", False, "ok", "ok"), ("openai", False, "ok", "busy")])
        assert counts(second) == (2, 1)
    finally:
        second.dispose()


def test_rejected_requests_never_enter_the_provider_mock_or_dispatch(app_engine):
    service = make_service(_factory(app_engine), 1, 1)
    run_sequence(service, [(p, True, "ok", "ok") for p in PROVIDERS])
    ledger = run_sequence(service, [(p, anonymous, "ok", "busy") for p in PROVIDERS for anonymous in (True, False)])
    assert ledger == {"entered": 0, "dispatched": 0}
    results = race([_spec(i, limits=(1, 1)) for i in range(3)])
    assert tally(results) == Counter(busy=3)
    assert sum(r["entered"] + r["dispatched"] for r in results) == 0


# --- 14 to 18: anonymous daily allowance (0007) ---------------------------------------------

ANONYMOUS_COLUMNS = {
    "anonymous_usage_days": ["visitor_digest", "usage_date"],
    "anonymous_usage_slots": ["visitor_digest", "usage_date", "slot", "expires_at", "state", "completed_date"],
}


def anonymous_rows(engine):
    """(visitor-days, slots) as stored, ordered."""
    with engine.connect() as conn:
        days = conn.exec_driver_sql(
            "SELECT visitor_digest, usage_date FROM kalillac.anonymous_usage_days "
            "ORDER BY usage_date, visitor_digest").all()
        slots = conn.exec_driver_sql(
            "SELECT visitor_digest, usage_date, slot, state, completed_date FROM kalillac.anonymous_usage_slots "
            "ORDER BY usage_date, visitor_digest, slot").all()
    return [tuple(r) for r in days], [tuple(r) for r in slots]


def anonymous_quota(engine, clock=None, limit=ANONYMOUS_LIMIT, **options):
    from kalillac_accounts.anonymous_quota import AnonymousQuota
    factory = _factory(engine)
    return AnonymousQuota(limit, session_factory=lambda: factory, clock=clock, **options)


def anonymous_reserved(service, token=VISITOR):
    request = service.new_request(token)
    request.reserve_now()
    return request


def test_anonymous_first_row_race_admits_exactly_the_limit(app_engine):
    results = race([_anonymous_spec(i) for i in range(ANONYMOUS_RACERS)], _anonymous_worker)
    assert Counter(r["outcome"] for r in results) == Counter(
        reserved=ANONYMOUS_LIMIT, anonymous_daily_limit_reached=ANONYMOUS_RACERS - ANONYMOUS_LIMIT)
    assert all(r["status"] == 429 for r in results if r["outcome"] != "reserved")
    assert sorted(r["slot"] for r in results if r["outcome"] == "reserved") == list(range(ANONYMOUS_LIMIT))
    from kalillac_accounts.anonymous_quota import day_digest
    digest, usage_date = day_digest(VISITOR, RACE_NOW.date()), RACE_NOW.date()
    days, slots = anonymous_rows(app_engine)
    assert days == [(digest, usage_date)]  # Exactly one visitor-day row, no duplicate-key failure.
    assert slots == [(digest, usage_date, n, "pending", None) for n in range(ANONYMOUS_LIMIT)]


def test_anonymous_last_slot_contention(app_engine):
    from kalillac_accounts.anonymous_quota import day_digest
    seeded = anonymous_quota(app_engine, clock=lambda: RACE_NOW)
    for _ in range(ANONYMOUS_LIMIT - 1):
        anonymous_reserved(seeded)  # Nine consuming pending slots at the race instant.
    results = race([_anonymous_spec(i) for i in range(RACERS)], _anonymous_worker)
    assert Counter(r["outcome"] for r in results) == Counter(
        reserved=1, anonymous_daily_limit_reached=RACERS - 1)
    assert [r["slot"] for r in results if r["outcome"] == "reserved"] == [ANONYMOUS_LIMIT - 1]
    digest, usage_date = day_digest(VISITOR, RACE_NOW.date()), RACE_NOW.date()
    days, slots = anonymous_rows(app_engine)
    assert days == [(digest, usage_date)]
    assert slots == [(digest, usage_date, n, "pending", None) for n in range(ANONYMOUS_LIMIT)]


def test_anonymous_midnight_rollover_prepares_under_the_new_day(app_engine, monkeypatch):
    import kalillac_accounts.anonymous_quota as service_module
    from kalillac_accounts.anonymous_quota import day_digest
    old_day, new_day = date(2030, 1, 1), date(2030, 1, 2)
    clock = {"now": datetime(2030, 1, 1, 23, 59, 59, tzinfo=timezone.utc)}
    service = anonymous_quota(app_engine, clock=lambda: clock["now"])
    request = anonymous_reserved(service)
    old = request.lease
    assert (old.visitor_digest, old.usage_date) == (day_digest(VISITOR, old_day), old_day)

    seen = {}
    real = service_module.roll_over_anonymous_slot

    def observed_roll_over(*args):
        replacement = real(*args)
        # Inside the transaction, before commit: the request still holds the
        # old lease, and another connection sees neither change yet.
        seen["lease_before_commit"] = request.lease
        with _connect(APP) as other:
            seen["visible_before_commit"] = other.execute(
                "SELECT usage_date, state FROM kalillac.anonymous_usage_slots ORDER BY usage_date").fetchall()
        return replacement
    monkeypatch.setattr(service_module, "roll_over_anonymous_slot", observed_roll_over)

    clock["now"] = datetime(2030, 1, 2, 0, 0, 1, tzinfo=timezone.utc)
    assert request.complete_now() == new_day
    new = request.lease
    assert seen == {"lease_before_commit": old, "visible_before_commit": [(old_day, "pending")]}
    assert new is not old and not request.holds_token
    assert (new.visitor_digest, new.usage_date) == (day_digest(VISITOR, new_day), new_day)
    assert new.visitor_digest != old.visitor_digest
    days, slots = anonymous_rows(app_engine)
    assert days == [(old.visitor_digest, old_day), (new.visitor_digest, new_day)]
    assert slots == [(old.visitor_digest, old_day, 0, "released", None),
                     (new.visitor_digest, new_day, 0, "ready", new_day)]
    assert request.record_sent_now() == new_day
    assert anonymous_rows(app_engine)[1][1] == (new.visitor_digest, new_day, 0, "completed", new_day)
    # No stored cross-day identifier or mapping: the anonymous schema is
    # unchanged and every row carries only its own day's digest.
    with app_engine.connect() as conn:
        columns = {}
        for table, column in conn.exec_driver_sql(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'kalillac' AND table_name LIKE 'anonymous%%' "
                "ORDER BY table_name, ordinal_position").all():
            columns.setdefault(table, []).append(column)
    assert columns == ANONYMOUS_COLUMNS
    days, slots = anonymous_rows(app_engine)
    assert all(row[0] == day_digest(VISITOR, row[1]) for row in days + slots)


def test_anonymous_expiry_recovers_pending_and_keeps_ready_charged(app_engine):
    from kalillac_db.repositories.anonymous_usage import AnonymousLimitReached
    clock = {"now": datetime(2030, 3, 1, 12, tzinfo=timezone.utc)}
    service = anonymous_quota(app_engine, clock=lambda: clock["now"], limit=3, ttl=timedelta(seconds=5))
    pending = anonymous_reserved(service)   # Crashes before preparation.
    ready = anonymous_reserved(service)
    ready.complete_now()                    # Crashes after preparation, before settlement.
    completed = anonymous_reserved(service)
    completed.complete_now()
    completed.record_sent_now()
    with pytest.raises(AnonymousLimitReached):
        anonymous_reserved(service)
    clock["now"] += timedelta(seconds=5)
    replacement = anonymous_reserved(service)  # Recovery frees only the expired pending slot.
    assert replacement.lease.slot == pending.lease.slot
    assert replacement.lease.expires_at > pending.lease.expires_at

    def states():
        return [row[2:4] for row in anonymous_rows(app_engine)[1]]
    assert states() == [(0, "pending"), (1, "uncertain"), (2, "completed")]
    for lease in (pending.lease, ready.lease, completed.lease):
        service.release(lease)  # Stale, uncertain and completed: no refund.
    assert states() == [(0, "pending"), (1, "uncertain"), (2, "completed")]
    with pytest.raises(AnonymousLimitReached):
        anonymous_reserved(service)  # Uncertain and completed still consume capacity.
    assert len(states()) == 3


def test_anonymous_pruning_skips_locked_rows_and_is_bounded(app_engine):
    from kalillac_accounts.anonymous_quota import day_digest
    from kalillac_db.repositories.anonymous_usage import prune_anonymous_days
    today = date(2030, 6, 15)
    old_days = [today - timedelta(days=n) for n in (6, 5, 4, 3, 2)]  # All older than yesterday.
    kept_days = [today - timedelta(days=1), today]
    seeded = {}
    with app_engine.begin() as conn:
        for number, day in enumerate(old_days + kept_days):
            digest = day_digest(bytes([number + 1]) * 32, day)
            seeded[day] = digest
            conn.exec_driver_sql("INSERT INTO kalillac.anonymous_usage_days VALUES (%(d)s, %(u)s)",
                                 {"d": digest, "u": day})
            conn.exec_driver_sql(
                "INSERT INTO kalillac.anonymous_usage_slots VALUES (%(d)s, %(u)s, 0, %(e)s, 'completed', %(u)s)",
                {"d": digest, "u": day, "e": datetime(2030, 1, 1, tzinfo=timezone.utc)})
    factory = _factory(app_engine)

    def prune(batch):
        started = time.monotonic()
        with factory() as session, session.begin():
            deleted = prune_anonymous_days(session, today, batch=batch)
        elapsed = time.monotonic() - started
        assert elapsed < 3, f"pruning waited {elapsed:.1f}s"  # The connection lock timeout is 8s.
        return deleted

    def remaining():
        days, slots = anonymous_rows(app_engine)
        assert [row[:2] for row in slots] == days  # Each remaining day keeps its own slot.
        return [day for _, day in days]

    locked_day = old_days[0]  # The oldest: first in pruning order.
    holder = _connect(APP, autocommit=False, application="anonymous-lock-holder")
    try:
        assert holder.execute(
            "SELECT usage_date FROM kalillac.anonymous_usage_days WHERE visitor_digest = %s AND usage_date = %s "
            "FOR UPDATE", [seeded[locked_day], locked_day]).fetchall() == [(locked_day,)]
        assert prune(2) == 2
        assert remaining() == [locked_day] + old_days[3:] + kept_days
        assert prune(2) == 2
        assert remaining() == [locked_day] + kept_days
        assert prune(2) == 0  # Only the locked row is eligible, and it is skipped.
        assert remaining() == [locked_day] + kept_days
    finally:
        holder.rollback()
        holder.close()
    assert prune(2) == 1  # A later prune removes it once the lock is released.
    assert remaining() == kept_days
    assert prune(2) == 0
    assert remaining() == kept_days
