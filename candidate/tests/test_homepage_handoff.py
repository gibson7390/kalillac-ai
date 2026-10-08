"""Homepage -> /app/ handoff: executable browser tests and static checks.

The browser tests serve, from loopback only, the BUILT marketing site
(site/dist/public) at "/" and the canonical chat (frontend/) at "/app/" --
the routing production's web server is expected to provide -- plus a second
loopback origin that plays the cross-origin attacker. Headless Microsoft Edge
(or another Chromium-family browser), driven over the DevTools protocol at an
exact 1440 x 900 desktop and 390 x 844 touch-phone viewport, runs the real
pages with
frontend_harness/handoff_parent.js injected into the site and
frontend_harness/handoff_child.js injected into the chat. Nothing leaves the
machine: the chat's fetch is scripted, every non-loopback connection goes to a
dead proxy, and no other host name resolves.

Build the site first (cd site && npm run build). Without a build the browser
tests skip; with a build older than the site sources they fail.

The static checks always run.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

import pytest

from test_frontend_error_contract import EDGE_WINDOWS, find_browser


REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
SITE = REPO / "site"
SITE_SRC = SITE / "src"
SITE_DIST = SITE / "dist" / "public"
HARNESS = Path(__file__).resolve().parent / "frontend_harness"
APP_JS = FRONTEND / "app.js"
APP_CSS = FRONTEND / "app.css"
INDEX_HTML = FRONTEND / "index.html"
APP_TSX = SITE_SRC / "App.tsx"
PROTOCOL_TS = SITE_SRC / "embed-protocol.ts"
SITE_CSS = SITE_SRC / "index.css"
BACKEND = REPO / "candidate" / "app_fastapi_candidate.py"

PROMPT = "kalillac:embed:prompt-submitted"
PRESENTATION = "kalillac:embed:presentation"

VIEWPORTS = {"desktop": (1440, 900), "mobile": (390, 844)}
# The compact card's fixed height (site/src/index.css), by viewport.
CARD_HEIGHT = {"desktop": 188, "mobile": 232}

FIRST = "What is 2 + 2?"
SECOND = "And 3 + 3?"
PROMPT_FRAGMENTS = ("2 + 2", "2+%2B", "2%20%2B", "What", "3 + 3", "And%203")


# --- loopback servers --------------------------------------------------------------------------


class LoopbackServer:
    def __init__(self, route):
        self.paths: list[str] = []
        self.reports: list[dict] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, status, ctype, body):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                server.paths.append(self.path)
                self._send(*route(self.path))

            def do_POST(self):
                server.paths.append("POST " + self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)

                if self.path == "/__handoff__/report":
                    server.reports.append(json.loads(body))
                    self._send(204, "text/plain", b"")
                else:
                    self._send(404, "text/plain", b"")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


CONTENT_TYPES = {
    ".js": "text/javascript", ".css": "text/css", ".html": "text/html; charset=utf-8",
    ".png": "image/png", ".svg": "image/svg+xml", ".txt": "text/plain",
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
}


def serve_file(root: Path, relative: str):
    target = (root / relative).resolve()

    if root.resolve() in target.parents and target.is_file():
        return 200, CONTENT_TYPES.get(target.suffix, "application/octet-stream"), target.read_bytes()

    return 404, "text/plain", b""


class Site:
    """Origin A: the built site at /, the canonical chat at /app/ (as the
    production web server routes them), and the test helpers."""

    def __init__(self):
        self.mode = ""
        self.cross_origin = ""
        self.http = LoopbackServer(self.route)

    def site_page(self) -> bytes:
        page = (SITE_DIST / "index.html").read_text(encoding="utf-8")
        probe = (
            f"<script>window.__HANDOFF_MODE__ = {json.dumps(self.mode)};"
            f" window.__HANDOFF_CROSS_ORIGIN__ = {json.dumps(self.cross_origin)};</script>"
            '<script src="/__handoff__/parent.js"></script>'
        )
        assert page.count("<head>") == 1
        return page.replace("<head>", "<head>" + probe, 1).encode("utf-8")

    def chat_page(self) -> bytes:
        page = INDEX_HTML.read_text(encoding="utf-8")
        marker = "<!-- Local vendored libraries only"
        assert page.count(marker) == 1
        return page.replace(
            marker, '<script src="/__handoff__/child.js"></script>\n  ' + marker, 1
        ).encode("utf-8")

    def route(self, raw_path: str):
        path = raw_path.split("?", 1)[0]

        if path in ("/", "/privacy", "/terms"):
            return 200, "text/html; charset=utf-8", self.site_page()
        if path == "/app/":
            return 200, "text/html; charset=utf-8", self.chat_page()
        if path.startswith("/app/"):
            return serve_file(FRONTEND, path[len("/app/"):])
        if path == "/__handoff__/parent.js":
            return 200, "text/javascript", (HARNESS / "handoff_parent.js").read_bytes()
        if path == "/__handoff__/child.js":
            return 200, "text/javascript", (HARNESS / "handoff_child.js").read_bytes()
        if path == "/__handoff__/blank":
            return 200, "text/html; charset=utf-8", b"<!DOCTYPE html><html><body></body></html>"
        if path.startswith("/__"):
            return 404, "text/plain", b""

        return serve_file(SITE_DIST, path.lstrip("/"))


class CrossOrigin:
    """Origin B: pages that send protocol messages from the wrong origin."""

    def __init__(self, site_origin: str):
        self.site_origin = site_origin
        self.http = LoopbackServer(self.route)

    def route(self, raw_path: str):
        path = raw_path.split("?", 1)[0]
        valid_prompt = json.dumps({"type": PROMPT, "version": 1})
        valid_expand = json.dumps({"type": PRESENTATION, "version": 1, "expanded": True})

        if path == "/__handoff__/xorigin-sender":
            page = ("<!DOCTYPE html><html><body><script>"
                    f"for (var i = 0; i < 5; i++) parent.postMessage({valid_prompt}, '*');"
                    "</script></body></html>")
            return 200, "text/html; charset=utf-8", page.encode("utf-8")

        if path == "/__handoff__/xorigin-parent":
            page = ("<!DOCTYPE html><html><body>"
                    f'<iframe id="f" src="{self.site_origin}/app/" style="width:380px;height:240px"></iframe>'
                    "<script>var f = document.getElementById('f');"
                    "f.addEventListener('load', function () { var n = 0;"
                    " var t = setInterval(function () {"
                    f" f.contentWindow.postMessage({valid_expand}, '*');"
                    " if (++n >= 100) clearInterval(t); }, 100); });"
                    "</script></body></html>")
            return 200, "text/html; charset=utf-8", page.encode("utf-8")

        return 404, "text/plain", b""


# --- browser (DevTools protocol for exact device metrics) ------------------------------------------


class DevTools:
    """A minimal Chrome DevTools Protocol client over the browser websocket.
    Headless window sizes cannot go below ~500 CSS px wide, so the exact
    390 x 844 phone viewport (and 1440 x 900 desktop) is set with
    Emulation.setDeviceMetricsOverride instead."""

    def __init__(self, url: str):
        from websockets.sync.client import connect

        self.ws = connect(url, max_size=None, open_timeout=30)
        self.next_id = 0
        self.events: list[dict] = []

    def call(self, method: str, params: dict | None = None, session: str | None = None,
             timeout: float = 60) -> dict:
        self.next_id += 1
        message = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.ws.send(json.dumps(message))
        deadline = time.monotonic() + timeout

        while True:
            data = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            if data.get("id") == self.next_id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})
            if "method" in data:
                self.events.append(data)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def run_browser(browser: Path, url: str, profile: Path, size: tuple[int, int], mobile: bool,
                done, timeout: float = 120, screenshot: Path | None = None) -> dict:
    """Open url at exactly `size` CSS pixels and wait until done() is true.
    Returns {"stderr", "timed_out", "requests"} -- requests is every URL the
    page and its frames asked the network for; optionally saves a PNG."""
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
        "--hide-scrollbars",
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        # Every non-loopback connection goes to a dead proxy; only 127.0.0.1
        # (any port) bypasses it, and no other host name resolves.
        "--proxy-server=http://127.0.0.1:9",
        "--proxy-bypass-list=127.0.0.1",
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
        "about:blank",
    ]

    if os.name != "nt":
        args.insert(1, "--no-sandbox")

    stderr_path = profile / "browser-stderr.txt"
    with open(stderr_path, "wb") as stderr:
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr)

    devtools = None
    timed_out = False
    requests: list[str] = []

    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 30
        while not (port_file.is_file() and len(port_file.read_text().split()) >= 2):
            assert time.monotonic() < deadline, "the browser did not open its DevTools port"
            time.sleep(0.1)
        port, path = port_file.read_text().split()[:2]

        devtools = DevTools(f"ws://127.0.0.1:{port}{path}")
        target = devtools.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = devtools.call("Target.attachToTarget",
                                {"targetId": target, "flatten": True})["sessionId"]
        devtools.call("Emulation.setDeviceMetricsOverride",
                      {"width": size[0], "height": size[1], "deviceScaleFactor": 1,
                       "mobile": mobile}, session)
        if mobile:
            devtools.call("Emulation.setTouchEmulationEnabled",
                          {"enabled": True, "maxTouchPoints": 5}, session)
        devtools.call("Page.enable", {}, session)
        devtools.call("Network.enable", {}, session)
        devtools.call("Page.navigate", {"url": url}, session)

        deadline = time.monotonic() + timeout
        while not done():
            if time.monotonic() > deadline:
                timed_out = True
                break
            time.sleep(0.1)

        if screenshot is not None and not timed_out:
            shot = devtools.call("Page.captureScreenshot", {"format": "png"}, session)
            screenshot.write_bytes(base64.b64decode(shot["data"]))

        # A round trip drains every queued event, then read the request log.
        devtools.call("Runtime.evaluate", {"expression": "1"}, session)
        requests = [e["params"]["request"]["url"] for e in devtools.events
                    if e.get("method") == "Network.requestWillBeSent"]
    finally:
        if devtools is not None:
            try:
                devtools.call("Browser.close", timeout=10)
            except Exception:
                pass
            devtools.close()
        try:
            process.wait(20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(10)

    return {"stderr": stderr_path.read_text(encoding="utf-8", errors="replace"),
            "timed_out": timed_out, "requests": requests}


def require_browser_and_build() -> Path:
    browser = find_browser()

    if browser is None:
        if os.name == "nt":
            pytest.fail(f"Microsoft Edge is required on Windows and was not found at {EDGE_WINDOWS}")
        pytest.skip("no supported Chromium-family browser is installed")

    built = SITE_DIST / "index.html"

    if not built.is_file():
        pytest.skip("site build output not found; run `npm run build` in site/ first")

    sources = [p for p in SITE_SRC.rglob("*") if p.is_file()]
    sources += [SITE / "index.html", SITE / "vite.config.ts", SITE / "package.json"]
    newest = max(p.stat().st_mtime for p in sources)
    assert built.stat().st_mtime >= newest, "site/dist is older than site sources; rebuild it"

    return browser


@pytest.fixture(scope="module", params=list(VIEWPORTS))
def handoff_run(request, tmp_path_factory):
    browser = require_browser_and_build()
    viewport = request.param
    profile = tmp_path_factory.mktemp(f"edge-handoff-{viewport}")

    site = Site()
    with site.http:
        cross = CrossOrigin(site.http.origin)
        with cross.http:
            site.mode = "main"
            site.cross_origin = cross.http.origin
            result = run_browser(
                browser, site.http.origin + "/", profile, VIEWPORTS[viewport],
                mobile=viewport == "mobile", done=lambda: len(site.http.reports) >= 2,
            )
            reports = list(site.http.reports)
            paths = list(site.http.paths)

    shutil.rmtree(profile, ignore_errors=True)
    diagnostics = (f"viewport={viewport}\ntimed_out={result['timed_out']}\n"
                   f"stderr={result['stderr'][-3000:]}\n"
                   f"paths={paths}\nreports={json.dumps(reports)[:6000]}")

    assert result["timed_out"] is False, diagnostics
    parent = [r for r in reports if r.get("phase") == "parent"]
    standalone = [r for r in reports if r.get("phase") == "standalone"]
    assert len(parent) == 1 and len(standalone) == 1, diagnostics
    assert parent[0]["errors"] == [], diagnostics
    assert standalone[0]["errors"] == [], diagnostics

    return {"viewport": viewport, "size": VIEWPORTS[viewport], "parent": parent[0],
            "refreshed": standalone[0], "paths": paths, "origin": site.http.origin,
            "cross_origin": cross.http.origin, "diagnostics": diagnostics}


@pytest.fixture(scope="module")
def direct_visit(tmp_path_factory):
    browser = require_browser_and_build()
    profile = tmp_path_factory.mktemp("edge-direct")

    site = Site()
    with site.http:
        result = run_browser(browser, site.http.origin + "/app/", profile, VIEWPORTS["desktop"],
                             mobile=False, done=lambda: len(site.http.reports) >= 1)
        reports = list(site.http.reports)

    shutil.rmtree(profile, ignore_errors=True)
    assert result["timed_out"] is False, result["stderr"][-3000:]
    assert len(reports) == 1 and reports[0]["phase"] == "standalone", reports
    assert reports[0]["errors"] == [], reports
    return reports[0]


def step(run, label):
    return next(s for s in run["parent"]["steps"] if s["label"] == label)


def assert_collapsed(snap, run, requests):
    assert snap["path"] == "/"
    assert snap["cardExpanded"] is False
    assert snap["childExpanded"] is False
    assert snap["childEmbedded"] is True
    assert snap["childHeaderShown"] is False and snap["childFooterShown"] is False
    # The card never grows with the conversation.
    assert snap["cardRect"]["height"] == CARD_HEIGHT[run["viewport"]], snap["cardRect"]
    assert len(snap["requests"]) == requests


def assert_expanded(snap, run, requests, settled=False):
    """Expanded: the frame covers the full viewport from the first frame of
    the transition. Its clip-path reveal runs for 320 ms, so only a settled
    snapshot must also be the topmost element at the very top edge (proving
    the sticky site header cannot cover the chat)."""
    width, height = run["size"]
    assert snap["path"] == "/app/"
    assert snap["cardExpanded"] is True
    assert snap["childExpanded"] is True
    assert snap["childHeaderShown"] is True and snap["childFooterShown"] is True
    assert snap["viewport"] == {"width": width, "height": height}
    assert snap["cardRect"] == {"top": 0, "left": 0, "width": width, "height": height,
                                "bottom": height, "right": width}
    assert snap["frameIsTopAtCenter"] is True
    if settled:
        assert snap["frameIsTopAtTop"] is True and snap["elementAtTop"] == "iframe"
    assert snap["htmlOverflow"] == "hidden" and snap["bodyOverflow"] == "hidden"
    assert len(snap["requests"]) == requests


def assert_same_iframe(snap, run):
    assert snap["sameFrameWindow"] is True
    assert snap["childLoadedAt"] == step(run, "loaded")["childLoadedAt"]


# --- handoff ---------------------------------------------------------------------------------------


def test_homepage_starts_as_a_fixed_compact_card(handoff_run):
    loaded = step(handoff_run, "loaded")

    assert_collapsed(loaded, handoff_run, requests=0)
    assert loaded["search"] == "" and loaded["hash"] == ""
    assert loaded["users"] == []
    # The composer and Send are visible and on top inside the card.
    assert loaded["composerHit"] is True and loaded["sendHit"] is True
    assert 0 <= loaded["composerRect"]["top"] and loaded["composerRect"]["bottom"] <= loaded["childViewportHeight"]


def test_first_prompt_expands_immediately_and_is_sent_exactly_once(handoff_run):
    submitted = step(handoff_run, "submitted")

    # 60 ms after Send, before the scripted 300 ms reply: already expanded.
    assert_expanded(submitted, handoff_run, requests=1)
    assert_same_iframe(submitted, handoff_run)
    assert "Reply 1." not in submitted["assistants"]
    assert submitted["users"] == [FIRST]
    (request,) = submitted["requests"]
    assert request["url"] == "/api/chat" and request["method"] == "POST"
    assert request["body"] == {"message": FIRST, "history": [], "session_id": None}
    assert submitted["historyLength"] == step(handoff_run, "after-control-back")["historyLength"]


def test_reply_stays_in_the_same_iframe_with_a_usable_composer(handoff_run):
    replied = step(handoff_run, "replied")
    probes = handoff_run["parent"]["probes"]

    assert_expanded(replied, handoff_run, requests=1, settled=True)
    assert_same_iframe(replied, handoff_run)
    assert replied["users"] == [FIRST]
    assert replied["assistants"] == ["Reply 1."]
    # Composer and Send are inside the viewport and not covered.
    assert replied["childViewportHeight"] == handoff_run["size"][1]
    assert 0 <= replied["composerRect"]["top"] < replied["composerRect"]["bottom"] <= replied["childViewportHeight"]
    assert replied["sendRect"]["bottom"] <= replied["childViewportHeight"]
    assert replied["composerHit"] is True and replied["sendHit"] is True
    assert probes["composerFocused"] is True and probes["sendEnabledWithDraft"] is True


def test_no_nested_scroll_container(handoff_run):
    for label in ("submitted", "replied", "forward", "second-replied"):
        snap = step(handoff_run, label)
        # The homepage behind the chat cannot scroll; inside the chat only the
        # conversation scrolls, never the chat document itself.
        assert snap["htmlOverflow"] == "hidden" and snap["bodyOverflow"] == "hidden", label
        assert snap["childDocScroll"]["scrollHeight"] <= snap["childDocScroll"]["clientHeight"], label
        assert snap["conversationOverflowY"] == "auto", label

    collapsed = step(handoff_run, "back")
    assert collapsed["htmlOverflow"] != "hidden"
    assert collapsed["childDocScroll"]["scrollHeight"] <= collapsed["childDocScroll"]["clientHeight"]


def test_back_returns_to_the_homepage_without_resending(handoff_run):
    back = step(handoff_run, "back")

    assert_collapsed(back, handoff_run, requests=1)
    assert_same_iframe(back, handoff_run)
    assert back["users"] == [FIRST]                  # shown once, never duplicated
    assert back["assistants"] == ["Reply 1."]

    forward = step(handoff_run, "forward")
    assert_expanded(forward, handoff_run, requests=1)
    assert_same_iframe(forward, handoff_run)
    assert forward["users"] == [FIRST]

    again = step(handoff_run, "back-again")
    assert_collapsed(again, handoff_run, requests=1)
    assert again["users"] == [FIRST]


def test_a_later_prompt_from_the_card_hands_off_again(handoff_run):
    submitted = step(handoff_run, "second-submitted")
    replied = step(handoff_run, "second-replied")

    assert_expanded(submitted, handoff_run, requests=2)
    assert_expanded(replied, handoff_run, requests=2, settled=True)
    assert_same_iframe(replied, handoff_run)
    assert submitted["requests"][1]["body"] == {
        "message": SECOND,
        "history": [{"role": "user", "content": FIRST}, {"role": "assistant", "content": "Reply 1."}],
        "session_id": "s-1",
    }
    assert replied["users"] == [FIRST, SECOND]
    assert replied["assistants"] == ["Reply 1.", "Reply 2."]
    assert len(replied["requests"]) == 2           # every prompt exactly once, in total


def test_prompt_never_enters_the_url_or_history_state(handoff_run):
    for snap in handoff_run["parent"]["steps"]:
        assert snap["search"] == "" and snap["hash"] == "", snap["label"]
        assert snap["childSearch"] == "" and snap["childHash"] == "", snap["label"]
        assert snap["childPath"] == "/app/", snap["label"]
        for fragment in PROMPT_FRAGMENTS:
            assert fragment not in snap["href"], (snap["label"], fragment)
            assert fragment not in snap["historyState"], (snap["label"], fragment)

    for url in handoff_run["parent"]["visitedUrls"]:
        assert url in (handoff_run["origin"] + "/", handoff_run["origin"] + "/app/"), url

    # Nothing the server saw carries the prompt either.
    for path in handoff_run["paths"]:
        for fragment in PROMPT_FRAGMENTS:
            assert fragment not in path, path


def test_no_browser_storage_or_cookie_writes(handoff_run):
    parent = handoff_run["parent"]

    assert parent["parentWrites"] == [] and parent["childWrites"] == []
    assert parent["parentLocalStorage"] == 0 and parent["parentSessionStorage"] == 0
    assert parent["childLocalStorage"] == 0 and parent["childSessionStorage"] == 0
    assert parent["parentCookie"] == "" and parent["childCookie"] == ""
    assert parent["databases"] == []

    refreshed = handoff_run["refreshed"]
    assert refreshed["writes"] == []
    assert refreshed["localStorageLength"] == 0 and refreshed["sessionStorageLength"] == 0
    assert refreshed["cookie"] == "" and refreshed["databases"] == []


def test_refreshed_app_is_a_fresh_standalone_session(handoff_run):
    refreshed = handoff_run["refreshed"]
    assert_fresh_standalone(refreshed)
    # The refresh really was a top-level load of /app/ served as the chat.
    assert handoff_run["paths"].count("/app/") >= 2


def test_direct_app_visit_is_a_fresh_standalone_session(direct_visit):
    assert_fresh_standalone(direct_visit)
    assert direct_visit["writes"] == []
    assert direct_visit["localStorageLength"] == 0 and direct_visit["sessionStorageLength"] == 0
    assert direct_visit["cookie"] == ""


def assert_fresh_standalone(report):
    before, after = report["before"], report["after"]

    assert before["path"] == "/app/" and before["search"] == "" and before["hash"] == ""
    assert before["embedded"] is False and before["expanded"] is False
    assert before["headerShown"] is True
    assert before["emptyShown"] is True and before["conversationEmpty"] is True
    assert before["users"] == [] and before["requests"] == []
    (request,) = after["requests"]
    assert request["body"] == {"message": "after refresh", "history": [], "session_id": None}
    assert after["users"] == ["after refresh"]


# --- protocol rejection ------------------------------------------------------------------------------


def test_parent_rejects_every_unexpected_sender(handoff_run):
    loaded = step(handoff_run, "loaded")
    received = handoff_run["parent"]["received"]
    origin, cross = handoff_run["origin"], handoff_run["cross_origin"]

    for label in ("parent-own-window", "parent-sibling-window", "parent-cross-origin",
                  "parent-invalid-payloads"):
        snap = step(handoff_run, label)
        assert_collapsed(snap, handoff_run, requests=0)
        assert snap["historyLength"] == loaded["historyLength"], label

    # Each rejected message really was delivered.
    assert any(m["fromSelf"] and m["type"] == PROMPT for m in received)
    assert any(m["origin"] == origin and not m["fromChat"] and not m["fromSelf"]
               and m["type"] == PROMPT for m in received)
    assert any(m["origin"] == cross and m["type"] == PROMPT for m in received)
    assert sum(1 for m in received if m["fromChat"]) >= 12


def test_parent_accepts_the_exact_message_and_it_sends_nothing(handoff_run):
    control = step(handoff_run, "parent-valid-message-only")

    assert_expanded(control, handoff_run, requests=0)
    assert control["historyLength"] == step(handoff_run, "loaded")["historyLength"] + 1
    assert_collapsed(step(handoff_run, "after-control-back"), handoff_run, requests=0)


def test_chat_rejects_every_unexpected_sender(handoff_run):
    probes = handoff_run["parent"]["probes"]
    received = handoff_run["parent"]["childReceived"]
    origin = handoff_run["origin"]

    assert probes["childInvalidFromParent"] is False         # bad type/version/keys
    assert probes["childFromSibling"] is False               # same origin, wrong window
    assert probes["childFromItself"] is False                # same origin, its own window
    assert probes["childFromCrossOriginParent"] is False     # wrong origin
    assert probes["crossOriginParentDelivered"] >= 1

    # Each rejected message really was delivered.
    assert any(m["fromSelf"] and m["type"] == PRESENTATION for m in received)
    assert any(m["origin"] == origin and not m["fromParent"] and not m["fromSelf"]
               and m["type"] == PRESENTATION for m in received)
    assert sum(1 for m in received if m["fromParent"]) >= 9

    # Positive control: the exact message from the real parent is honoured.
    assert probes["childFromRealParent"] is True
    assert probes["childCollapsedByRealParent"] is True


# --- static contract (always runs) ---------------------------------------------------------------------


def site_sources() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in SITE_SRC.rglob("*")
            if p.suffix in (".ts", ".tsx", ".css")}


def test_protocol_constants_match_on_both_sides():
    js = APP_JS.read_text(encoding="utf-8")
    ts = PROTOCOL_TS.read_text(encoding="utf-8")

    assert f'EMBED_PROMPT_SUBMITTED = "{PROMPT}"' in js
    assert f'EMBED_PRESENTATION = "{PRESENTATION}"' in js
    assert "EMBED_PROTOCOL_VERSION = 1;" in js
    assert f"PROMPT_SUBMITTED = '{PROMPT}'" in ts
    assert f"PRESENTATION = '{PRESENTATION}'" in ts
    assert "EMBED_PROTOCOL_VERSION = 1;" in ts


def test_messages_are_posted_only_to_the_exact_origin():
    js = APP_JS.read_text(encoding="utf-8")
    ts = PROTOCOL_TS.read_text(encoding="utf-8")

    for source in [js, ts, *site_sources().values()]:
        assert not re.search(r"""postMessage\([^;]*['"]\*['"]""", source)

    assert js.count("postMessage(") == 1
    assert re.search(r"postMessage\(\s*\{ type: EMBED_PROMPT_SUBMITTED, version: EMBED_PROTOCOL_VERSION \},\s*"
                     r"window\.location\.origin\s*\)", js)
    assert ts.count("postMessage(") == 1 and "origin," in ts
    # The incoming checks: exact origin and exact window on both sides.
    assert "event.origin !== window.location.origin" in js
    assert "event.source !== window.parent" in js
    assert "event.origin !== origin" in ts
    assert "event.source !== frameWindow" in ts


def test_no_mutation_observer_storage_or_url_transfer():
    sources = {APP_JS: APP_JS.read_text(encoding="utf-8"), **site_sources()}

    for path, source in sources.items():
        assert "MutationObserver" not in source, path
        # API use, not the words (comments and the Privacy page name them).
        for forbidden in (r"\blocalStorage\s*[.\[]", r"\bsessionStorage\s*[.\[]", r"\bindexedDB\s*\.",
                          r"document\.cookie", r"\bcookieStore\s*\.", r"URLSearchParams",
                          r"location\.hash\s*=", r"location\.search\s*=", r"[?&#]prompt="):
            assert not re.search(forbidden, source), (path, forbidden)

    app = APP_TSX.read_text(encoding="utf-8")
    # The URL change is exactly a pushState to the fixed /app/ path.
    assert app.count("pushState(") == 1
    assert "window.history.pushState({ kalillac: 'chat' }, '', CHAT_PATH);" in app
    assert "CHAT_PATH = '/app/'" in PROTOCOL_TS.read_text(encoding="utf-8")


def test_site_contains_no_duplicate_chat_or_replit_leftovers():
    names = {p.relative_to(SITE).as_posix() for p in SITE.rglob("*")
             if p.is_file() and "node_modules" not in p.parts and "dist" not in p.parts}
    app = APP_TSX.read_text(encoding="utf-8")
    package = json.loads((SITE / "package.json").read_text(encoding="utf-8"))
    deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}

    assert not any("app_1790308213208" in n for n in names)
    assert not any(n.startswith("src/components/ui/") for n in names)
    assert "ChatPage" not in app and 'path="/app"' not in app
    for leftover in ("marked", "dompurify", "katex", "has-conversation", "appjsurl"):
        assert leftover not in app.lower(), leftover
    assert "has-conversation" not in SITE_CSS.read_text(encoding="utf-8")
    assert not any(k.startswith("@replit/") for k in deps)
    for chat_lib in ("marked", "dompurify", "katex"):
        assert chat_lib not in deps
    assert "PORT" not in (SITE / "vite.config.ts").read_text(encoding="utf-8")


