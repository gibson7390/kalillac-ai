"""Frontend error contract: executable browser tests and static checks.

Browser tests run the real frontend/index.html and frontend/app.js in
headless Microsoft Edge (or another installed Chromium-family browser),
served from a loopback-only HTTP server, with frontend_harness/harness.js
injected before app.js to script fetch and drive the real DOM. No request
leaves the machine: fetch is replaced in the page, every non-loopback
connection is sent to a dead proxy, and every other host name fails to
resolve. On Windows, Edge at its standard path is required; elsewhere the
browser tests skip only when no supported browser exists. The static checks
always run.
"""

from __future__ import annotations

import html
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading

import pytest


REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
APP_JS = FRONTEND / "app.js"
INDEX_HTML = FRONTEND / "index.html"
HARNESS_JS = Path(__file__).resolve().parent / "frontend_harness" / "harness.js"
BACKEND = REPO / "candidate" / "app_fastapi_candidate.py"

EDGE_WINDOWS = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
CACHE_KEY = "20261020-syntax-highlight1"
CSS_CACHE_KEY = "20261020-syntax-highlight1"
# The baseline the error-contract slice started from; only app.js, app.css
# (the homepage handoff's embedded presentation) and index.html may differ
# from it under frontend/.
BASELINE_COMMIT = "973e2c9099ca9f44d17821d33b94b849e1fbd749"
# Edge writes internal diagnostics to stderr that vary from run to run (for
# example the task-manager notice, or a blocked identity-image or Cast
# certificate fetch -- blocked by the dead proxy and host-resolver rules,
# which is the isolation working). They are recorded, not treated as
# failures; only these signs of a broken page fail the run.
PAGE_FAILURE_STDERR = ("Page load failed", "Uncaught", "net::ERR_")

UNREACHABLE = "Kalillac couldn't reach the server. Check your connection and try again."
INCOMPLETE = "Kalillac couldn't complete the request. Please try again."
UNREADABLE = "Kalillac couldn't read that request. Please try again."
BUSY = "Kalillac is busy right now. Please try again in a moment."
STOPPED = "Stopped."

# The anonymous daily allowance (fixed copy; typographic apostrophes).
DAILY_LIMIT_CODE = "anonymous_daily_limit_reached"
DAILY_LIMIT_NOTE = "You\u2019ve used today\u2019s 10 free responses."
DAILY_LIMIT_TITLE = "Daily limit reached"
DAILY_LIMIT_RESET = "Your free allowance resets at 12:00 AM UTC."
DAILY_LIMIT_BUTTON = "Got it"
DAILY_LIMIT_DIALOG = " ".join((DAILY_LIMIT_TITLE, DAILY_LIMIT_NOTE, DAILY_LIMIT_RESET, DAILY_LIMIT_BUTTON))
DAILY_LIMIT_FORBIDDEN = ("tomorrow", "unlimited", "subscri", "upgrade", "premium", "pricing",
                         "sign up", "sign-up", "signup", "stripe")

COPY = {
    "invalid_json": UNREADABLE,
    "invalid_body": UNREADABLE,
    "invalid_request": UNREADABLE,
    "empty_message": "Please type a message.",
    "message_too_long": "That message is too long. Please shorten it and try again.",
    "history_too_long": (
        "This conversation is too long to continue. Start a new conversation to keep going."
    ),
    "processing_limit_reached": (
        "This request needed more processing than Kalillac allows for one message. "
        "Try simplifying it or splitting it up."
    ),
    "busy": BUSY,
    "request_cancelled": STOPPED,
    "service_unavailable": "Kalillac is temporarily unavailable. Please try again shortly.",
    "model_provider_unavailable": (
        "Kalillac couldn't get an answer from its AI model just now. Please try again shortly."
    ),
    "request_timeout": (
        "That took too long and was stopped. Please try again, or try a simpler request."
    ),
    "internal_error": "Something went wrong on Kalillac's end. Please try again.",
    DAILY_LIMIT_CODE: DAILY_LIMIT_NOTE,
}
NO_RETRY = {"empty_message", "message_too_long", "history_too_long", "processing_limit_reached",
            DAILY_LIMIT_CODE}

# Text that must never reach the page: raw response fields, HTML error pages,
# provider names and exception messages.
SENTINELS = (
    "RAW-", "SECRET", "OpenAI", "Tavily", "Traceback", "nginx", "Bad Gateway",
    "<html", "teapot", "HARNESS:", "THIS MUST NOT BE RENDERED", "s-ambiguous",
)

GENERIC_FAILURES = {
    "bare_429": BUSY,
    "proxy_502": INCOMPLETE,
    "proxy_503": INCOMPLETE,
    "proxy_504": INCOMPLETE,
    "unknown_status": INCOMPLETE,
    "fetch_reject": UNREACHABLE,
    "reply_missing": INCOMPLETE,
    "reply_empty": INCOMPLETE,
    "reply_whitespace": INCOMPLETE,
    "reply_not_string": INCOMPLETE,
    "success_not_json": INCOMPLETE,
    "success_body_error": INCOMPLETE,
    "success_error_code": INCOMPLETE,
    "success_reply_with_error": INCOMPLETE,
    "render_exception": INCOMPLETE,
    "daily_limit_wrong_status": INCOMPLETE,
}

DAILY_LIMIT_SCENARIOS = (
    "daily_limit_escape", "daily_limit_backdrop", "daily_limit_reopen",
    "daily_limit_without_show_modal", "daily_limit_layout_360x640",
)

