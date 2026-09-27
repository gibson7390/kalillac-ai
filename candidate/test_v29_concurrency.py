"""V29 concurrency tests for app_fastapi_candidate.py.

Never contacts Groq, Cloudflare, or Tavily: the chat() pipeline is replaced by
an instrumented in-process fake for the concurrency tests. The existing
deterministic/router/prompt suites run against the real pipeline code paths
that make no network calls.

Usage (from the directory holding the candidate, staging venv):
    GROQ_API_KEY=test-not-real python3 test_v29_concurrency.py app_fastapi_candidate
"""
import asyncio
import importlib
import io
import json
import os
import sys
import threading
import time
from contextlib import redirect_stdout

os.environ.setdefault("GROQ_API_KEY", "test-not-real")
os.environ.pop("TAVILY_API_KEY", None)
os.environ["DEBUG_MODE"] = "false"

MODULE = sys.argv[1] if len(sys.argv) > 1 else "app_fastapi_candidate"
with redirect_stdout(io.StringIO()):
    m = importlib.import_module(MODULE)

REAL_CHAT = m.chat
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class Probe:
    """Instrumented replacement for chat(). Tracks global and per-session
    concurrency inside the pipeline and can block until released."""

    def __init__(self, hold=None, barrier=None, work_seconds=0.0):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.per_session = {}
        self.max_per_session = {}
        self.overlap_violations = 0
        self.calls = []
        self.hold = hold              # threading.Event to wait on, or None
        self.barrier = barrier        # threading.Barrier to prove overlap
        self.barrier_ok = None
        self.work_seconds = work_seconds

    def __call__(self, message, history, request=None, session_id=None):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            n = self.per_session.get(session_id, 0) + 1
            self.per_session[session_id] = n
            self.max_per_session[session_id] = max(self.max_per_session.get(session_id, 0), n)
            if n > 1:
                self.overlap_violations += 1
            self.calls.append((session_id, message))
        try:
            # Touch real session state the way the pipeline does.
            state = m.get_session_state_by_id(session_id)
            with m.SESSION_LOCK:
                before = len(state["memory"])
            if self.barrier is not None:
                try:
                    self.barrier.wait(timeout=5)
                    self.barrier_ok = True if self.barrier_ok is None else self.barrier_ok
                except threading.BrokenBarrierError:
                    self.barrier_ok = False
            if self.hold is not None:
                self.hold.wait(timeout=10)
            if self.work_seconds:
                time.sleep(self.work_seconds)
            with m.SESSION_LOCK:
                # Non-atomic read-modify-write across the whole call: a second
                # concurrent run for the same session would lose an entry.
                state["memory"] = state["memory"][:before] + [{"fact": message}]
            return f"reply:{message}"
        finally:
            with self.lock:
                self.active -= 1
                self.per_session[session_id] -= 1


def reset_loop_state():
    m._chat_semaphore = None
    m._chat_waiting = 0
    m._session_locks.clear()


def seed_session(sid):
    m.get_session_state_by_id(sid)
    return sid


def decode(resp):
    return resp.status_code, json.loads(bytes(resp.body).decode())


async def post(body):
    return decode(await m.api_chat(FakeRequest(body)))


def bookkeeping_clean():
    sem = m._get_chat_semaphore()
    return (
        m._session_locks == {}
        and m._chat_waiting == 0
        and getattr(sem, "_value", m.MAX_CONCURRENT_CHATS) == m.MAX_CONCURRENT_CHATS
    )


# ---- H: configuration -------------------------------------------------------------
check("MAX_CONCURRENT_CHATS == 4", m.MAX_CONCURRENT_CHATS == 4, m.MAX_CONCURRENT_CHATS)
check("MAX_QUEUED_CHATS == 16", m.MAX_QUEUED_CHATS == 16, m.MAX_QUEUED_CHATS)