def test_homepage_copy_and_navigation():
    app = APP_TSX.read_text(encoding="utf-8")

    assert "Private by design.<br /><span>Powerful when it matters.</span>" in app
    assert ("Ask questions, develop ideas, write and troubleshoot code, or search the current "
            "web—without creating an account or building a permanent chat history.") in app
    assert "Start a private session" in app
    assert ("Temporary sessions <span className=\"note-divider\">·</span> No account required "
            "<span className=\"note-divider\">·</span>") in app and ">Clear provider disclosure<" in app
    for item in (">Product<", ">Privacy<", ">How it works<", ">Try Kalillac<"):
        assert item in app, item
    for unfinished in ("Pricing", "Sign in", "Sign up", "Create account", "Log in"):
        assert unfinished not in app, unfinished


def test_privacy_wording_boundaries():
    app = APP_TSX.read_text(encoding="utf-8")
    text = re.sub(r"<[^>]+>", " ", app)
    lower = text.lower()

    for claim in ("completely private", "nothing is stored", "nobody else",
                  "refresh deletes", "refreshing deletes", "refresh erases",
                  # Carried-over claims this repository cannot prove.
                  "debug", "zero data retention", "zero-retention", "retention marketing",
                  "google", "legal approval", "legally approved", "lawyer"):
        assert claim not in lower, claim
    # "anonymous" only in the explicit disclaimer.
    assert lower.count("anonymous") == lower.count("not a promise that the service is anonymous") == 1

    # Groq and Workers AI appear only inside the one sentence saying they are
    # not in the current path (once on Privacy, once in Terms).
    approved = "Cloudflare Workers AI and Groq are not part of the current active model-provider path."
    assert text.count(approved) == 2
    assert text.count("Groq") == text.count("Workers AI") == 2

    for required in (
        "Kalillac does not save chat transcripts as persistent conversations in its own application database.",
        "Active-session context may remain in server memory until eviction or service restart.",
        "Refreshing or closing the page removes the browser’s access to that session.",
        "That does not promise immediate deletion of the server-memory entry.",
        "OpenAI may process requests sent for model inference.",
        "When web search is used, relevant query text may be sent to Tavily.",
        "Cloudflare provides edge delivery and security for",
        "may process network metadata",
        "Cloudflare Workers AI and Groq are not part of the current active model-provider path.",
        "Do not submit information you cannot allow OpenAI, Tavily, or Cloudflare to process.",
        "No account or paid subscription is currently offered.",
    ):
        assert required in text, required