# Syntax highlighting of rendered fenced code (frontend presentation only).
CODE_SCENARIOS = ("syntax_highlighting", "syntax_highlighting_360x640")
CODE_SOURCES = {
    "python": 'def greet(name):\n    # Say hello.\n    return f"Hello {name}"\n',
    "javascript": ("const total = [1, 2, 3].reduce((sum, n) => sum + n, 0); // add them\n"
                   "console.log(`total ${total}`);\n"),
    "sql": "SELECT email, COUNT(*) AS n FROM users WHERE active = 1 GROUP BY email HAVING COUNT(*) > 1;\n",
    "json": '{"name": "Ada", "admin": true, "age": 36}\n',
    "html": '<script>alert("test")</script>\n<img src=x onerror="window.__executed=1">\n',
    "foobarlang": "some text that is not code\n",
    "": "unlabeled block\n  keeps   spacing\n",
}
CODE_REPLY = "\n".join([
    "Here is the Python version.", "",
    "```python", "def greet(name):", "    # Say hello.", '    return f"Hello {name}"', "```", "",
    "```javascript", "const total = [1, 2, 3].reduce((sum, n) => sum + n, 0); // add them",
    "console.log(`total ${total}`);", "```", "",
    "```sql", "SELECT email, COUNT(*) AS n FROM users WHERE active = 1 GROUP BY email HAVING COUNT(*) > 1;", "```", "",
    "```json", '{"name": "Ada", "admin": true, "age": 36}', "```", "",
    "```html", '<script>alert("test")</script>', '<img src=x onerror="window.__executed=1">', "```", "",
    "```foobarlang", "some text that is not code", "```", "",
    "```", "unlabeled block", "  keeps   spacing", "```", "",
    "Inline `code` stays plain. This explanation remains normal prose outside the code block.",
])
HIGHLIGHTED_LANGUAGES = ("python", "javascript", "sql", "json", "html")
CODE_BACKGROUND = "#f1f4fa"   # --panel-2, the fenced-code background
HIGHLIGHT_JS = FRONTEND / "vendor" / "highlight" / "highlight.min.js"
HIGHLIGHT_LICENSE = FRONTEND / "vendor" / "highlight" / "LICENSE"
# highlight.js 11.12.0 from @highlightjs/cdn-assets (npm), integrity-checked
# against the registry's sha512 before vendoring.
HIGHLIGHT_JS_SHA256 = "8ab71eb09c51f501e5e25157d9cff100e46cc29bcbfc744d0b746d451fca7f53"
HIGHLIGHT_LICENSE_SHA256 = "6c081431591d9df696c82dc598fe1423765b8a299b200ed00b281afd0f64c490"
VENDORED_HIGHLIGHT = {"frontend/vendor/highlight/highlight.min.js", "frontend/vendor/highlight/LICENSE"}

SCENARIOS = (
    [f"code_{code}" for code in COPY]
    + list(GENERIC_FAILURES)
    + [
        "success", "stop", "stop_then_retry", "retry_success", "retry_fails_again",
        "newer_draft_during_request", "edited_draft_then_retry", "unedited_draft_retry",
        "history_too_long_new_conversation", "success_copy_and_retry",
        "cleared_draft_then_retry", "retry_completed_with_later_failure",
        "message_too_long_4001",
    ]
    + list(DAILY_LIMIT_SCENARIOS)
    + list(CODE_SCENARIOS)
)


# --- browser discovery -------------------------------------------------------------------------


def find_browser() -> Path | None:
    if os.name == "nt":
        return EDGE_WINDOWS if EDGE_WINDOWS.is_file() else None

    for name in ("microsoft-edge", "microsoft-edge-stable", "chromium", "chromium-browser",
                 "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return Path(found)

    return None


# --- loopback server ------------------------------------------------------------------------------


class Harness:
    """A loopback-only server for the real frontend plus the harness routes.
    Every requested path is recorded; anything unexpected is a failure."""

    ALLOWED = re.compile(
        r"^(/app/(\?scenario=[a-z0-9_]+)?|/app/[A-Za-z0-9_./-]+(\?v=[A-Za-z0-9._-]+)?"
        r"|/__harness__/harness\.js|/__harness__/runner\?scenarios=[a-z0-9_,]+"
        r"|/favicon\.ico|/assets/kalillac-ai-logo\.png\?v=[A-Za-z0-9-]+)$"
    )

    def __init__(self):
        self.paths: list[str] = []
        self.unexpected: list[str] = []
        harness = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                harness.paths.append(self.path)

                if not Harness.ALLOWED.match(self.path):
                    harness.unexpected.append(self.path)

                status, ctype, body = harness.route(self.path)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                harness.paths.append("POST " + self.path)
                harness.unexpected.append("POST " + self.path)
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def route(self, raw_path: str):
        path = raw_path.split("?", 1)[0]

        if path == "/app/":
            page = INDEX_HTML.read_text(encoding="utf-8")
            marker = "<!-- Local vendored libraries only"
            assert page.count(marker) == 1
            page = page.replace(
                marker, '<script src="/__harness__/harness.js"></script>\n  ' + marker, 1
            )
            return 200, "text/html; charset=utf-8", page.encode("utf-8")

        if path == "/__harness__/harness.js":
            return 200, "text/javascript", HARNESS_JS.read_bytes()

        if path == "/__harness__/runner":
            page = ('<!DOCTYPE html><html><body><pre id="results">PENDING</pre>'
                    '<script src="/__harness__/harness.js"></script></body></html>')
            return 200, "text/html; charset=utf-8", page.encode("utf-8")

        if path.startswith("/app/"):
            target = (FRONTEND / path[len("/app/"):]).resolve()

            if FRONTEND.resolve() in target.parents and target.is_file():
                ctype = {
                    ".js": "text/javascript", ".css": "text/css", ".html": "text/html",
                    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
                }.get(target.suffix, "application/octet-stream")
                return 200, ctype, target.read_bytes()

        return 404, "text/plain", b""

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


def run_browser(browser: Path, url: str, profile: Path):
    args = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        f"--user-data-dir={profile}",
        # Every non-loopback connection goes to a dead proxy; only 127.0.0.1
        # bypasses it. ("<-loopback>" would REMOVE the loopback bypass.)
        "--proxy-server=http://127.0.0.1:9",
        "--proxy-bypass-list=127.0.0.1",
        # No other host name resolves.
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
        "--virtual-time-budget=600000",
        "--dump-dom",
        url,
    ]

    if os.name != "nt":
        args.insert(1, "--no-sandbox")

    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
    )


@pytest.fixture(scope="module")
def browser_results(tmp_path_factory):
    browser = find_browser()

    if browser is None:
        if os.name == "nt":
            pytest.fail(f"Microsoft Edge is required on Windows and was not found at {EDGE_WINDOWS}")
        pytest.skip("no supported Chromium-family browser is installed")

    profile = tmp_path_factory.mktemp("edge-profile")

    with Harness() as harness:
        url = f"{harness.origin}/__harness__/runner?scenarios={','.join(SCENARIOS)}"
        result = run_browser(browser, url, profile)
        paths, unexpected = list(harness.paths), list(harness.unexpected)

    shutil.rmtree(profile, ignore_errors=True)

    diagnostics = (
        f"browser={browser}\nexit={result.returncode}\nstdout={result.stdout[-4000:]}\n"
        f"stderr={result.stderr[-4000:]}\npaths={paths}"
    )
    match = re.search(r'<pre id="results">(.*?)</pre>', result.stdout, re.S)
    assert result.returncode == 0 and match, diagnostics

    stderr_lines = [line for line in result.stderr.splitlines() if line.strip()]
    results = json.loads(html.unescape(match.group(1)))

    return {
        "results": results,
        "paths": paths,
        "unexpected": unexpected,
        "stderr_lines": stderr_lines,
        "diagnostics": diagnostics,
    }