# ---- A: different sessions run concurrently up to the limit -----------------------
async def test_a():
    reset_loop_state()
    probe = Probe(barrier=threading.Barrier(m.MAX_CONCURRENT_CHATS))
    m.chat = probe
    sids = [seed_session(f"a-session-{i}") for i in range(m.MAX_CONCURRENT_CHATS)]
    out = await asyncio.gather(*[post({"message": f"m{i}", "session_id": s}) for i, s in enumerate(sids)])
    return probe, out

probe, out = asyncio.run(test_a())
check("A: 4 different sessions inside chat simultaneously (barrier met)", probe.barrier_ok is True, probe.barrier_ok)
check("A: all 200", all(st == 200 for st, _ in out), out)
check("A: bookkeeping clean", bookkeeping_clean(), m._session_locks)


# ---- B + C: same session serialized, second waits then completes ------------------
async def test_bc():
    reset_loop_state()
    probe = Probe(work_seconds=0.15)
    m.chat = probe
    sid = seed_session("bc-shared-session")
    with m.SESSION_LOCK:
        m.SESSION_STATE[sid]["memory"] = []
    t0 = time.monotonic()
    out = await asyncio.gather(*[post({"message": f"same{i}", "session_id": sid}) for i in range(3)])
    return probe, out, sid, time.monotonic() - t0

probe, out, sid, elapsed = asyncio.run(test_bc())
check("B: same session never concurrent inside chat_core", probe.max_per_session.get(sid) == 1 and probe.overlap_violations == 0, probe.max_per_session)
check("C: all same-session requests complete 200", [st for st, _ in out] == [200, 200, 200], out)
check("C: replies match their own requests", [b["reply"] for _, b in out] == ["reply:same0", "reply:same1", "reply:same2"], out)
with m.SESSION_LOCK:
    mem = [e["fact"] for e in m.SESSION_STATE[sid]["memory"]]
check("C: no lost session-state update (3 entries)", sorted(mem) == ["same0", "same1", "same2"], mem)
check("C: ran sequentially (>= 3 x work time)", elapsed >= 0.45 - 0.02, round(elapsed, 3))
check("B/C: bookkeeping clean", bookkeeping_clean(), m._session_locks)


# ---- D: global active never exceeds MAX_CONCURRENT_CHATS --------------------------
async def test_d():
    reset_loop_state()
    probe = Probe(work_seconds=0.05)
    m.chat = probe
    sids = [seed_session(f"d-session-{i}") for i in range(12)]
    reqs = [post({"message": f"d{i}", "session_id": s}) for i, s in enumerate(sids)]
    # add same-session overlap too
    reqs += [post({"message": f"d-dup{i}", "session_id": sids[0]}) for i in range(3)]
    out = await asyncio.gather(*reqs)
    return probe, out

probe, out = asyncio.run(test_d())
check("D: max global active == MAX_CONCURRENT_CHATS", probe.max_active == m.MAX_CONCURRENT_CHATS, probe.max_active)
check("D: all 15 completed 200", all(st == 200 for st, _ in out) and len(probe.calls) == 15, [st for st, _ in out])
check("D: no same-session overlap under mixed load", probe.overlap_violations == 0)
check("D: bookkeeping clean", bookkeeping_clean(), m._session_locks)


# ---- E: queue saturation -> 429 busy ----------------------------------------------
async def test_e_global():
    reset_loop_state()
    hold = threading.Event()
    probe = Probe(hold=hold)
    m.chat = probe
    limit = m.MAX_CONCURRENT_CHATS + m.MAX_QUEUED_CHATS
    sids = [seed_session(f"e-session-{i}") for i in range(limit + 1)]
    tasks = [asyncio.ensure_future(post({"message": f"e{i}", "session_id": s})) for i, s in enumerate(sids[:limit])]
    for _ in range(200):
        await asyncio.sleep(0.01)
        if probe.active == m.MAX_CONCURRENT_CHATS and m._chat_waiting == m.MAX_QUEUED_CHATS:
            break
    active, waiting = probe.active, m._chat_waiting
    rejected = await post({"message": "overflow", "session_id": sids[limit]})
    hold.set()
    done = await asyncio.gather(*tasks)
    return active, waiting, rejected, done