# --- no automatic third-party resources ---------------------------------------------------------


EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//", re.I)


def test_site_sources_load_nothing_from_another_origin():
    page = (SITE / "index.html").read_text(encoding="utf-8")
    css = SITE_CSS.read_text(encoding="utf-8")

    for tag in re.findall(r"<(?:link|script)\b[^>]*>", page):
        for url in re.findall(r"\b(?:href|src)=\"([^\"]*)\"", tag):
            assert not EXTERNAL.match(url), tag
    assert "@import url(" not in css
    for url in re.findall(r"url\(\s*['\"]?([^'\")]+)", css):
        assert not EXTERNAL.match(url), url
    for source in [page, css, *site_sources().values()]:
        for host in ("fonts.googleapis.com", "fonts.gstatic.com"):
            assert host not in source, host


def test_built_site_loads_nothing_from_another_origin():
    """Static check of the production build: no Google Fonts URL anywhere, no
    external stylesheet, script, module, preload, preconnect or icon in the
    HTML, and no external @import or url() in the CSS."""
    if not (SITE_DIST / "index.html").is_file():
        pytest.skip("site build output not found; run `npm run build` in site/ first")

    page = (SITE_DIST / "index.html").read_text(encoding="utf-8")
    files = [p for p in SITE_DIST.rglob("*") if p.suffix in (".html", ".css", ".js")]
    assert any(p.suffix == ".js" for p in files) and any(p.suffix == ".css" for p in files)

    for path in files:
        source = path.read_text(encoding="utf-8")
        for host in ("fonts.googleapis.com", "fonts.gstatic.com", "googleapis", "gstatic"):
            assert host not in source, (path, host)
        if path.suffix == ".css":
            assert not re.search(r"@import", source), path
            for url in re.findall(r"url\(\s*['\"]?([^'\")]+)", source):
                assert not EXTERNAL.match(url), (path, url)

    for tag in re.findall(r"<(?:link|script|img|iframe|source)\b[^>]*>", page):
        for url in re.findall(r"\b(?:href|src|srcset)=\"([^\"]*)\"", tag):
            assert not EXTERNAL.match(url), tag
    for rel in ("preconnect", "dns-prefetch", "prefetch"):
        assert f'rel="{rel}"' not in page, rel