def scenario(browser_results, name):
    result = browser_results["results"][name]
    assert result.get("error") is None, (name, result, browser_results["diagnostics"])
    return result


def step(result, label):
    return next(s for s in result["steps"] if s["label"] == label)


def assert_idle(snap, composer=None):
    """Controls are back to the not-busy state after a terminal outcome."""
    assert snap["readOnly"] is False
    assert snap["sendIsStop"] is False
    assert snap["sendLabel"] == "Send message"
    assert snap["sendDisabled"] is (not (snap["composer"] or "").strip())
    assert snap["heightStyle"].endswith("px")

    if composer is not None:
        assert snap["composer"] == composer


def assert_failed(snap, message, retry, user="first question"):
    """One user row and one result row carrying exactly one fixed note."""
    assert snap["users"] == [user]
    assert snap["rowOrder"] == ["user", "assistant"]
    (row,) = snap["assistants"]
    assert [note["text"] for note in row["notes"]] == [message]
    assert row["text"] == message                 # no partial answer beside the note
    assert row["actions"] == (["Retry"] if retry else [])
    assert row["visibleActionBar"] is retry


def assert_clean(result):
    for snap in result["steps"]:
        for sentinel in SENTINELS:
            assert sentinel not in snap["threadText"], (sentinel, snap["label"])


def bodies(result):
    return [request["body"] for request in result["requests"]]


# --- browser isolation ---------------------------------------------------------------------------


def test_browser_served_only_approved_loopback_paths(browser_results):
    assert browser_results["unexpected"] == []
    assert any(p.startswith("/app/app.js?v=" + CACHE_KEY) for p in browser_results["paths"])
    assert not any(p.startswith("POST") for p in browser_results["paths"])


def test_browser_reports_no_page_failure(browser_results):
    failures = [line for line in browser_results["stderr_lines"]
                if any(sign in line for sign in PAGE_FAILURE_STDERR)]
    assert failures == [], browser_results["diagnostics"]


def test_every_scenario_completed(browser_results):
    assert set(browser_results["results"]) == set(SCENARIOS)

    for name in SCENARIOS:
        assert browser_results["results"][name].get("error") is None, name


def test_no_request_left_the_page(browser_results):
    for name, result in browser_results["results"].items():
        for request in result["requests"]:
            assert request["url"] == "/api/chat" and request["method"] == "POST", name


# --- fixed backend codes ------------------------------------------------------------------------


@pytest.mark.parametrize("code", list(COPY))
def test_backend_code_shows_fixed_copy_and_action(browser_results, code):
    result = scenario(browser_results, f"code_{code}")
    failed = step(result, "failed")
    message = COPY[code]

    if code == DAILY_LIMIT_CODE:
        assert_failed(failed, message, retry=False)
        assert [n["kind"] for n in failed["assistants"][0]["notes"]] == ["notice-note"]
    elif code == "history_too_long":
        assert failed["assistants"][0]["actions"] == ["New conversation"]
        assert [n["text"] for n in failed["assistants"][0]["notes"]] == [message]
    else:
        assert_failed(failed, message, retry=code not in NO_RETRY)

    # The submitted text is restored and controls are idle again.
    assert_idle(failed, composer="first question")
    assert_clean(result)

    # The failure never enters history, and a deliberate new send replaces it.
    after = step(result, "after-new-send")
    first, second = bodies(result)
    assert first == {"message": "first question", "history": [], "session_id": None}
    assert second == {"message": "next question", "history": [], "session_id": None}
    assert after["users"] == ["next question"]
    assert after["rowOrder"] == ["user", "assistant"]
    assert after["assistants"][0]["notes"] == []
    assert after["assistants"][0]["text"].strip() == "Fine."
    assert_idle(after, composer="")


@pytest.mark.parametrize("name, message", list(GENERIC_FAILURES.items()))
def test_status_and_processing_failures_use_the_right_wording(browser_results, name, message):
    result = scenario(browser_results, name)
    failed = step(result, "failed")

    assert_failed(failed, message, retry=True)
    assert_idle(failed, composer="first question")
    assert_clean(result)

    # Only a genuine fetch rejection says the server could not be reached.
    if name != "fetch_reject":
        assert UNREACHABLE not in failed["threadText"]

    after = step(result, "after-new-send")
    assert bodies(result)[1]["history"] == []
    assert after["users"] == ["next question"]
    assert after["assistants"][0]["text"].strip() == "Fine."


def test_empty_replies_never_render_an_empty_bubble(browser_results):
    for name in ("reply_missing", "reply_empty", "reply_whitespace", "reply_not_string"):
        failed = step(scenario(browser_results, name), "failed")
        (row,) = failed["assistants"]
        # The only content is the note: no empty answer bubble, no Copy action.
        assert row["text"] == INCOMPLETE
        assert "Copy" not in row["actions"]


def test_reply_with_error_code_is_incomplete_and_commits_nothing(browser_results):
    result = scenario(browser_results, "success_reply_with_error")
    failed = step(result, "failed")
    after = step(result, "after-new-send")
    first, second = bodies(result)

    assert_failed(failed, INCOMPLETE, retry=True)
    assert "THIS MUST NOT BE RENDERED" not in failed["threadText"]
    assert "THIS MUST NOT BE RENDERED" not in after["threadText"]
    # Neither its session id nor a turn was committed.
    assert first["session_id"] is None
    assert second == {"message": "next question", "history": [], "session_id": None}


def test_rendering_failure_is_incomplete_not_unreachable(browser_results):
    result = scenario(browser_results, "render_exception")
    failed = step(result, "failed")

    assert_failed(failed, INCOMPLETE, retry=True)
    assert "<strong>" not in failed["assistants"][0]["html"]
    assert bodies(result)[1]["history"] == []


# --- success, Stop and Retry ---------------------------------------------------------------------


