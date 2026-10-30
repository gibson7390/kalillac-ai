"""End & erase in the real chat frontend.

The browser scenarios run the real frontend/index.html and frontend/app.js in
headless Microsoft Edge (or another installed Chromium-family browser) through
the same loopback-only harness as test_frontend_error_contract.py: fetch is
scripted in the page and nothing leaves the machine. Static checks always run.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil

import pytest

from test_frontend_error_contract import (
    APP_JS, EDGE_WINDOWS, INDEX_HTML, SENTINELS, Harness, find_browser, run_browser,
)


APP_CSS = APP_JS.parent / "app.css"
END = "/api/chat/session/end"
RECEIPT_REMOVED = ("Ended on this device. Kalillac removed this session’s temporary conversation "
                   "memory. Any active response was asked to stop.")
RECEIPT_NOT_FOUND = "Ended on this device. Kalillac found no temporary session memory to remove."
RECEIPT_LIMITS = ("Information already sent to AI or search providers, required allowance records, "
                  "and operational or security logs are not erased by this action. Provider "
                  "processing may not stop immediately.")
RECEIPT_FAILED = ("Cleared from this device, but Kalillac could not confirm removal of its temporary "
                  "server memory.")
RECEIPT_LOCAL = "Cleared from this device. No Kalillac session had been confirmed for this conversation."
SESSION_ENDED = "This conversation was ended. Your next message starts a new one."
DIALOG_TITLE = "End and erase this conversation?"
DIALOG_TEXT = ("This clears the conversation and any unsent draft from this device and asks Kalillac "
               "to remove its temporary memory of this session. This can’t be undone.")

SCENARIOS = (
    "erase_enablement", "erase_cancel_paths", "erase_removed", "erase_not_found",
    "erase_failure_then_retry", "erase_network_failure", "erase_in_flight",
    "erase_without_session", "erase_first_message_in_flight", "session_ended_reply",
    "erase_overlap_newer_first", "erase_overlap_older_first", "erase_overlap_all_not_found",
    "erase_timeout_then_retry", "erase_notice_auto_dismiss", "erase_notice_pause_resume",
    "erase_notice_manual_dismiss", "erase_notice_reduced_motion", "erase_dialog_focus", "erase_confirm_once",
)


@pytest.fixture(scope="module")
def erase_results(tmp_path_factory):
    browser = find_browser()

    if browser is None:
        if os.name == "nt":
            pytest.fail(f"Microsoft Edge is required on Windows and was not found at {EDGE_WINDOWS}")
        pytest.skip("no supported Chromium-family browser is installed")

    profile = tmp_path_factory.mktemp("edge-profile-erase")

    with Harness() as harness:
        url = f"{harness.origin}/__harness__/runner?scenarios={','.join(SCENARIOS)}"
        result = run_browser(browser, url, profile)
        unexpected = list(harness.unexpected)

    shutil.rmtree(profile, ignore_errors=True)
    match = re.search(r'<pre id="results">(.*?)</pre>', result.stdout, re.S)
    assert result.returncode == 0 and match, result.stderr[-3000:]
    assert unexpected == []
    return json.loads(html.unescape(match.group(1)))


def run(results, name):
    result = results[name]
    assert result.get("error") is None, (name, result)
    return result


def step(result, label):
    return next(s for s in result["steps"] if s["label"] == label)


def assert_cleared(snap):
    """Nothing of the conversation is left on the page, and the controls are idle."""
    assert snap["users"] == [] and snap["assistants"] == [] and snap["rowOrder"] == []
    assert snap["emptyShown"] is True and snap["conversationEmpty"] is True
    assert snap["composer"] == ""
    assert snap["sendIsStop"] is False and snap["readOnly"] is False
    assert snap["sendLabel"] == "Send message"
    assert snap["erase"]["buttonDisabled"] is True
    assert snap["erase"]["dialogOpen"] is False
    assert snap["focusIsComposer"] is True


def erase_request(request, session_id, timed_out=False):
    assert request["url"] == END and request["method"] == "POST"
    assert request["body"] == {"session_id": session_id}
    assert request["contentType"] == "application/json"
    assert request["keepalive"] is True
    # Its own signal (for its timeout), never the chat request's: aborting
    # the chat request does not abort it.
    assert request["hasSignal"] is True
    if not timed_out:
        assert request["aborted"] is False


def endpoint_requests(result):
    return [r for r in result["requests"] if r["url"] == END]


def receipt(snap):
    e = snap["erase"]
    return e["receiptText"] if e["receiptShown"] else None


# --- browser scenarios -------------------------------------------------------------------------


def test_every_erase_scenario_completed(erase_results):
    assert set(erase_results) == set(SCENARIOS)
    for name in SCENARIOS:
        assert erase_results[name].get("error") is None, name


def test_control_is_disabled_only_with_nothing_to_erase(erase_results):
    result = run(erase_results, "erase_enablement")
    states = {label: step(result, label)["erase"]["buttonDisabled"]
              for label in ("initial", "draft", "cleared", "conversation")}
    assert states == {"initial": True, "draft": False, "cleared": True, "conversation": False}
    assert step(result, "initial")["erase"]["buttonText"] == "End & erase"


def test_confirmation_is_a_modal_dialog_and_every_dismissal_keeps_everything(erase_results):
    result = run(erase_results, "erase_cancel_paths")
    opened = step(result, "open")["erase"]
    assert opened["dialogOpen"] is True and opened["dialogModal"] is True
    assert opened["focusIsCancel"] is True                  # the safe choice has focus
    assert DIALOG_TITLE in opened["dialogText"] and DIALOG_TEXT in opened["dialogText"]

    for label in ("after-cancel", "after-escape", "after-backdrop"):
        snap = step(result, label)
        assert snap["erase"]["dialogOpen"] is False, label
        assert snap["users"] == ["question"], label
        assert snap["composer"] == "my draft", label
        assert snap["erase"]["receiptShown"] is False, label
    assert step(result, "after-cancel")["erase"]["focusIsButton"] is True
    # No erase request was ever sent.
    assert [r["url"] for r in result["requests"]] == ["/api/chat"]


def test_confirmed_erase_clears_everything_and_reports_removal(erase_results):
    result = run(erase_results, "erase_removed")
    erased = step(result, "erased")
    assert_cleared(erased)
    assert erased["erase"]["receiptShown"] is True
    assert erased["erase"]["receiptText"] == RECEIPT_REMOVED
    assert erased["erase"]["receiptNote"] == RECEIPT_LIMITS
    assert erased["erase"]["retryShown"] is False
    assert erased["erase"]["receiptRole"] == "status" and erased["erase"]["receiptLive"] == "polite"

    chat_one, chat_two, end, fresh = result["requests"]
    assert chat_two["body"]["session_id"] == "s-1"
    erase_request(end, "s-1")
    # The next message starts a new conversation: no session id, no old history.
    assert fresh["url"] == "/api/chat"
    assert fresh["body"] == {"message": "a new question", "history": [], "session_id": None}
    after = step(result, "after-new-send")
    assert after["users"] == ["a new question"]
    assert after["erase"]["receiptShown"] is False


def assert_notice_shown(snap):
    e = snap["erase"]
    assert e["receiptShown"] is True and e["receiptDisplay"] != "none" and e["receiptLeaving"] is False, snap["label"]
    assert e["receiptText"] == RECEIPT_REMOVED and e["receiptNote"] == RECEIPT_LIMITS
    assert e["dismissText"] == "Dismiss" and e["receiptRole"] == "status" and e["receiptLive"] == "polite"


def assert_notice_gone(snap):
    """Hidden, out of the layout (no gray block, no hole), countdown stopped,
    and the clean empty workspace remains."""
    e = snap["erase"]
    assert e["receiptShown"] is False and e["receiptDisplay"] == "none" and e["receiptHeight"] == 0, snap["label"]
    assert e["armedTickers"] == 0, snap["label"]
    assert snap["emptyShown"] is True and snap["users"] == [] and snap["composer"] == ""


def test_completion_notice_dismisses_itself_after_twelve_seconds(erase_results):
    result = run(erase_results, "erase_notice_auto_dismiss")
    assert_notice_shown(step(result, "shown"))
    assert step(result, "shown")["erase"]["armedTickers"] == 1
    assert_notice_shown(step(result, "tick-47"))            # 11.75 s: still there
    leaving = step(result, "tick-48")                       # 12 s: the 160 ms fade starts
    assert leaving["erase"]["receiptShown"] is True and leaving["erase"]["receiptLeaving"] is True
    assert_notice_gone(step(result, "dismissed"))
    assert len(endpoint_requests(result)) == 1


def test_hover_and_focus_pause_the_countdown_and_it_resumes_where_it_stopped(erase_results):
    result = run(erase_results, "erase_notice_pause_resume")
    # 20 ticks counted, then 100 ticks while hovered and 100 while focused: none counted.
    assert_notice_shown(step(result, "hovered"))
    assert_notice_shown(step(result, "focused"))
    # 20 + 27 = 47 counted ticks: still shown. One more ends it.
    assert_notice_shown(step(result, "resumed-47"))
    assert_notice_gone(step(result, "dismissed"))


def test_dismiss_closes_the_notice_and_returns_focus_to_the_composer(erase_results):
    result = run(erase_results, "erase_notice_manual_dismiss")
    assert_notice_shown(step(result, "focused"))
    assert step(result, "leaving")["erase"]["receiptLeaving"] is True
    dismissed = step(result, "dismissed")
    assert_notice_gone(dismissed)
    assert dismissed["focusIsComposer"] is True


def test_reduced_motion_removes_the_notice_at_once_without_a_fade(erase_results):
    snap = step(run(erase_results, "erase_notice_reduced_motion"), "dismissed")
    assert snap["erase"]["receiptLeaving"] is False
    assert_notice_gone(snap)


def test_dialog_keeps_focus_inside_and_cancel_returns_it_to_the_control(erase_results):
    result = run(erase_results, "erase_dialog_focus")
    assert step(result, "open")["erase"]["focusIsCancel"] is True
    assert step(result, "shift-tab-from-cancel")["erase"]["focusIsConfirm"] is True
    assert step(result, "tab-from-confirm")["erase"]["focusIsCancel"] is True
    cancelled = step(result, "cancelled")
    assert cancelled["erase"]["dialogOpen"] is False and cancelled["erase"]["focusIsButton"] is True
    assert cancelled["users"] == ["question"]
    assert endpoint_requests(result) == []


def test_repeated_confirmation_erases_exactly_once(erase_results):
    result = run(erase_results, "erase_confirm_once")
    erased = step(result, "erased")
    assert_cleared(erased)
    (end,) = endpoint_requests(result)
    erase_request(end, "s-1")
    assert erased["erase"]["receiptText"] == RECEIPT_REMOVED


def test_not_found_receipt(erase_results):
    result = run(erase_results, "erase_not_found")
    erased = step(result, "erased")
    assert_cleared(erased)
    assert erased["erase"]["receiptText"] == RECEIPT_NOT_FOUND
    assert erased["erase"]["receiptNote"] == RECEIPT_LIMITS
    erase_request(result["requests"][1], "s-1")


def test_unconfirmed_removal_offers_retry_with_the_same_opaque_id(erase_results):
    result = run(erase_results, "erase_failure_then_retry")
    failed = step(result, "failed")
    assert_cleared(failed)                                  # the device is cleared regardless
    assert failed["erase"]["receiptText"] == RECEIPT_FAILED
    assert failed["erase"]["receiptNote"] is None
    assert failed["erase"]["retryShown"] is True

    retried = step(result, "retried")
    assert retried["erase"]["receiptText"] == RECEIPT_REMOVED
    assert retried["erase"]["receiptNote"] == RECEIPT_LIMITS
    assert retried["erase"]["retryShown"] is False
    assert retried["users"] == [] and retried["composer"] == ""   # nothing was restored

    _, first, second = result["requests"]
    erase_request(first, "s-1")
    erase_request(second, "s-1")
    for snap in result["steps"]:
        for sentinel in SENTINELS:
            assert sentinel not in (snap["erase"]["receiptText"] or ""), sentinel


def test_network_failure_and_unrecognized_replies_are_not_confirmations(erase_results):
    result = run(erase_results, "erase_network_failure")
    for label in ("failed", "still-failed"):
        snap = step(result, label)
        assert snap["erase"]["receiptText"] == RECEIPT_FAILED, label
        assert snap["erase"]["retryShown"] is True, label
    assert [r["url"] for r in result["requests"]] == ["/api/chat", END, END]


def test_erasing_mid_reply_aborts_it_and_sends_a_separate_erase(erase_results):
    result = run(erase_results, "erase_in_flight")
    assert step(result, "in-flight")["sendIsStop"] is True
    erased = step(result, "erased")
    assert_cleared(erased)
    assert "Stopped." not in erased["threadText"]           # nothing of the old exchange returns
    assert erased["erase"]["receiptText"] == RECEIPT_REMOVED

    first, hanging, end = result["requests"]
    assert hanging["url"] == "/api/chat" and hanging["aborted"] is True
    assert hanging["body"]["session_id"] == "s-1"
    erase_request(end, "s-1")


def test_without_a_session_id_only_the_device_is_cleared(erase_results):
    result = run(erase_results, "erase_without_session")
    erased = step(result, "erased")
    assert_cleared(erased)
    assert result["requests"] == []                         # no endpoint request at all
    assert erased["erase"]["receiptText"] == RECEIPT_LOCAL
    assert erased["erase"]["receiptNote"] is None


def test_a_first_message_in_flight_is_aborted_with_no_erase_request(erase_results):
    result = run(erase_results, "erase_first_message_in_flight")
    erased = step(result, "erased")
    assert_cleared(erased)
    (chat,) = result["requests"]
    assert chat["url"] == "/api/chat" and chat["aborted"] is True
    assert erased["erase"]["receiptText"] == RECEIPT_LOCAL
    # A request had been sent, so what providers may already hold is disclosed.
    assert erased["erase"]["receiptNote"] == RECEIPT_LIMITS


def test_a_session_ended_reply_forgets_the_ended_session(erase_results):
    result = run(erase_results, "session_ended_reply")
    ended = step(result, "ended")
    assert [n["text"] for n in ended["assistants"][-1]["notes"]] == [SESSION_ENDED]
    assert ended["assistants"][-1]["actions"] == []
    one, two, three = result["requests"]
    assert two["body"]["session_id"] == "s-1"
    assert three["body"]["session_id"] is None


def test_overlapping_erases_newer_completing_first_keep_each_result(erase_results):
    result = run(erase_results, "erase_overlap_newer_first")
    # Nothing is decided while an erase in the round is pending.
    for label in ("a-pending", "both-pending", "b-removed-a-pending"):
        assert receipt(step(result, label)) is None, label
    assert_cleared(step(result, "both-pending"))

    failed = step(result, "a-failed")["erase"]
    assert failed["receiptText"] == RECEIPT_FAILED and failed["receiptNote"] is None
    assert failed["retryShown"] is True and failed["retryDisabled"] is False

    # Retry erase asks again for A alone: B's confirmation was kept.
    a_first, b, a_retry = endpoint_requests(result)[:3]
    erase_request(a_first, "s-A")
    erase_request(b, "s-B")
    erase_request(a_retry, "s-A")
    retried = step(result, "a-retried")["erase"]
    # A was not found on retry, but B was removed in the same round.
    assert retried["receiptText"] == RECEIPT_REMOVED and retried["receiptNote"] == RECEIPT_LIMITS
    assert retried["retryShown"] is False

    # The settled round was forgotten: no id is asked about again.
    later = step(result, "later-local-erase")
    assert receipt(later) == RECEIPT_LOCAL
    assert len(endpoint_requests(result)) == 3
    chats = [r["body"]["session_id"] for r in result["requests"] if r["url"] == "/api/chat"]
    assert chats == [None, None]                            # B never reused A's id


def test_overlapping_erases_older_completing_first_keep_each_result(erase_results):
    result = run(erase_results, "erase_overlap_older_first")
    assert receipt(step(result, "a-failed-b-pending")) is None
    b_removed = step(result, "b-removed")["erase"]
    assert b_removed["receiptText"] == RECEIPT_FAILED and b_removed["retryShown"] is True

    a_first, b, a_retry = endpoint_requests(result)
    erase_request(a_first, "s-A")
    erase_request(b, "s-B")
    erase_request(a_retry, "s-A")                           # exactly the unresolved id
    retried = step(result, "a-retried")["erase"]
    assert retried["receiptText"] == RECEIPT_REMOVED and retried["retryShown"] is False


def test_a_round_with_nothing_found_reports_not_found(erase_results):
    result = run(erase_results, "erase_overlap_all_not_found")
    settled = step(result, "settled")["erase"]
    assert settled["receiptText"] == RECEIPT_NOT_FOUND and settled["receiptNote"] == RECEIPT_LIMITS
    assert [r["body"]["session_id"] for r in endpoint_requests(result)] == ["s-A", "s-B"]


def test_an_erase_timeout_is_unconfirmed_and_a_stale_answer_changes_nothing(erase_results):
    result = run(erase_results, "erase_timeout_then_retry")
    assert receipt(step(result, "pending")) is None

    timed_out = step(result, "timed-out")["erase"]
    assert timed_out["receiptText"] == RECEIPT_FAILED and timed_out["retryShown"] is True

    # While the retry is pending, the timed-out attempt's late "removed"
    # answer is ignored: the receipt is unchanged and Retry stays disabled.
    for label in ("retry-pending", "stale-answer"):
        erase = step(result, label)["erase"]
        assert erase["receiptText"] == RECEIPT_FAILED and erase["retryDisabled"] is True, label

    failed = step(result, "retry-failed")["erase"]
    assert failed["receiptText"] == RECEIPT_FAILED and failed["retryDisabled"] is False
    # The authoritative answer was "not found", never the stale "removed".
    final = step(result, "final")["erase"]
    assert final["receiptText"] == RECEIPT_NOT_FOUND and final["retryShown"] is False

    first, second, third = endpoint_requests(result)
    erase_request(first, "s-1", timed_out=True)
    assert first["aborted"] is True                         # the timeout aborted it
    erase_request(second, "s-1")
    erase_request(third, "s-1")
    assert result["eraseTimers"] == [
        {"cleared": True, "fired": True},
        {"cleared": True, "fired": False},
        {"cleared": True, "fired": False},
    ]


def test_every_erase_request_has_one_timeout_that_is_always_cleared(erase_results):
    for name in SCENARIOS:
        result = run(erase_results, name)
        timers = result["eraseTimers"]
        assert len(timers) == len(endpoint_requests(result)), name
        assert all(t["cleared"] for t in timers), name      # none left armed


# --- static contract ---------------------------------------------------------------------------


def test_markup_is_accessible_fixed_copy():
    page = INDEX_HTML.read_text(encoding="utf-8")
    # Inside the composer box, so it is visible in the full page and in the
    # homepage's embedded chat (which hides the header).
    shell = page[page.index('<div class="composer-shell" id="composer-shell">'):page.index("</form>")]
    start = shell.index('<button type="button" class="erase-btn"')
    button = re.sub(r"\s+", " ", shell[start:shell.index("</button>", start)])
    assert button.startswith('<button type="button" class="erase-btn" id="erase-btn" '
                             'aria-haspopup="dialog" disabled>')
    # The label is visible text, never only a screen-reader label or a tooltip.
    assert '<span class="erase-btn-label">End &amp; erase</span>' in button
    assert "sr-only" not in button and "title=" not in button
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".erase-btn-label" not in re.sub(r"/\*.*?\*/", "", css, flags=re.S)   # never restyled away
    assert "@media (prefers-reduced-motion: reduce) {\n  .erase-btn { transition: none; }" in css
    header = page[page.index('<header class="app-header">'):page.index("</header>")]
    assert "erase" not in header
    assert ('<dialog class="erase-dialog" id="erase-dialog" aria-labelledby="erase-dialog-title" '
            'aria-describedby="erase-dialog-text">') in page
    assert f'<h2 class="erase-dialog-title" id="erase-dialog-title">{DIALOG_TITLE}</h2>' in page
    assert f'<p class="erase-dialog-text" id="erase-dialog-text">{DIALOG_TEXT}</p>' in page
    assert '<button type="button" class="erase-dialog-cancel" id="erase-dialog-cancel">Cancel</button>' in page
    assert ('<button type="button" class="erase-dialog-confirm" id="erase-dialog-confirm">End &amp; erase'
            '</button>') in page
    assert ('<div class="erase-receipt" id="erase-receipt" role="status" aria-live="polite" hidden>'
            in page)
    assert '<button type="button" class="erase-receipt-retry" id="erase-retry" hidden>Retry erase</button>' in page
    # A clearly labelled manual dismiss control inside the notice.
    receipt = page[page.index('<div class="erase-receipt"'):page.index('<div class="limit-notice"')]
    assert '<button type="button" class="erase-receipt-dismiss" id="erase-receipt-dismiss">Dismiss</button>' in receipt
    # The 12-second lifetime, its pausable 250 ms ticks and the 160 ms exit fade.
    source = APP_JS.read_text(encoding="utf-8")
    for constant in ("var RECEIPT_SHOW_MS = 12000;", "var RECEIPT_TICK_MS = 250;", "var RECEIPT_FADE_MS = 160;"):
        assert constant in source, constant
    assert ".erase-receipt { transition: none; }" in css


def test_receipt_copy_is_exact_and_makes_no_overclaims():
    source = APP_JS.read_text(encoding="utf-8")
    for text in (RECEIPT_REMOVED, RECEIPT_NOT_FOUND, RECEIPT_LIMITS, RECEIPT_FAILED, RECEIPT_LOCAL,
                 SESSION_ENDED):
        assert f'"{text}"' in source, text
    module = source[source.index("// ---- End & erase"):source.index("// ---- composer behavior")]
    # User-facing text only: the module's string literals and the page markup
    # without its comments (code comments may say what is NOT guaranteed).
    literals = " ".join(re.findall(r'"((?:[^"\\]|\\.)*)"', module))
    markup = re.sub(r"<!--.*?-->", "", INDEX_HTML.read_text(encoding="utf-8"), flags=re.S)
    lower = (literals + " " + markup).lower()
    for claim in ("securely", "permanently", "wiped", "deleted from", "all data", "privacy receipt",
                  "guarantee", "zeroed", "forever"):
        assert claim not in lower, claim


def test_erase_request_contract_in_source():
    source = APP_JS.read_text(encoding="utf-8")
    module = source[source.index("// ---- End & erase"):]
    post = module[module.index("function postEraseRequest"):module.index("function renderEraseRound")]
    assert 'fetch("/api/chat/session/end", {' in post
    assert 'headers: { "Content-Type": "application/json" }' in post
    assert "body: JSON.stringify({ session_id: sessionId })" in post
    assert "keepalive: true" in post
    # Its own controller per request and a bounded timeout, cleared on settlement.
    assert "var ERASE_TIMEOUT_MS = 10000;" in module
    assert "var eraseAbort = new AbortController();" in post
    assert "signal: eraseAbort.signal" in post
    assert "setTimeout(function () {\n        eraseAbort.abort();\n        finish(null);" in post
    assert "clearTimeout(timer);" in post
    assert "controller" not in post.replace("AbortController", "")   # never the chat request's
    # Cookies and storage are never used anywhere in the chat script (API use,
    # not the words: the header comment names what is not used).
    for forbidden in (r"document\.cookie", r"\blocalStorage\s*[.\[]", r"\bsessionStorage\s*[.\[]",
                      r"sendBeacon"):
        assert not re.search(forbidden, source), forbidden


# Every text source this slice touches. Tab, CR and LF are the only C0 control
# characters allowed: a stray one (a backspace typed into r"\b...", say) can
# silently change what a pattern or a string means.
SLICE_TEXT_SOURCES = (
    ".github/workflows/code-structure-frontend-verification.yml",
    "candidate/app_fastapi_candidate.py",
    "candidate/tests/frontend_harness/harness.js",
    "candidate/tests/test_frontend_end_erase.py",
    "candidate/tests/test_frontend_error_contract.py",
    "candidate/tests/test_homepage_handoff.py",
    "candidate/tests/test_homepage_interaction.py",
    "candidate/tests/test_openai_only_policy.py",
    "candidate/tests/test_public_identity.py",
    "candidate/tests/test_session_end.py",
    "frontend/app.css",
    "frontend/app.js",
    "frontend/index.html",
    "site/src/App.tsx",
)


def test_slice_sources_contain_no_unexpected_control_characters():
    repo = APP_JS.resolve().parents[1]
    allowed = {0x09, 0x0A, 0x0D}
    for name in SLICE_TEXT_SOURCES:
        data = (repo / name).read_bytes()
        data.decode("utf-8")                                # valid UTF-8 text
        found = sorted({f"0x{b:02x}" for b in data if (b < 0x20 and b not in allowed) or b == 0x7F})
        assert found == [], (name, found)