@pytest.mark.parametrize("path", ["/", "/privacy", "/terms"])
def test_pages_request_nothing_from_another_origin(path, tmp_path_factory):
    """Runtime proof: every network request made while loading the page (and
    the framed chat on the homepage) goes to the page's own origin."""
    browser = require_browser_and_build()
    profile = tmp_path_factory.mktemp("edge-requests")

    site = Site()
    with site.http:
        started = time.monotonic()
        result = run_browser(browser, site.http.origin + path, profile, VIEWPORTS["desktop"],
                             mobile=False, done=lambda: time.monotonic() - started > 3)
        origin = site.http.origin

    shutil.rmtree(profile, ignore_errors=True)
    requests = result["requests"]
    assert requests and requests[0] == origin + path
    if path == "/":
        assert origin + "/app/" in requests             # the framed chat was loaded too
    for url in requests:
        assert url.startswith(origin + "/") or url.startswith("data:"), url


def test_backend_input_limit_is_unchanged():
    source = BACKEND.read_text(encoding="utf-8")
    assert re.search(r"^MAX_INPUT_CHARS = 4000$", source, re.M)
    # The chat client does not truncate or cap input itself; the backend's
    # message_too_long contract (test_frontend_error_contract) enforces it.
    assert "maxlength" not in INDEX_HTML.read_text(encoding="utf-8").lower()