def test_success_renders_sanitized_markdown_and_one_turn(browser_results):
    result = scenario(browser_results, "success")
    first = step(result, "first")
    (row,) = first["assistants"]

    assert "<strong>world</strong>" in row["html"]
    assert "onerror" not in row["html"] and "<script" not in row["html"]
    assert row["notes"] == []
    assert row["actions"] == ["Copy", "Retry"]
    assert_idle(first, composer="")

    first_body, second_body = bodies(result)
    assert first_body["history"] == []
    assert second_body["history"] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant",
         "content": "Hello **world** <img src=x onerror=alert(1)><script>alert(2)</script>"},
    ]
    assert second_body["session_id"] == "s-1"


def test_stop_shows_stopped_restores_text_and_skips_history(browser_results):
    result = scenario(browser_results, "stop")
    in_flight = step(result, "in-flight")
    stopped = step(result, "stopped")

    assert in_flight["readOnly"] is True and in_flight["sendIsStop"] is True
    assert in_flight["composer"] == ""
    assert_failed(stopped, STOPPED, retry=True)
    assert stopped["assistants"][0]["notes"][0]["kind"] == "notice-note"
    assert_idle(stopped, composer="first question")
    assert bodies(result)[1]["history"] == []
    assert step(result, "after-new-send")["users"] == ["next question"]


def test_retry_after_stop_reuses_the_exchange(browser_results):
    result = scenario(browser_results, "stop_then_retry")
    retried = step(result, "retried")
    first, second = bodies(result)

    assert second == first
    assert retried["users"] == ["first question"]
    assert retried["assistants"][0]["text"].strip() == "Recovered."
    assert retried["assistants"][0]["actions"] == ["Copy", "Retry"]
    assert_idle(retried, composer="")


