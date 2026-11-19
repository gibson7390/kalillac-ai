"""F4, second round: a file worker whose HTTP request was cancelled still belongs to its chat session.

Codex's reproduction (verification of c6fcaeb): hold the file assistant's model call, cancel the HTTP request,
End & erase the session, then let the model answer. Before the correction the handler's cancellation released
the session's only registration, so End & erase reported no active request, and the worker went on to create a
file and make another model call. These run the real application in a child process: the real chat route,
handoff, FileService worker thread, assistant, request budget, End & erase and store; only the account lookup
and the model are substituted, and the model blocks on events so every step is deterministic.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pptx")
pytest.importorskip("typst")

from test_files_session_and_modes import COMMON  # noqa: E402
from test_unsafe_request_guard import ORIGIN, _run_app  # noqa: E402

DETACHED = COMMON + r'''
import time
a._request_limits = object()                  # The file scope runs under its real RequestBudget.
entered, release, calls = threading.Event(), threading.Event(), []
def model(items):
    calls.append(1)
    if len(calls) == 1:
        entered.set()
        assert release.wait(30)
        return CREATE                         # Asks for a file: a tool the worker must not run after the end.
    return REPLY
a._file_service.call_model = model
slots = a._file_service.assists._value

def drained(sid, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with a.SESSION_LOCK:
            if sid not in a._SESSION_WORK:
                return True
        time.sleep(0.02)
    return False

async def scenario(erase):
    entered.clear(); release.clear(); del calls[:]
    sid = a.new_session_id()
    a.get_session_state_by_id(sid)["memory"].append("an earlier temporary turn")
    before = len(a._file_store.list("acct-paid"))
    async with client() as c:
        task = asyncio.create_task(c.post("/api/chat", headers=headers("paid"),
                                          json={"message": "Make me a slide deck", "session_id": sid}))
        assert await asyncio.to_thread(entered.wait, 20)
        task.cancel()                                          # The client goes away mid-request.
        await asyncio.gather(task, return_exceptions=True)
        with a.SESSION_LOCK:
            registered = len(a._SESSION_WORK.get(sid, ()))
        slots_while_running = a._file_service.assists._value
        ended = None
        if erase:
            ended = (await c.post("/api/chat/session/end", headers=headers("paid"), json={"session_id": sid})).json()
        release.set()
        finished = await asyncio.to_thread(drained, sid)
        model_calls, new_files = len(calls), len(a._file_store.list("acct-paid")) - before
        again = await c.post("/api/chat", headers=headers("paid"),
                             json={"message": "Make me a slide deck", "session_id": sid})
    with a.SESSION_LOCK:
        tombstone = a._ENDED_SESSIONS.get(sid, "absent")
    return {"registered_after_disconnect": registered, "ended": ended, "model_calls": model_calls,
            "new_files": new_files, "drained": finished,
            "slots_while_running": slots_while_running, "slots_after": a._file_service.assists._value,
            "slots": slots, "state_revived": sid in a.SESSION_STATE, "again": again.status_code,
            "tombstone_drained": isinstance(tombstone, float)}

print(json.dumps(asyncio.run(scenario(erase=True))))
print(json.dumps(asyncio.run(scenario(erase=False))))
'''


def _lines(result, count):
    assert result.returncode == 0, result.stderr[-4000:]
    return [json.loads(line) for line in result.stdout.strip().splitlines() if line.startswith("{")][-count:]


def _env(tmp_path):
    return {"KALILLAC_SITE_ORIGIN": ORIGIN, "KALILLAC_FILES_ENABLED": "true",
            "KALILLAC_FILES_ROOT": str(tmp_path / "files"), "OPENAI_API_KEY": "sk-test-not-real"}


def test_a_worker_whose_request_was_cancelled_is_still_found_and_stopped_by_end_and_erase(tmp_path):
    erased, kept = _lines(_run_app(_env(tmp_path), DETACHED.replace("ORIGIN_VALUE", ORIGIN)), 2)
    # Codex's sequence. The worker keeps its own registration after the handler is cancelled...
    assert erased["registered_after_disconnect"] == 1, erased
    # ...so End & erase finds it and cancels its budget...
    assert erased["ended"] == {"ended": True, "kalillac_session_state": "removed", "active_requests": 1,
                               "cancellation": "requested"}, erased
    # ...and once the model answers, the worker runs nothing more: no file, no second model call.
    assert erased["model_calls"] == 1 and erased["new_files"] == 0, erased
    # It then releases its registration and its assistant slot, and nothing revives the session.
    assert erased["drained"] and erased["tombstone_drained"] and not erased["state_revived"], erased
    assert erased["again"] == 409, erased
    # The assistant slot stayed taken while the thread ran, and was returned when it ended.
    assert erased["slots_while_running"] == erased["slots"] - 1 and erased["slots_after"] == erased["slots"], erased


def test_a_cancelled_request_without_an_end_finishes_its_work_and_then_releases_everything(tmp_path):
    """Ending the conversation is what stops the worker, not the client going away: without an end the worker
    finishes, its file belongs to the account, and its registration and slot are released when it ends."""
    erased, kept = _lines(_run_app(_env(tmp_path), DETACHED.replace("ORIGIN_VALUE", ORIGIN)), 2)
    assert kept["registered_after_disconnect"] == 1 and kept["ended"] is None, kept
    assert kept["model_calls"] == 2 and kept["new_files"] == 1, kept
    assert kept["drained"] and kept["slots_after"] == kept["slots"], kept
    assert kept["again"] == 200, kept


DIRECT = COMMON + r'''
import time
entered, release = threading.Event(), threading.Event()
def model(items):
    entered.set()
    assert release.wait(30)
    return REPLY
a._file_service.call_model = model
slots = a._file_service.assists._value
async def scenario():
    async with client() as c:
        task = asyncio.create_task(c.post("/api/files/assist", headers={**headers("paid"),
                                          "content-type": "application/json"},
                                          content=json.dumps({"message": "Make me a slide deck"})))
        assert await asyncio.to_thread(entered.wait, 20)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        held = a._file_service.assists._value
        release.set()
        deadline = time.monotonic() + 20
        while a._file_service.assists._value != slots and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        return {"slots": slots, "held": held, "after": a._file_service.assists._value}
print(json.dumps(asyncio.run(scenario())))
'''


def test_the_direct_file_route_keeps_its_assistant_slot_until_a_cancelled_worker_ends(tmp_path):
    """/api/files/assist has no chat session; a cancelled request must still not free the concurrency slot
    while its thread is running (the bound on concurrent assistants would otherwise not hold)."""
    (result,) = _lines(_run_app(_env(tmp_path), DIRECT.replace("ORIGIN_VALUE", ORIGIN)), 1)
    assert result["held"] == result["slots"] - 1 and result["after"] == result["slots"], result