# --- live public assets are never overwritten by the build ----------------------------------------


PRESERVE_MANIFEST = SITE / "deployment-preserve-live-assets.txt"
PRESERVED_LIVE_ASSETS = [
    "/apple-touch-icon.png",
    "/favicon-96x96.png",
    "/favicon.ico",
    "/favicon.svg",
    "/google96aef4c4924e7f64.html",
    "/icon-192.png",
    "/icon-512.png",
    "/robots.txt",
    "/site.webmanifest",
    "/sitemap.xml",
]


def test_preserve_manifest_lists_exactly_the_live_assets():
    raw = PRESERVE_MANIFEST.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    assert raw.decode("ascii").splitlines() == PRESERVED_LIVE_ASSETS


def test_site_sources_ship_none_of_the_live_assets():
    """The established live files exist only on the server. The site must not
    carry its own copies, which the deployment would otherwise write over
    them (the old placeholder favicon.svg and incomplete robots.txt)."""
    for asset in PRESERVED_LIVE_ASSETS:
        assert not (SITE / "public" / asset.lstrip("/")).exists(), asset

    page = (SITE / "index.html").read_text(encoding="utf-8")
    assert re.findall(r"<link\b[^>]*\brel=\"icon\"[^>]*>", page) == ['<link rel="icon" href="/favicon.ico" />']
    assert "favicon.svg" not in page


def test_built_site_contains_no_live_asset_and_no_manifest():
    if not (SITE_DIST / "index.html").is_file():
        pytest.skip("site build output not found; run `npm run build` in site/ first")

    for asset in PRESERVED_LIVE_ASSETS:
        assert not (SITE_DIST / asset.lstrip("/")).exists(), asset
    assert not any(p.name == PRESERVE_MANIFEST.name for p in SITE_DIST.rglob("*"))

    page = (SITE_DIST / "index.html").read_text(encoding="utf-8")
    assert '<link rel="icon" href="/favicon.ico" />' in page
    assert "favicon.svg" not in page