def test_retry_reuses_text_history_and_rows(browser_results):
    result = scenario(browser_results, "retry_success")
    failed = step(result, "failed")
    retried = step(result, "retried")
    after = step(result, "after-third")
    earlier, failing, retry, third = bodies(result)

    pair = [{"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "Earlier answer."}]
    assert failing == {"message": "failing question", "history": pair, "session_id": "s-1"}
    assert retry == failing                               # exact text and pre-send history
    assert failed["users"] == ["earlier question", "failing question"]
    assert retried["users"] == ["earlier question", "failing question"]   # no duplicate row
    assert retried["rowOrder"] == ["user", "assistant", "user", "assistant"]
    assert retried["assistants"][1]["text"].strip() == "Recovered."
    assert retried["assistants"][1]["notes"] == []
    assert_idle(retried, composer="")                     # restored text was reclaimed
    # Exactly one completed turn was added by the successful Retry.
    assert third["history"] == pair + [
        {"role": "user", "content": "failing question"},
        {"role": "assistant", "content": "Recovered."},
    ]
    assert after["users"] == ["earlier question", "failing question", "third question"]


def test_second_failure_keeps_one_failed_exchange(browser_results):
    result = scenario(browser_results, "retry_fails_again")
    snap = step(result, "failed-twice")

    assert snap["users"] == ["earlier question", "failing question"]
    assert snap["rowOrder"] == ["user", "assistant", "user", "assistant"]
    row = snap["assistants"][1]
    assert [n["text"] for n in row["notes"]] == [COPY["service_unavailable"]]
    assert row["actions"] == ["Retry"]
    assert_idle(snap, composer="failing question")


def test_successful_answer_copy_and_retry_still_work(browser_results):
    result = scenario(browser_results, "success_copy_and_retry")
    copied = step(result, "copied")
    regenerated = step(result, "regenerated")
    first, regen, nxt = bodies(result)

    assert "Copied" in copied["assistants"][0]["actions"]
    assert result["copied"] == ["Answer one."]             # the turn's own answer text
    # Same text and pre-turn history; the session the first reply opened is kept.
    assert (regen["message"], regen["history"]) == (first["message"], first["history"])
    assert (first["session_id"], regen["session_id"]) == (None, "s-1")
    assert regenerated["users"] == ["question one"]
    assert regenerated["assistants"][0]["text"].strip() == "Answer one, again."
    assert nxt["history"] == [{"role": "user", "content": "question one"},
                              {"role": "assistant", "content": "Answer one, again."}]


# --- draft protection -----------------------------------------------------------------------------


def test_newer_draft_during_a_request_is_never_overwritten(browser_results):
    snap = step(scenario(browser_results, "newer_draft_during_request"), "failed")

    assert_failed(snap, BUSY, retry=True, user="original question")
    assert_idle(snap, composer="a newer draft")


def test_edited_restored_draft_survives_a_failed_retry(browser_results):
    result = scenario(browser_results, "edited_draft_then_retry")

    assert step(result, "restored")["composer"] == "original question"
    snap = step(result, "failed-again")
    assert_idle(snap, composer="original question, edited")
    # Retry still sends the original text, not the edited draft.
    assert [b["message"] for b in bodies(result)] == ["original question"] * 2


def test_unedited_restored_draft_is_reclaimed_then_restored(browser_results):
    result = scenario(browser_results, "unedited_draft_retry")

    assert step(result, "retry-in-flight")["composer"] == ""
    assert_idle(step(result, "failed-again"), composer="original question")


def test_cleared_draft_stays_empty_after_a_failed_retry(browser_results):
    result = scenario(browser_results, "cleared_draft_then_retry")

    assert step(result, "restored")["composer"] == "original question"
    assert step(result, "cleared")["composer"] == ""
    snap = step(result, "failed-again")

    assert_failed(snap, COPY["service_unavailable"], retry=True, user="original question")
    assert_idle(snap, composer="")                       # the user's clear is kept
    first, retry = bodies(result)
    assert retry == first == {"message": "original question", "history": [], "session_id": None}


def test_retrying_a_completed_answer_removes_a_later_failed_exchange(browser_results):
    result = scenario(browser_results, "retry_completed_with_later_failure")
    failed = step(result, "failed")
    regenerated = step(result, "regenerated")
    after = step(result, "after-next")
    first, failing, regen, nxt = bodies(result)

    assert failed["users"] == ["question one", "question two"]
    assert failed["composer"] == "question two"

    # The failed exchange's two rows are gone; the first user row is kept once,
    # followed only by its regenerated answer.
    assert regenerated["users"] == ["question one"]
    assert regenerated["rowOrder"] == ["user", "assistant"]
    assert regenerated["assistants"][0]["text"].strip() == "Answer one, regenerated."
    assert regenerated["assistants"][0]["notes"] == []
    # The other exchange's restored draft is not touched.
    assert_idle(regenerated, composer="question two")

    assert failing["history"] == [{"role": "user", "content": "question one"},
                                  {"role": "assistant", "content": "Answer one."}]
    assert (regen["message"], regen["history"]) == ("question one", [])
    assert regen["session_id"] == "s-1"
    assert nxt["history"] == [{"role": "user", "content": "question one"},
                              {"role": "assistant", "content": "Answer one, regenerated."}]
    assert after["users"] == ["question one", "question three"]


# --- new conversation ------------------------------------------------------------------------------


def test_history_too_long_offers_a_real_new_conversation(browser_results):
    result = scenario(browser_results, "history_too_long_new_conversation")
    too_long = step(result, "too-long")
    reset = step(result, "reset")
    after = step(result, "after-reset-send")

    row = too_long["assistants"][1]
    assert row["actions"] == ["New conversation"]          # and no Retry
    assert [n["text"] for n in row["notes"]] == [COPY["history_too_long"]]

    # The reset sends nothing, clears every message and shows the empty state.
    assert reset["requestCount"] == too_long["requestCount"] == 2
    assert reset["users"] == [] and reset["assistants"] == [] and reset["rowOrder"] == []
    assert reset["emptyShown"] is True and reset["conversationEmpty"] is True
    assert reset["focusIsComposer"] is True
    assert_idle(reset, composer="a draft to keep")

    # TURNS and the session were cleared through the real state.
    assert bodies(result)[2] == {"message": "fresh question", "history": [], "session_id": None}
    assert after["users"] == ["fresh question"]
    assert after["emptyShown"] is False


# --- input limit and the embedded handoff signal ---------------------------------------------------


def test_input_over_4000_characters_is_sent_once_and_refused_with_fixed_copy(browser_results):
    result = scenario(browser_results, "message_too_long_4001")
    failed = step(result, "failed")
    (body,) = bodies(result)

    # The client neither truncates nor blocks it; the backend's 4,000-character
    # limit answers message_too_long and the fixed copy is shown.
    assert body == {"message": "x" * 4001, "history": [], "session_id": None}
    assert_failed(failed, COPY["message_too_long"], retry=False, user="x" * 4001)
    assert_idle(failed, composer="x" * 4001)


# --- the anonymous daily limit ----------------------------------------------------------------


def assert_dialog_open(snap):
    dialog = snap["limitDialog"]
    assert dialog["open"] is True and dialog["modal"] is True, snap["label"]
    assert dialog["text"] == DAILY_LIMIT_DIALOG
    assert dialog["focusIsOk"] is True and snap["focusIsComposer"] is False


def assert_dialog_closed(snap, focus_composer=True):
    assert snap["limitDialog"]["open"] is False, snap["label"]
    assert snap["limitDialog"]["modal"] is False
    if focus_composer:
        assert snap["focusIsComposer"] is True, snap["label"]


def assert_limit_notice(snap, composer="first question", user="first question"):
    assert_failed(snap, DAILY_LIMIT_NOTE, retry=False, user=user)
    assert [n["kind"] for n in snap["assistants"][0]["notes"]] == ["notice-note"]
    assert_idle(snap, composer=composer)


def test_daily_limit_opens_the_neutral_dialog_and_got_it_closes_it(browser_results):
    result = scenario(browser_results, f"code_{DAILY_LIMIT_CODE}")
    failed, closed, after = step(result, "failed"), step(result, "closed"), step(result, "after-new-send")

    assert_dialog_open(failed)
    assert_limit_notice(failed)          # no Retry, the draft restored
    # The inline notice stays after the dialog closes; focus returns to the composer.
    assert_dialog_closed(closed)
    assert_limit_notice(closed)
    assert_dialog_closed(after)
    assert after["assistants"][0]["text"].strip() == "Fine."


def test_daily_limit_escape_closes_through_the_cancel_handler(browser_results):
    result = scenario(browser_results, "daily_limit_escape")
    assert_dialog_open(step(result, "open"))
    closed = step(result, "closed")
    assert closed["limitDialog"]["cancelPrevented"] is True   # One close function, not the default.
    assert_dialog_closed(closed)
    assert_limit_notice(closed)


def test_daily_limit_backdrop_click_closes_but_a_panel_click_does_not(browser_results):
    result = scenario(browser_results, "daily_limit_backdrop")
    assert_dialog_open(step(result, "panel-click"))
    closed = step(result, "closed")
    assert_dialog_closed(closed)
    assert_limit_notice(closed)


def test_daily_limit_dialog_reopens_without_error(browser_results):
    result = scenario(browser_results, "daily_limit_reopen")
    assert_dialog_closed(step(result, "first-closed"))
    reopened = step(result, "reopened")
    assert_dialog_open(reopened)
    assert_limit_notice(reopened, composer="second question", user="second question")
    assert_dialog_closed(step(result, "second-closed"))
    assert [b["message"] for b in bodies(result)] == ["first question", "second question"]


def test_daily_limit_without_show_modal_shows_only_the_inline_notice(browser_results):
    failed = step(scenario(browser_results, "daily_limit_without_show_modal"), "failed")
    assert_dialog_closed(failed)
    assert_limit_notice(failed)


def test_daily_limit_dialog_fits_a_360_by_640_viewport(browser_results):
    snap = step(scenario(browser_results, "daily_limit_layout_360x640"), "open")
    assert_dialog_open(snap)
    layout = snap["layout"]
    assert layout["viewport"] == [360, 640]
    left, top, right, bottom = layout["dialog"]
    assert 0 <= left < right <= 360 and 0 <= top < bottom <= 640, layout
    b_left, b_top, b_right, b_bottom = layout["button"]
    assert left <= b_left < b_right <= right and top <= b_top < b_bottom <= bottom, layout
    assert b_bottom - b_top >= 44, layout
    assert layout["dialogOverflowX"] <= 0 and layout["dialogOverflowY"] <= 0, layout
    assert layout["pageOverflowX"] <= 0, layout


def test_busy_and_bare_429_keep_retry_and_never_open_the_dialog(browser_results):
    for name in ("code_busy", "bare_429"):
        failed = step(scenario(browser_results, name), "failed")
        assert_failed(failed, BUSY, retry=True)
        assert failed["limitDialog"]["open"] is False, name


def test_dialog_stays_closed_unless_the_daily_limit_code_arrives(browser_results):
    limit_scenarios = {f"code_{DAILY_LIMIT_CODE}", *DAILY_LIMIT_SCENARIOS} - {"daily_limit_without_show_modal"}
    for name, result in browser_results["results"].items():
        snaps = [s for s in result["steps"] if "limitDialog" in s]
        assert snaps, name
        for snap in snaps:
            assert snap["limitDialog"] is not None, name
            if name not in limit_scenarios:
                assert snap["limitDialog"]["open"] is False, (name, snap["label"])
                assert snap["limitDialog"]["text"] == DAILY_LIMIT_DIALOG   # Never rewritten.


def test_daily_limit_never_shows_server_text(browser_results):
    for name in (f"code_{DAILY_LIMIT_CODE}", *DAILY_LIMIT_SCENARIOS):
        result = scenario(browser_results, name)
        assert_clean(result)
        for snap in result["steps"]:
            assert snap["limitDialog"]["text"] == DAILY_LIMIT_DIALOG, (name, snap["label"])
            for sentinel in SENTINELS:
                assert sentinel not in snap["limitDialog"]["text"], (name, sentinel)


def daily_limit_markup() -> str:
    page = INDEX_HTML.read_text(encoding="utf-8")
    start = page.index('<dialog class="limit-dialog"')
    return page[start:page.index("</dialog>", start) + len("</dialog>")]


def test_daily_limit_markup_is_fixed_accessible_copy():
    markup = daily_limit_markup()
    page = INDEX_HTML.read_text(encoding="utf-8")

    assert page.count("<dialog") == 1
    assert ('<dialog class="limit-dialog" id="limit-dialog" aria-labelledby="limit-dialog-title" '
            'aria-describedby="limit-dialog-text limit-dialog-reset">') in markup
    assert f'<h2 class="limit-dialog-title" id="limit-dialog-title">{DAILY_LIMIT_TITLE}</h2>' in markup
    assert f'<p class="limit-dialog-text" id="limit-dialog-text">{DAILY_LIMIT_NOTE}</p>' in markup
    assert f'<p class="limit-dialog-text" id="limit-dialog-reset">{DAILY_LIMIT_RESET}</p>' in markup
    assert (f'<button type="button" class="limit-dialog-ok" id="limit-dialog-ok">{DAILY_LIMIT_BUTTON}'
            '</button>') in markup
    assert "Retry" not in markup


def test_daily_limit_copy_avoids_unapproved_claims():
    source = js_source()
    outcome = source[source.index("var OUTCOME_DAILY_LIMIT"):]
    outcome = outcome[: outcome.index("\n")]
    for text in (daily_limit_markup().lower(), outcome.lower(), f'"{DAILY_LIMIT_NOTE}"'.lower()):
        for word in DAILY_LIMIT_FORBIDDEN:
            assert word not in text, word
    assert f'var COPY_DAILY_LIMIT = "{DAILY_LIMIT_NOTE}";' in source
    assert 'action: null, dialog: true' in outcome


def test_daily_limit_dialog_script_contract():
    source = js_source()

    # Only a 429 carrying the exact code opens it.
    assert 'if (code === "anonymous_daily_limit_reached" && status !== 429) code = null;' in source
    assert "anonymous_daily_limit_reached: OUTCOME_DAILY_LIMIT" in source
    # Feature-detected, opened once, focus on Got it.
    assert 'typeof limitDialog.showModal !== "function"' in source
    assert "if (!limitDialog.open) limitDialog.showModal();" in source
    assert "limitDialogOk.focus();" in source
    # Escape is the cancel event: its default is prevented and one function closes.
    cancel = source[source.index('limitDialog.addEventListener("cancel"'):]
    cancel = cancel[: cancel.index("});")]
    assert "event.preventDefault();" in cancel and "closeLimitDialog();" in cancel
    assert 'limitDialogOk.addEventListener("click", closeLimitDialog);' in source
    assert "if (event.target === limitDialog) closeLimitDialog();" in source
    close = source[source.index("function closeLimitDialog()"):]
    close = close[: close.index("\n  }")]
    assert "input.focus({ preventScroll: true });" in close
    # A settled send never pulls focus out of the open dialog.
    assert source.count(".then(focusComposer);") == 3
    assert "if (!inFlight && !limitDialogOpen()) input.focus({ preventScroll: true });" in source
    # No client-side counter, countdown or lockout, and no clock.
    for forbidden in ("Date(", "setInterval", "countdown", "remaining"):
        assert forbidden not in source[source.index("// ---- daily-limit dialog"):source.index("function restoreDraft")], forbidden


def test_daily_limit_dialog_styles():
    css = (FRONTEND / "app.css").read_text(encoding="utf-8")
    block = css[css.index("/* ---------------- Daily-limit dialog"):]

    assert ".limit-dialog {" in block and "width: min(400px, calc(100vw - 2 * var(--rail)));" in block
    assert "max-height: calc(100dvh - 32px);" in block and "overflow: auto;" in block
    ok = block[block.index(".limit-dialog-ok {"):]
    assert "min-height: 44px;" in ok[: ok.index("}")]
    reduced = block[block.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".limit-dialog[open] { animation: none; }" in reduced
    for token in ("var(--panel)", "var(--ink)", "var(--muted)", "var(--line)", "var(--radius)",
                  "var(--shadow)", "var(--blue)", "var(--font-ui)"):
        assert token in block, token


# --- syntax highlighting ---------------------------------------------------------------------------


def rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in re.findall(r"\d+", value)[:3])


def contrast(color: tuple[int, int, int], background: str) -> float:
    def lum(c):
        def channel(v):
            v = v / 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b = (channel(v) for v in c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    bg = tuple(int(background[i:i + 2], 16) for i in (1, 3, 5))
    hi, lo = sorted((lum(color), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def rendered_code(browser_results, name):
    return step(scenario(browser_results, name), "rendered")


@pytest.mark.parametrize("name", CODE_SCENARIOS)
def test_known_languages_get_restrained_visible_token_colors(browser_results, name):
    blocks = {b["language"]: b for b in rendered_code(browser_results, name)["code"]}
    assert set(blocks) == set(CODE_SOURCES)
    for language in HIGHLIGHTED_LANGUAGES:
        block = blocks[language]
        assert block["highlighted"] is True and block["spans"] > 0, language
        # Only token spans with classes: no other element or attribute.
        assert set(block["tags"]) == {"span"} and set(block["attrs"]) == {"class"}, language
    for language, classes in {
        "python": {"hljs-keyword", "hljs-string", "hljs-comment"},
        "javascript": {"hljs-keyword", "hljs-string", "hljs-comment", "hljs-number"},
        "sql": {"hljs-keyword", "hljs-number"},
        "json": {"hljs-attr", "hljs-string", "hljs-number"},
        "html": {"hljs-tag", "hljs-name", "hljs-attr", "hljs-string"},
    }.items():
        assert classes <= set(blocks[language]["colors"]), (language, blocks[language]["colors"])
    # Keywords, strings, comments and plain code are visibly different colors.
    python = blocks["python"]
    distinct = {python["colors"]["hljs-keyword"], python["colors"]["hljs-string"],
                python["colors"]["hljs-comment"], python["plainColor"]}
    assert len(distinct) == 4, python["colors"]
    # Every rendered token color stays readable on the code background.
    for language in HIGHLIGHTED_LANGUAGES:
        for token, color in blocks[language]["colors"].items():
            assert contrast(rgb(color), CODE_BACKGROUND) >= 4.5, (language, token, color)


@pytest.mark.parametrize("name", CODE_SCENARIOS)
def test_highlighting_never_changes_the_code_text(browser_results, name):
    for block in rendered_code(browser_results, name)["code"]:
        assert block["text"] == CODE_SOURCES[block["language"]], block["language"]


@pytest.mark.parametrize("name", CODE_SCENARIOS)
def test_unknown_and_unlabeled_fences_stay_readable_plain_code(browser_results, name):
    blocks = {b["language"]: b for b in rendered_code(browser_results, name)["code"]}
    for language in ("foobarlang", ""):
        assert blocks[language]["highlighted"] is False and blocks[language]["spans"] == 0
        assert blocks[language]["text"] == CODE_SOURCES[language]


@pytest.mark.parametrize("name", CODE_SCENARIOS)
def test_inline_code_and_prose_are_not_highlighted(browser_results, name):
    snap = rendered_code(browser_results, name)
    assert snap["inlineCode"] == [{"text": "code", "spans": 0, "highlighted": False}]
    assert snap["prose"] == [
        "Here is the Python version.",
        "Inline code stays plain. This explanation remains normal prose outside the code block.",
    ]


@pytest.mark.parametrize("name", CODE_SCENARIOS)
def test_script_looking_code_is_displayed_and_never_executed(browser_results, name):
    snap = rendered_code(browser_results, name)
    assert snap["page"]["scripts"] == 0 and snap["page"]["images"] == 0
    assert snap["page"]["alerts"] == [] and snap["page"]["executed"] is False
    html_block = next(b for b in snap["code"] if b["language"] == "html")
    assert html_block["text"] == CODE_SOURCES["html"]


@pytest.mark.parametrize("name", CODE_SCENARIOS)
def test_copy_returns_the_original_reply_text(browser_results, name):
    assert scenario(browser_results, name)["copied"] == [CODE_REPLY]


def test_highlighted_code_fits_a_narrow_mobile_viewport(browser_results):
    snap = rendered_code(browser_results, "syntax_highlighting_360x640")
    assert snap["page"]["viewport"] == [360, 640]
    assert snap["page"]["overflowX"] <= 0                      # no page-level horizontal overflow
    assert all(block["preRight"] <= 360 for block in snap["code"])
    # Long lines scroll inside their own block, as before.
    assert any(block["preScrolls"] for block in snap["code"])


def test_vendored_highlightjs_is_the_verified_local_release():
    import hashlib
    assert hashlib.sha256(HIGHLIGHT_JS.read_bytes()).hexdigest() == HIGHLIGHT_JS_SHA256
    assert hashlib.sha256(HIGHLIGHT_LICENSE.read_bytes()).hexdigest() == HIGHLIGHT_LICENSE_SHA256
    source = HIGHLIGHT_JS.read_text(encoding="utf-8")
    assert source.startswith("/*!\n  Highlight.js v11.12.0")
    assert "BSD 3-Clause License" in HIGHLIGHT_LICENSE.read_text(encoding="utf-8")
    for network in ("fetch(", "XMLHttpRequest", "sendBeacon", "import(", "WebSocket", "eval(", "new Function"):
        assert network not in source, network
    assert {p.name for p in HIGHLIGHT_JS.parent.iterdir()} == {"highlight.min.js", "LICENSE"}
    page = INDEX_HTML.read_text(encoding="utf-8")
    tag = '<script src="vendor/highlight/highlight.min.js?v=11.12.0"></script>'
    assert page.count(tag) == 1 and page.index(tag) < page.index('<script src="app.js?v=')


def test_highlighting_keeps_the_sanitizer_boundary():
    source = APP_JS.read_text(encoding="utf-8")
    render = source[source.index("function renderAssistantText"):source.index("function highlightCode")]
    # Highlighting runs after Markdown is sanitized, on the sanitized DOM.
    assert render.index("renderMarkdownSafe(") < render.index("highlightCode(shell.body);")
    block = source[source.index("function highlightCode"):]
    block = block[: block.index("\n  }\n")]
    assert 'container.querySelectorAll("pre > code")' in block           # fenced code only
    assert "hljs.getLanguage(match[1])" in block                          # known labels only
    assert "var source = code.textContent;" in block                      # from the text, not markup
    assert "window.DOMPurify.sanitize(tokens, HIGHLIGHT_PURIFY_CONFIG)" in block
    assert "if (code.textContent === source)" in block and "code.textContent = source;" in block
    config = source[source.index("var HIGHLIGHT_PURIFY_CONFIG"):]
    config = config[: config.index("};")]
    assert 'ALLOWED_TAGS: ["span"]' in config and 'ALLOWED_ATTR: ["class"]' in config
    for automatic in ("highlightAll", "highlightElement", "highlightBlock"):
        assert automatic not in source, automatic


def test_syntax_token_colors_meet_contrast_on_the_code_background():
    css = (FRONTEND / "app.css").read_text(encoding="utf-8")
    block = css[css.index("/* Syntax highlighting (highlight.js token classes)"):]
    block = block[: block.index(".md .hljs-emphasis")]
    colors = set(re.findall(r"(?<![-\w])color: (#[0-9a-f]{6});", block))
    assert len(colors) >= 6, colors
    for color in colors:
        value = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        assert contrast(value, CODE_BACKGROUND) >= 4.5, color
    # Colors only: no font-size, spacing or layout change for highlighted code.
    for property_name in ("font-size", "padding", "margin", "white-space", "display", "overflow"):
        assert property_name + ":" not in block, property_name


def test_embedded_chat_never_messages_its_parent(browser_results):
    """Every scenario runs framed by the runner (its parent window). The chat
    posts nothing to its parent: the homepage workspace has one stable size,
    so there is no resize or handoff signal of any kind."""
    for name, result in browser_results["results"].items():
        assert result["parentMessages"] == [], name

    source = APP_JS.read_text(encoding="utf-8")
    assert "postMessage" not in source
    assert 'addEventListener("message"' not in source


# --- static contract (always runs) -----------------------------------------------------------------


def js_source() -> str:
    # Join adjacent string-literal concatenations so copy split across lines
    # still matches exactly.
    return re.sub(r'"\s*\+\s*\n?\s*"', "", APP_JS.read_text(encoding="utf-8"))


REPOSITORIES = REPO / "candidate" / "kalillac_db" / "repositories"
# Account sign-in errors reach the chat only with accounts enabled; their
# frontend copy is a separate, later change.
DEFERRED_UNMAPPED = {"not_authenticated"}


def quota_error_codes() -> set[str]:
    """The fixed code of every QuotaError subclass in the repositories, read
    statically (the app raises these as _budget_error(status, defect.code))."""
    import ast

    classes = {}
    for path in REPOSITORIES.glob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ClassDef):
                bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", None) for b in node.bases}
                code = next((stmt.value.value for stmt in node.body
                             if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                             and getattr(stmt.targets[0], "id", None) == "code"
                             and isinstance(stmt.value, ast.Constant)), None)
                classes[node.name] = (bases, code)

    def is_quota_error(name, seen=()):
        if name == "QuotaError":
            return True
        if name not in classes or name in seen:
            return False
        return any(is_quota_error(base, seen + (name,)) for base in classes[name][0])

    codes = set()
    for name, (bases, code) in classes.items():
        if is_quota_error(name):
            # A subclass without its own code inherits its parent's.
            current = name
            while code is None and classes.get(current):
                parents = [b for b in classes[current][0] if b in classes]
                if not parents:
                    break
                current = parents[0]
                code = classes[current][1]
            if code is not None:
                codes.add(code)
    return codes


def backend_error_codes() -> set[str]:
    source = BACKEND.read_text(encoding="utf-8")
    codes = set(re.findall(r'_budget_error\(\s*\d+\s*,\s*"([a-z_]+)"\s*\)', source))
    codes |= set(re.findall(r'"error"\s*:\s*"([a-z_]+)"', source))
    codes |= {"empty_message", "message_too_long", "history_too_long", "invalid_request"}
    codes |= quota_error_codes()
    return codes - DEFERRED_UNMAPPED


def test_quota_error_codes_are_discovered_statically():
    codes = quota_error_codes()
    assert {DAILY_LIMIT_CODE, "busy", "service_unavailable"} <= codes, codes
    # The deferred code is discovered too, so it cannot silently disappear.
    assert DEFERRED_UNMAPPED <= codes
    assert DAILY_LIMIT_CODE in backend_error_codes()


def test_every_backend_error_code_is_mapped():
    codes = backend_error_codes()
    source = js_source()

    assert {"processing_limit_reached", "service_unavailable", "request_timeout"} <= codes
    assert codes <= set(COPY), codes - set(COPY)

    for code in codes:
        assert re.search(rf"\b{code}:", source), code


def test_every_fixed_message_is_exact():
    source = js_source()

    for message in set(COPY.values()) | {UNREACHABLE, INCOMPLETE, BUSY, STOPPED}:
        assert f'"{message}"' in source, message


def test_no_provider_name_appears_in_the_frontend_script():
    source = APP_JS.read_text(encoding="utf-8").lower()

    for name in ("openai", "tavily", "groq", "cloudflare", "llama", "gpt-"):
        assert name not in source, name


def test_only_the_fetch_rejection_path_uses_network_wording():
    source = js_source()

    assert source.count(UNREACHABLE) == 1
    assert "couldn't reach" not in source.replace(UNREACHABLE, "")
    # The outcome carrying it is defined once and used once, for a rejected fetch.
    assert source.count("OUTCOME_UNREACHABLE") == 2
    rejection = source[source.index("No HTTP response was received"):]
    rejection = rejection[: rejection.index("})")]
    assert "OUTCOME_UNREACHABLE" in rejection


def test_later_stage_failures_use_completion_wording():
    source = js_source()

    assert f'"{INCOMPLETE}"' in source
    assert source.count("OUTCOME_INCOMPLETE") >= 5


def test_app_js_cache_key():
    page = INDEX_HTML.read_text(encoding="utf-8")

    assert re.findall(r'src="app\.js\?v=([^"]+)"', page) == [CACHE_KEY]
    assert re.findall(r'href="app\.css\?v=([^"]+)"', page) == [CSS_CACHE_KEY]
    assert page.count("?v=0.18.4") == 3


def test_no_public_url_or_cdn_dependency():
    for path in (APP_JS, INDEX_HTML):
        assert not re.search(r"https?://", path.read_text(encoding="utf-8")), path


def test_production_and_storage_boundaries():
    source = APP_JS.read_text(encoding="utf-8")

    # API use, not the words (the header comment says none of these are used).
    for forbidden in (r"\blocalStorage\s*[.\[]", r"\bsessionStorage\s*[.\[]",
                      r"\bindexedDB\s*\.", r"document\.cookie", r"\bconsole\.",
                      r"location\.reload", r"sendBeacon", r"__HARNESS"):
        assert not re.search(forbidden, source), forbidden


def test_only_app_js_app_css_and_index_html_changed_in_the_frontend():
    """Relative to the imported baseline, the frontend source changed only in
    app.js, app.css and index.html (vendored assets are untouched)."""
    git = shutil.which("git")
    assert git, "git is required for this check"

    changed = subprocess.run(
        [git, "diff", "--name-only", BASELINE_COMMIT, "--", "frontend"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split()
    untracked = subprocess.run(
        [git, "ls-files", "--others", "--exclude-standard", "--", "frontend"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split()

    # Plus exactly the two vendored highlight.js files (pinned by hash below).
    assert set(changed) | set(untracked) <= {
        "frontend/app.js", "frontend/app.css", "frontend/index.html",
    } | VENDORED_HIGHLIGHT