active, waiting, rejected, done = asyncio.run(test_e_global())
check("E: saturated at 4 active + 16 waiting", (active, waiting) == (4, 16), (active, waiting))
check("E: overflow -> 429 {'error':'busy'}", rejected == (429, {"error": "busy"}), rejected)
check("E: admitted requests all complete 200", all(st == 200 for st, _ in done), [st for st, _ in done])
check("E: bookkeeping clean", bookkeeping_clean(), m._session_locks)


async def test_e_same_session():
    reset_loop_state()
    hold = threading.Event()
    probe = Probe(hold=hold)
    m.chat = probe
    sid = seed_session("e-flood-session")
    tasks = [asyncio.ensure_future(post({"message": f"f{i}", "session_id": sid})) for i in range(1 + m.MAX_QUEUED_CHATS)]
    for _ in range(200):
        await asyncio.sleep(0.01)
        if probe.active == 1 and m._chat_waiting == m.MAX_QUEUED_CHATS:
            break
    active, waiting = probe.active, m._chat_waiting
    rejected = await post({"message": "flood-overflow", "session_id": sid})
    hold.set()
    done = await asyncio.gather(*tasks)
    return active, waiting, rejected, done, probe

active, waiting, rejected, done, probe = asyncio.run(test_e_same_session())
check("E: one session: 1 running + 16 queued (counts against global queue)", (active, waiting) == (1, 16), (active, waiting))
check("E: same-session flood overflow -> 429 busy", rejected == (429, {"error": "busy"}), rejected)
check("E: flood completes serially without overlap", all(st == 200 for st, _ in done) and probe.overlap_violations == 0)
check("E: bookkeeping clean after flood", bookkeeping_clean(), m._session_locks)


# ---- F: bookkeeping does not grow ---------------------------------------------------
async def test_f():
    reset_loop_state()
    probe = Probe()
    m.chat = probe
    peak = 0
    for batch in range(10):
        reqs = [post({"message": f"f{batch}-{i}"}) for i in range(20)]  # 200 new sessions
        await asyncio.gather(*reqs)
        peak = max(peak, len(m._session_locks))
    return peak

peak = asyncio.run(test_f())
check("F: 200 new-session requests leave _session_locks empty", m._session_locks == {} and peak == 0, (peak, len(m._session_locks)))


# ---- cancellation keeps limits honest --------------------------------------------------
async def test_cancel():
    reset_loop_state()
    hold = threading.Event()
    probe = Probe(hold=hold)
    m.chat = probe
    sid = seed_session("cancel-session")
    t1 = asyncio.ensure_future(post({"message": "c1", "session_id": sid}))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if probe.active == 1:
            break
    t1.cancel()
    try:
        await t1
    except asyncio.CancelledError:
        pass
    still_held = sid in m._session_locks and m._get_chat_semaphore()._value == m.MAX_CONCURRENT_CHATS - 1
    t2 = asyncio.ensure_future(post({"message": "c2", "session_id": sid}))
    await asyncio.sleep(0.1)
    c2_blocked = probe.active == 1 and len(probe.calls) == 1
    hold.set()
    r2 = await t2
    for _ in range(100):
        await asyncio.sleep(0.01)
        if bookkeeping_clean():
            break
    return still_held, c2_blocked, r2, probe

still_held, c2_blocked, r2, probe = asyncio.run(test_cancel())
check("cancel: slot + session lock held until orphaned thread finishes", still_held)
check("cancel: next same-session request waits for orphaned run", c2_blocked)
check("cancel: next request then completes, no overlap", r2[0] == 200 and probe.overlap_violations == 0, r2)
check("cancel: bookkeeping clean afterwards", bookkeeping_clean(), m._session_locks)


# ---- I + J: response shape and single session-id resolution ----------------------------
async def test_ij():
    reset_loop_state()
    seen = {}
    calls = {"resolve": 0}
    real_resolve = m.resolve_session_id

    def counting_resolve(sid):
        calls["resolve"] += 1
        return real_resolve(sid)

    def spy(message, history, request=None, session_id=None):
        m.get_session_state_by_id(session_id)  # the real pipeline registers state
        seen["chat_sid"] = session_id
        seen["lock_keys"] = list(m._session_locks.keys())
        return "spy reply"

    m.resolve_session_id = counting_resolve
    m.chat = spy
    try:
        new = await post({"message": "hello, no session"})
        unknown = await post({"message": "hello", "session_id": "well-formed-but-unknown"})
        seen_unknown = dict(seen)
        known = await post({"message": "again", "session_id": new[1]["session_id"]})
    finally:
        m.resolve_session_id = real_resolve
    return new, unknown, seen_unknown, known, seen, calls

new, unknown, seen_unknown, known, seen, calls = asyncio.run(test_ij())
check("I: response is exactly {reply, session_id}", new[0] == 200 and set(new[1]) == {"reply", "session_id"} and new[1]["reply"] == "spy reply", new)
check("J: resolve_session_id called once per request", calls["resolve"] == 3, calls)
check("J: new session: lock key == chat_core id == returned id",
      seen_unknown["lock_keys"] == [seen_unknown["chat_sid"]] == [unknown[1]["session_id"]]
      and unknown[1]["session_id"] != "well-formed-but-unknown", (seen_unknown, unknown))
check("J: known session id reused unchanged", known[1]["session_id"] == new[1]["session_id"] and seen["chat_sid"] == new[1]["session_id"], known)


# ---- unchanged API codes ------------------------------------------------------------------
async def test_codes():
    reset_loop_state()

    class BadJSON:
        async def json(self):
            raise ValueError("bad")

    a = decode(await m.api_chat(BadJSON()))
    b = await post([1])
    c = await post({"message": "   "})
    d = await post({"message": "x" * (m.MAX_INPUT_CHARS + 1)})
    e = await post({"message": "hi", "history": [{"role": "user", "content": "a"}] * (m.MAX_HISTORY_TURNS + 1)})

    def boom(*a, **k):
        raise KeyError("x")

    m.chat = boom
    f = await post({"message": "hi"})
    return a, b, c, d, e, f

a, b, c, d, e, f = asyncio.run(test_codes())
check("codes: invalid_json 400", a == (400, {"error": "invalid_json"}), a)
check("codes: invalid_body 400", b == (400, {"error": "invalid_body"}), b)
check("codes: empty_message 422", c == (422, {"error": "empty_message"}), c)
check("codes: message_too_long 422", d == (422, {"error": "message_too_long"}), d)
check("codes: history_too_long 422", e == (422, {"error": "history_too_long"}), e)
check("codes: internal_error 500 + bookkeeping clean", f == (500, {"error": "internal_error"}) and bookkeeping_clean(), f)

# ---- G: existing suites against the real pipeline ------------------------------------------
m.chat = REAL_CHAT
for suite in ["run_deterministic_tests", "run_router_tests", "run_voice_format_prompt_tests",
              "run_personal_no_talk_boundary_tests", "run_engagement_prompt_tests"]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = getattr(m, suite)()
    lines = buf.getvalue().splitlines()
    summary = [l for l in lines if "PASSED" in l]
    failed_names = [l.split(" ", 2)[1:] and l[:120] for l in lines if l.startswith("[FAIL]")]
    check(f"G: {suite}", ok, (summary[-1] if summary else "") + " " + " | ".join(failed_names))

fails = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  :: {detail}"))
print(f"\nV29 CONCURRENCY: {len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
sys.exit(1 if fails else 0)
