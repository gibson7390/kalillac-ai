"""Homepage composer to /app/: shared browser harness, the one-time draft
handoff, the direct /app/ visit, and static checks.

The browser harness serves, from loopback only, the BUILT marketing site
(site/dist/public) at "/" and the canonical chat (frontend/) at "/app/" --
the routing production's web server provides -- with
frontend_harness/handoff_child.js injected into the chat (scripted fetch, an
ordered record of storage writes and requests). Headless Microsoft Edge (or
another Chromium-family browser) is driven over the DevTools protocol with
trusted input. Nothing leaves the machine: the chat's fetch is scripted, every
non-loopback connection goes to a dead proxy, and no other host name resolves.
test_homepage_interaction.py and test_homepage_first_load.py use this harness.

The flow: compose on "/", press Send, converse on "/app/". Only Send moves the
draft, as one versioned envelope in this tab's sessionStorage that /app/
removes before it sends the message -- once. Nothing is put in the address,
a cookie or localStorage, and the homepage never loads /app/ by itself.
/app/ records every taken nonce in a bounded, message-free ledger and sends
only before its own deadline; the homepage gives a draft back from page
memory when the chat does not open. Storage faults, a pinned clock for exact
deadline boundaries and a held-back /app/ cover both sides.

Build the site first (cd site && npm run build). Without a build the browser
tests skip; with a build older than the site sources they fail.

The static checks always run.
"""

from __future__ import annotations

import base64
import contextlib
import http.server
import json
import math
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
SITE_CSS = SITE_SRC / "index.css"
BACKEND = REPO / "candidate" / "app_fastapi_candidate.py"

VIEWPORTS = {"desktop": (1440, 900), "mobile": (390, 844)}
# The handoff's sessionStorage keys (site/src/App.tsx and frontend/app.js).
HANDOFF_KEY, LEDGER_KEY = "kalillac.handoff", "kalillac.handoff.ledger"
# The homepage's notices when the chat does not open in time (site/src/App.tsx) ...
COPY_STALLED = "The chat didn’t open, so your message wasn’t sent. It is back in the message box."
COPY_STALLED_HELD = ("The chat didn’t open, so your message wasn’t sent, but this browser didn’t let the page "
                     "withdraw it. So that it can’t be sent twice, it stays here read-only. You can still select "
                     "and copy it.")
# ... on a Back before the chat took the draft ...
COPY_RETURNED = "Your message wasn’t sent. It is back in the message box."
COPY_HELD = ("Your message hasn’t been sent, but this browser didn’t let the page withdraw it from the chat. So that "
             "it can’t be sent twice, it stays here read-only. You can still select and copy it.")
COPY_HELD_UNKNOWN = ("This browser didn’t let the page check whether the chat took your message. So that it can’t "
                     "be sent twice, it stays here read-only. You can still select and copy it.")
COPY_HELD_TOO_OLD = ("This page can no longer check whether the chat took your message, because the chat has taken "
                     "a newer one since. So that it can’t be sent twice, it stays here read-only. You can still "
                     "select and copy it.")
# ... and /app/'s, for a homepage message it did not send automatically (frontend/app.js).
COPY_APP_LATE = ("The chat took too long to open, so your message wasn’t sent. It’s in the message box. "
                 "Press Send to send it.")
COPY_APP_UNCHECKED = ("The chat didn’t send your message automatically, because this browser’s storage didn’t "
                      "allow its check against sending a message twice. It’s in the message box. Press Send to "
                      "send it.")
COPY_APP_LEFT = ("Your message wasn’t sent, and it is still on the homepage: this browser’s storage didn’t let the "
                 "chat take it. Go back to the homepage to send it from there.")
COPY_APP_UNVERIFIED = ("Kalillac couldn’t verify this message’s status. To prevent it from being sent twice, it "
                       "remains read-only. You can select and copy it.")
COPY_APP_LEFT_UNVERIFIED = ("Kalillac couldn’t verify this message’s status, so this chat didn’t take it. If the "
                            "homepage still has it, you can send it from there.")


# --- loopback servers --------------------------------------------------------------------------


class LoopbackServer:
    def __init__(self, route):
        self.paths: list[str] = []
        self.reports: list[dict] = []
        # Paths served "no-cache" instead of "no-store": only those pages may
        # enter the browser's back/forward cache.
        self.cacheable: set[str] = set()
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, status, ctype, body):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                cacheable = self.path.split("?", 1)[0] in server.cacheable
                self.send_header("Cache-Control", "no-cache" if cacheable else "no-store")
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
        # When set, a top-level /app/ does not run handoff_child.js's own
        # standalone check; the test drives the page instead.
        self.child_passive = False
        # Seconds the /app/ document is held back (a stalled navigation).
        self.app_delay = 0.0
        # Seconds /app/app.js is held back: the chat's page has opened, but its
        # script -- the only code that takes the envelope -- has not run.
        self.script_delay = 0.0
        # Markup placed in /app/ before every other script: a test's storage
        # fault or pinned clock for that document only.
        self.app_prelude = ""
        # When set, /app/ runs app.js twice in the one document: repeated initialization.
        self.app_script_twice = False
        self.http = LoopbackServer(self.route)

    def site_page(self) -> bytes:
        return (SITE_DIST / "index.html").read_bytes()

    def chat_page(self) -> bytes:
        page = INDEX_HTML.read_text(encoding="utf-8")
        marker = "<!-- Local vendored libraries only"
        assert page.count(marker) == 1
        if self.app_script_twice:
            script = re.search(r'<script src="app\.js[^"]*"></script>', page).group(0)
            assert page.count(script) == 1
            page = page.replace(script, script + "\n  " + script)
        passive = "<script>window.__HANDOFF_PASSIVE__ = true;</script>" if self.child_passive else ""
        return page.replace(
            marker, self.app_prelude + passive + '<script src="/__handoff__/child.js"></script>\n  ' + marker, 1
        ).encode("utf-8")

    def route(self, raw_path: str):
        path = raw_path.split("?", 1)[0]

        if path in ("/", "/privacy", "/terms"):
            page = self.site_page() if path == "/" else (SITE_DIST / (path.lstrip("/") + ".html")).read_bytes()
            return 200, "text/html; charset=utf-8", page
        if path == "/app/":
            if self.app_delay:
                time.sleep(self.app_delay)
            return 200, "text/html; charset=utf-8", self.chat_page()
        if path.startswith("/app/"):
            if path == "/app/app.js" and self.script_delay:
                time.sleep(self.script_delay)
            return serve_file(FRONTEND, path[len("/app/"):])
        if path == "/__handoff__/child.js":
            return 200, "text/javascript", (HARNESS / "handoff_child.js").read_bytes()
        if path.startswith("/__"):
            return 404, "text/plain", b""

        return serve_file(SITE_DIST, path.lstrip("/"))


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


@contextlib.contextmanager
def browser_page(browser: Path, profile: Path, size: tuple[int, int], mobile: bool,
                 reduced_motion: bool = False, zoom_percent: int | None = None, switches: tuple[str, ...] = ()):
    """Launch an isolated headless browser with one blank page at exactly
    `size` CSS pixels (touch emulation when mobile; prefers-reduced-motion
    emulated on request). Yields (devtools, session); always closes it.

    With `zoom_percent`, `size` is instead the browser WINDOW, and the page is
    shown at that real browser page zoom: the profile's default zoom level
    (the setting Ctrl+Plus and Settings > Page zoom change) is set before
    launch, and no viewport or device-pixel emulation is applied, so the
    browser's own zoom decides the CSS viewport."""
    if zoom_percent is not None:
        # Chromium stores page zoom as a level: percent = 100 * 1.2 ** level.
        level = math.log(zoom_percent / 100) / math.log(1.2)
        (profile / "Default").mkdir(parents=True, exist_ok=True)
        (profile / "Default" / "Preferences").write_text(
            json.dumps({"partition": {"default_zoom_level": {"x": level}}}), encoding="utf-8")
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
        *([f"--window-size={size[0]},{size[1]}"] if zoom_percent is not None else []),
        # Every non-loopback connection goes to a dead proxy; only 127.0.0.1
        # (any port) bypasses it, and no other host name resolves.
        "--proxy-server=http://127.0.0.1:9",
        "--proxy-bypass-list=127.0.0.1",
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
        *switches,
        "about:blank",
    ]

    if os.name != "nt":
        args.insert(1, "--no-sandbox")

    with open(profile / "browser-stderr.txt", "wb") as stderr:
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr)

    devtools = None

    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 30
        while True:
            assert time.monotonic() < deadline, "the browser did not open its DevTools port"
            # Edge can create the file before releasing its Windows write lock.
            # Only this bounded startup read may wait; no test/assertion timeout grows.
            try:
                fields = port_file.read_text(encoding="utf-8").split()
            except (FileNotFoundError, PermissionError):
                fields = []
            if len(fields) >= 2:
                port, path = fields[:2]
                break
            time.sleep(0.1)

        devtools = DevTools(f"ws://127.0.0.1:{port}{path}")
        target = devtools.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = devtools.call("Target.attachToTarget",
                                {"targetId": target, "flatten": True})["sessionId"]
        if zoom_percent is None:
            devtools.call("Emulation.setDeviceMetricsOverride",
                          {"width": size[0], "height": size[1], "deviceScaleFactor": 1,
                           "mobile": mobile}, session)
        if mobile:
            devtools.call("Emulation.setTouchEmulationEnabled",
                          {"enabled": True, "maxTouchPoints": 5}, session)
        devtools.call("Emulation.setEmulatedMedia", {"features": [
            {"name": "prefers-reduced-motion", "value": "reduce" if reduced_motion else "no-preference"},
        ]}, session)
        devtools.call("Page.enable", {}, session)
        devtools.call("Network.enable", {}, session)
        yield devtools, session
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


def run_browser(browser: Path, url: str, profile: Path, size: tuple[int, int], mobile: bool,
                done, timeout: float = 120, screenshot: Path | None = None) -> dict:
    """Open url at exactly `size` CSS pixels and wait until done() is true.
    Returns {"stderr", "timed_out", "requests"} -- requests is every URL the
    page and its frames asked the network for; optionally saves a PNG."""
    timed_out = False

    with browser_page(browser, profile, size, mobile) as (devtools, session):
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

    return {"stderr": (profile / "browser-stderr.txt").read_text(encoding="utf-8", errors="replace"),
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
    sources += [SITE / "index.html", SITE / "vite.config.ts", SITE / "public-metadata.ts", SITE / "package.json"]
    newest = max(p.stat().st_mtime for p in sources)
    assert built.stat().st_mtime >= newest, "site/dist is older than site sources; rebuild it"

    return browser


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


# --- direct /app/ ----------------------------------------------------------------------------------


def test_direct_app_visit_is_a_fresh_standalone_session(direct_visit):
    assert_fresh_standalone(direct_visit)
    assert direct_visit["writes"] == []
    assert direct_visit["localStorageLength"] == 0 and direct_visit["sessionStorageLength"] == 0
    assert direct_visit["cookie"] == ""


def assert_fresh_standalone(report):
    before, after = report["before"], report["after"]

    assert before["path"] == "/app/" and before["search"] == "" and before["hash"] == ""
    assert before["embedded"] is False
    assert before["headerShown"] is True
    assert before["emptyShown"] is True and before["conversationEmpty"] is True
    assert before["users"] == [] and before["requests"] == []
    (request,) = after["requests"]
    assert request["body"] == {"message": "after refresh", "history": [], "session_id": None}
    assert after["users"] == ["after refresh"]


# --- static contract (always runs) ---------------------------------------------------------------------


def site_sources() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in SITE_SRC.rglob("*")
            if p.suffix in (".ts", ".tsx", ".css")}


def test_homepage_never_frames_messages_or_rewrites_history():
    app = APP_TSX.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    for source in (app, js, SITE_CSS.read_text(encoding="utf-8"), APP_CSS.read_text(encoding="utf-8")):
        assert "postMessage" not in source and "<iframe" not in source and "is-embedded" not in source
        assert 'addEventListener("message"' not in source and "addEventListener('message'" not in source
    for source in (app, js):
        assert "pushState" not in source and "replaceState" not in source and "popstate" not in source
    # The chat is a separate document at the fixed /app/ path, opened only by Send.
    assert "const CHAT_PATH = '/app/';" in app
    assert app.count("window.location.assign(CHAT_PATH);") == 1 and "location.href =" not in app
    assert not (SITE_SRC / "embed-protocol.ts").exists()
    assert not (HARNESS / "handoff_parent.js").exists()


def test_storage_is_only_the_one_time_sessionstorage_handoff():
    sources = {APP_JS: APP_JS.read_text(encoding="utf-8"), **site_sources()}

    for path, source in sources.items():
        assert "MutationObserver" not in source, path
        # API use, not the words (comments and the Privacy page name them).
        for forbidden in (r"\blocalStorage\s*[.\[]", r"\bindexedDB\s*\.", r"document\.cookie",
                          r"\bcookieStore\s*\.", r"URLSearchParams", r"location\.hash\s*=",
                          r"location\.search\s*=", r"[?&#]prompt=", r"\bwindow\.name\b",
                          r"BroadcastChannel", r"console\.(log|info|debug)\("):
            assert not re.search(forbidden, source), (path, forbidden)
    # sessionStorage: the homepage composer's envelope and /app/'s consumption only, by these exact keys.
    users = {path.name for path, source in sources.items()
             if re.search(r"\bsessionStorage\b", re.sub(r"//.*|/\*[\s\S]*?\*/", "", source))}
    assert users == {"App.tsx", "app.js"}, users
    app, js = APP_TSX.read_text(encoding="utf-8"), APP_JS.read_text(encoding="utf-8")
    assert "const HANDOFF_KEY = 'kalillac.handoff';" in app and 'var HANDOFF_KEY = "kalillac.handoff";' in js
    assert 'var HANDOFF_LEDGER_KEY = "kalillac.handoff.ledger";' in js
    assert "const HANDOFF_MAX_AGE_MS = 60_000;" in app and "var HANDOFF_MAX_AGE_MS = 60000;" in js
    assert "const MESSAGE_MAX_CHARS = 4000;" in app and "var HANDOFF_MAX_CHARS = 4000;" in js
    # The ledger keeps every taken nonce for the whole acceptance window plus the
    # clock allowance, up to a hard capacity.
    assert "var HANDOFF_SKEW_MS = 5000;" in js and "var HANDOFF_LEDGER_MAX = 32;" in js
    assert "var HANDOFF_RETAIN_MS = HANDOFF_MAX_AGE_MS + HANDOFF_SKEW_MS;" in js
    # The homepage knows how long a taken nonce stays in that ledger: the same window and allowance.
    assert "const HANDOFF_RETAIN_MS = HANDOFF_MAX_AGE_MS + 5_000;" in app
    # /app/'s own send deadline ends before the homepage gives a draft back, so
    # a draft the homepage has given back can never be sent automatically.
    send_ms = int(re.search(r"var HANDOFF_SEND_MS = (\d+);", js).group(1))
    stall_ms = int(re.search(r"const HANDOFF_STALL_MS = ([\d_]+);", app).group(1).replace("_", ""))
    assert (send_ms, stall_ms) == (8000, 10000) and send_ms < stall_ms
    # The homepage reads (never writes) the chat's ledger, by the same key, shape and limit.
    assert "const HANDOFF_LEDGER_KEY = 'kalillac.handoff.ledger';" in app and "const HANDOFF_LEDGER_MAX = 32;" in app
    assert "sessionStorage.setItem(HANDOFF_LEDGER_KEY" not in app and "sessionStorage.removeItem(HANDOFF_LEDGER_KEY" not in app
    assert "const HANDOFF_MAX_AGE_MS = 60_000;" in app
    composer = app[app.index("function HomeComposer("):app.index("function focusComposer(")]
    assert set(re.findall(r"sessionStorage\.(\w+)", app)) == {"getItem", "setItem", "removeItem"}
    assert set(re.findall(r"store\.(\w+)\(", js)) == {"getItem", "setItem", "removeItem"}
    # The envelope holds the draft, a nonce, its version and its time -- nothing else.
    assert "{ v: HANDOFF_VERSION, draft: text, nonce: newNonce(), createdAt: Date.now() }" in composer
    assert 'Object.keys(envelope).sort().join() !== "createdAt,draft,nonce,v"' in js
    # /app/ removes the envelope before it checks or sends anything -- except one too old for it ever to
    # take, which it leaves exactly where it is -- and records its nonce before deciding to send it; only
    # that decision calls send().
    take = js[js.index("function takeHandoff()"):js.index("function hideHandoffNotice()")]
    before_removal = take[:take.index("store.removeItem(HANDOFF_KEY);")]
    assert "var envelope = parseEnvelope(raw);" in before_removal
    assert before_removal.count("return ") == 3 and before_removal.count("return null;") == 3
    assert "if (envelope !== null && now - envelope.createdAt >= HANDOFF_MAX_AGE_MS) return null;" in before_removal
    assert "store.setItem" not in before_removal and "recordNonce" not in before_removal
    assert take.index("recordNonce(store, entries") < take.index("notice: null")
    assert take.count("notice: null") == 1 and "send(" not in take
    assert "if (handoff.notice === null) {\n      send();\n    }" in js
    # The homepage's recovery takes the draft from page memory, never from storage...
    recover = composer[composer.index("const recover = useCallback("):composer.index("useEffect(() => {")]
    assert "const mine = pending.current;" in recover and "readEnvelope(" not in recover
    assert "getItem" not in recover and "takeBack(mine, true);" in recover
    # ...and stops the navigation still on its way, so the page and the draft stay.
    assert recover.count("window.stop();") == 1 and recover.index("window.stop();") < recover.index("takeBack(")
    assert composer.index("pending.current = { draft: text, nonce: envelope.nonce, createdAt: envelope.createdAt };") \
        < composer.index("flushSync(")
    # Leaving the page stops timers only: the handoff stays this page's until a showing decides.
    assert "const onHide = () => stopTimers();" in composer
    # A draft is given back as unsent only after a confirmed withdrawal; anything else is held.
    take_back = composer[composer.index("const takeBack = useCallback("):composer.index("const reclaim = useCallback(")]
    assert "if (withdrawal === 'withdrawn') {" in take_back and "hold({ ...handoff, mayBeGone:" in take_back
    # The chat's ledger decides first: a nonce in it is the chat's, before the envelope is even read.
    owner = app[app.index("function ownerOf("):app.index("const DEFAULT_PLACEHOLDER")]
    assert owner.index("const taken = takenNonces();") < owner.index("if (taken.has(handoff.nonce)) return 'chat';") \
        < owner.index("readEnvelope()") < owner.index("return seen.by === DOCUMENT_ID ? 'withdrawn' : 'elsewhere';") \
        < owner.index("if (mayHaveBeenPruned(taken, handoff)) return 'too-old';") < owner.index("return 'untaken';")
    reclaim = composer[composer.index("const reclaim = useCallback("):composer.index("const recover = useCallback(")]
    assert reclaim.index("const owner = ownerOf(mine);") < reclaim.index("takeBack(mine, false);")
    chat = reclaim[reclaim.index("if (owner === 'chat') {"):reclaim.index("} else if (owner === 'withdrawn')")]
    assert "giveBack('', null);" in chat and "withdraw" not in chat.lower().replace("withdrawn", "")
    # No valid envelope is ever discarded, whatever its age: only a marker or an invalid value.
    assert reclaim.count("discardEnvelope();") == 2
    assert "if (seen.kind === 'withdrawn' || seen.kind === 'other') discardEnvelope();" in reclaim
    assert "HANDOFF_MAX_AGE_MS" not in reclaim and "createdAt <" not in reclaim
    # Nothing held is released by time: only a later showing that withdraws it or can tell who has it.
    hold = composer[composer.index("const hold = useCallback("):composer.index("const takeBack = useCallback(")]
    assert "setTimeout" not in hold and "Date.now()" not in hold and "giveBack(" not in hold
    assert composer.count("window.setTimeout(") == 1 and "stallTimer.current = window.setTimeout(recover" in composer
    # The chat takes a message only in a way the homepage can see; otherwise it leaves it there.
    assert 'if (!recorded && removal === "kept") return left;' in js
    # Without a readable ledger an earlier send cannot be ruled out, so nothing is said of the message's past.
    assert 'if (removal === "kept") return { draft: null, notice: COPY_HANDOFF_LEFT_UNVERIFIED };' in take
    # A withdrawn marker names the homepage document that wrote it, and only that document gets the draft back.
    assert "const DOCUMENT_ID = newNonce();" in app
    assert "JSON.stringify({ v: HANDOFF_VERSION, withdrawn: nonce, by: DOCUMENT_ID })" in app
    assert "Object.keys(marker).sort().join() === 'by,v,withdrawn'" in app
    assert "return seen.by === DOCUMENT_ID ? 'withdrawn' : 'elsewhere';" in owner
    assert app.count("DOCUMENT_ID") == 3          # defined once; written into markers; compared on reading
    # ...or, when it cannot tell whether its removal took effect, puts the envelope back and keeps a copy
    # with the send authority withheld.
    assert take.count("return unverifiedHandoff(store, raw, envelope);") == 2
    assert take.index('if (removal === "unchecked") return unverifiedHandoff(') < take.index("var recorded")
    assert 'if (!recorded && removal === "unchecked") return unverifiedHandoff(store, raw, envelope);' in take
    unverified = js[js.index("function unverifiedHandoff("):js.index("function takeHandoff()")]
    assert unverified.index("store.setItem(HANDOFF_KEY, raw);") < unverified.index('authority: "withheld"')
    # One authority, kept on the form, decides every send; nothing in the page grants it back.
    assert 'return form.getAttribute("data-authority") !== "withheld";' in js
    assert js.count('"data-authority"') == 2 and 'form.setAttribute("data-authority", "withheld");' in js
    assert 'removeAttribute("data-authority"' not in js and "pageshow" not in js
    for function, guard in (("function send() {", "if (!hasAuthority()) return;"),
                            ("function requestReply(exchange) {", "if (!hasAuthority()) return Promise.resolve();"),
                            ("function retryExchange(exchange) {", "if (!hasAuthority()) return;"),
                            ("function retryTurn(index) {", "if (!hasAuthority()) return;"),
                            ("function endAndErase() {", "if (!hasAuthority()) return;")):
        body = js[js.index(function) + len(function):]
        assert body.lstrip().startswith(guard), function
    # Every request is built in requestReply, and only there.
    assert js.count('fetch("/api/chat"') == 1
    reply = js[js.index("function requestReply(exchange) {"):js.index("function send() {")]
    assert 'fetch("/api/chat"' in reply



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


HERO_H1 = "Think clearly. Write well. Build and debug. Search the current web."
HERO_COPY = ("Work through difficult questions, draft and revise writing, explain and repair code, or "
             "research current information with sources—without creating an account or building a "
             "permanent chat history.")


def test_homepage_copy_and_navigation():
    app = APP_TSX.read_text(encoding="utf-8")
    home = app[app.index("function HomePage()"):app.index("type DocSectionProps")]
    text = re.sub(r"\{' '\}", " ", home)
    text = re.sub(r"<[^>]+>", "", text)

    # Exactly one hero h1, with the exact wording; PRIVATE BY DESIGN is an eyebrow.
    assert home.count("<h1") == 1 and HERO_H1 in text
    assert '<span className="eyebrow">PRIVATE BY DESIGN</span>' in home
    assert not re.search(r"<h[1-6][^>]*>\s*PRIVATE BY DESIGN", app)
    assert HERO_COPY in text
    assert ">Start a private session <ArrowRight" in home
    assert ('<span className="trust-item"><Clock size={16} aria-hidden="true"/> Temporary sessions</span>' in home
            and '<span className="trust-item"><CircleSlash size={16} aria-hidden="true"/> No account required</span>' in home
            and "Clear provider disclosure</Link>" in home)
    # Restrained navigation: no second filled button in the header.
    header = app[app.index("function SiteHeader()"):app.index("function SiteFooter()")]
    # Product and How it works are native homepage section anchors; Privacy
    # opens its full page through an in-app link.
    for item in ('<a href="/#product" className="nav-link" data-testid="link-product-nav">Product</a>',
                 '<Link href="/privacy" className="nav-link" data-testid="link-privacy-nav">Privacy</Link>',
                 '<a href="/#how-it-works" className="nav-link" data-testid="link-how-nav">How it works</a>',
                 '<a href="/#product" onClick={close} data-testid="link-mobile-product">Product</a>',
                 '<Link href="/privacy" onClick={close} data-testid="link-mobile-privacy">Privacy</Link>',
                 '<a href="/#how-it-works" onClick={close} data-testid="link-mobile-how">How it works</a>'):
        assert item in header, item
    assert header.count('href="/#product"') == 2 and 'href="/#privacy"' not in header
    assert "Try Kalillac" not in app and "nav-cta" not in app
    assert app.count('className="primary-link') == 1
    for unfinished in ("Pricing", "Sign in", "Sign up", "Create account", "Log in"):
        assert unfinished not in app, unfinished


def test_privacy_wording_boundaries():
    app = APP_TSX.read_text(encoding="utf-8")
    text = re.sub(r"<[^>]+>", " ", app)
    lower = text.lower()

    for claim in ("completely private", "nothing is stored", "nobody else",
                  "refresh deletes", "refreshing deletes", "refresh erases",
                  # Carried-over claims this repository cannot prove: any
                  # statement about the production debug switch. ("Build and
                  # debug" is the product's coding capability, not that switch.)
                  "debug mode", "debug switch", "debug logging", "debug log", "debug_mode",
                  "debugging is", "debugging disabled", "debugging off", "debugging turned",
                  "zero data retention", "zero-retention", "retention marketing",
                  "google", "legal approval", "legally approved", "lawyer"):
        assert claim not in lower, claim
    # "anonymous" only in the explicit disclaimer and in the name of the
    # anonymous daily allowance and its traffic -- never as a claim that the
    # service or its users are anonymous.
    scrubbed = lower
    for phrase in ("anonymous daily allowance", "anonymous chat response", "anonymous traffic"):
        scrubbed = scrubbed.replace(phrase, "")
    assert scrubbed.count("anonymous") == scrubbed.count("not a promise that the service is anonymous") == 1

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
        "When web search is used, relevant query text may be sent to Brave or Tavily, depending on which provider is used.",
        "Cloudflare provides reverse proxy, CDN, security and DNS services for",
        "Cloudflare can process request content while proxying it, including chat requests",
        "Cloudflare Workers AI and Groq are not part of the current active model-provider path.",
        "Do not submit information you cannot allow OpenAI, Brave, Tavily, Cloudflare, or DigitalOcean to process.",
        "No account or paid subscription is currently offered.",
    ):
        assert required in text, required



def privacy_page_text() -> str:
    app = APP_TSX.read_text(encoding="utf-8")
    page = app[app.index("const privacyToc"):app.index("const termsToc")]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", page))


def test_privacy_discloses_cloudflare_edge_security_and_analytics_precisely():
    text = privacy_page_text()

    for required in (
        # Edge processing: TLS terminates at Cloudflare; content is visible to it while proxied.
        "Traffic to kalillac.ai passes through Cloudflare before reaching Kalillac’s server.",
        "Cloudflare terminates the browser-facing HTTPS connection",
        "forwards the request to Kalillac’s origin over a separate encrypted connection.",
        "This means Cloudflare can process request content while proxying it, including chat requests, along with "
        "ordinary network information such as IP address, request headers, requested path, browser information, "
        "and time.",
        "Cloudflare handles what it receives under its own policies.",
        # Security code and the cf_clearance cookie.
        "Cloudflare may add security code used to detect automated or abusive traffic.",
        "Cloudflare may set an HttpOnly cf_clearance cookie showing that the browser passed a security check.",
        "Kalillac does not use this cookie to store, reconstruct, or identify prompts, responses, or conversation "
        "history.",
        # Browser RUM is disabled; edge statistics are not.
        "Cloudflare Web Analytics and Real User Measurements are disabled for kalillac.ai.",
        "Kalillac does not load Cloudflare’s browser RUM beacon or send prompts, responses, or conversation content "
        "to that beacon.",
        "Cloudflare still produces traffic, operational, performance, and security information from requests "
        "handled at its edge.",
        "are separate from the disabled browser RUM beacon and are handled under Cloudflare’s own policies.",
        # The precise replacement for the old sweeping statement.
        "Kalillac’s own frontend uses your device’s fonts and does not include advertising code, browser analytics "
        "code, remote fonts, or third-party asset dependencies. Cloudflare may separately add the security code "
        "described below at its edge.",
    ):
        assert required in text, required
    # Stated once, not repeated within the Cloudflare section.
    assert text.count("Cloudflare handles what it receives under its own policies") == 1

    # Each new section is reachable from the page's own navigation.
    app = APP_TSX.read_text(encoding="utf-8")
    toc = app[app.index("const privacyToc"):app.index("];", app.index("const privacyToc"))]
    for section_id, label in (("infrastructure", "Network infrastructure: Cloudflare"),
                              ("cloudflare-security", "Cloudflare security checks"),
                              ("analytics", "Analytics and performance measurement")):
        assert f"['{section_id}', '{label}']" in toc, section_id
        assert f'<DocSection id="{section_id}" title="{label}">' in app, section_id
    assert 'href="#cloudflare-security"' in app


def test_privacy_makes_no_global_cloudflare_cookie_or_tracking_claims():
    lower = privacy_page_text().lower()

    for claim in (
        # The old sweeping statement.
        "load no fonts, stylesheets, or scripts from other websites",
        "do not include advertising or analytics scripts",
        # Cloudflare cannot see content / receives no chat requests.
        "cloudflare cannot access", "cloudflare cannot see", "cloudflare cannot read", "cloudflare does not see",
        "cloudflare never sees", "cloudflare does not receive chat", "cloudflare receives no chat",
        "chat requests do not pass through cloudflare", "bypass cloudflare",
        # No cookies of any kind.
        "uses no cookies", "no cookies of any kind", "does not use cookies", "never sets a cookie",
        "sets no cookies", "cookie-free", "cookieless",
        # Nothing tracked / no metadata.
        "nothing is tracked", "no tracking", "not tracked", "never tracked",
        "no request metadata", "no metadata",
        # Cloudflare creates no logs or analytics.
        "creates no logs", "keeps no logs", "no logs", "no analytics", "no statistics",
        "cloudflare does not log", "cloudflare does not produce",
        # Disabling RUM disables security or edge processing.
        "disabling rum disables", "disabled cloudflare security", "security is disabled",
        "security checks are disabled", "cloudflare no longer processes",
    ):
        assert claim not in lower, claim


DAILY_ALLOWANCE_COOKIE_SECTION = (
    "When the anonymous daily allowance is active, Kalillac uses one strictly necessary first-party "
    "cookie, named __Host-kalillac_visitor, to apply the free allowance of 10 successful responses per "
    "browser per UTC day. It contains a random, opaque visitor value. It does not contain your messages, "
    "Kalillac’s responses, or the session identifier.",
    "The chat service sets this cookie with HttpOnly, Secure, SameSite=Lax, Path=/ and no Domain "
    "attribute. It is sent only to the Kalillac host that set it and cannot be read by page scripts. Each "
    "anonymous chat response refreshes its 48-hour lifetime.",
    "Kalillac does not store the raw visitor value in its PostgreSQL database. For each UTC day, the server derives a "
    "SHA-256 digest from the visitor value and date. The stored digest changes each UTC day and does not "
    "itself provide a stable identifier across days. The database also stores the reservation state and "
    "timestamps needed to enforce or refund the allowance.",
    "The service is designed to retain the current and previous UTC day’s records and lazily prune older "
    "records during later anonymous traffic. Because pruning is lazy and best-effort, older records may "
    "remain longer when there is no anonymous traffic or cleanup cannot complete.",
    "The allowance does not use an IP address, browser fingerprinting or account identity. Kalillac does "
    "not use this cookie for advertising, analytics or cross-site tracking.",
    "Clearing or refusing this cookie causes the browser to receive a new visitor value and can reset its "
    "per-browser allowance.",
)


def test_privacy_discloses_the_daily_allowance_cookie_accurately():
    text = privacy_page_text()
    app = APP_TSX.read_text(encoding="utf-8")
    lower = text.lower()

    for paragraph in DAILY_ALLOWANCE_COOKIE_SECTION:
        assert paragraph in text, paragraph
    for required in (
        # Overview, Browser storage and What you can do -- all conditional.
        "A daily allowance cookie, when the anonymous daily allowance is active. A random visitor value used "
        "only to apply the free daily limit, as described in Daily allowance cookie.",
        "When the anonymous daily allowance is active, Kalillac’s chat service also sets the daily allowance "
        "cookie described below. That cookie holds a random visitor value and none of your conversation.",
        "When the anonymous daily allowance is active, you can clear or block its cookie in your browser. "
        "Doing so can reset your browser’s daily allowance.",
    ):
        assert required in text, required
    # The existing statement about the chat interface's own storage is kept.
    assert ("It does not write them to localStorage, sessionStorage, IndexedDB, cookies, or the page "
            "address.") in text

    # Retention rows for the cookie and the database records.
    assert ("<tr><td>Daily allowance cookie, when the anonymous daily allowance is active</td>"
            "<td>Your browser</td><td>48 hours after the last anonymous chat response that refreshed it, "
            "or when you clear it</td></tr>") in app
    assert ("<tr><td>Daily allowance records: a per-day digest, reservation state and timestamps</td>"
            "<td>Kalillac’s application database</td><td>Designed to keep the current and previous UTC "
            "day. Older records are pruned lazily during later anonymous traffic and may remain longer"
            "</td></tr>") in app

    # Both documents were materially reviewed for the domain migration.
    toc = app[app.index("const privacyToc"):app.index("];", app.index("const privacyToc"))]
    assert "['browser', 'Browser storage'], ['visitor-cookie', 'Daily allowance cookie']," in toc
    assert app.count('<DocSection id="visitor-cookie" title="Daily allowance cookie">') == 1
    assert app.count('href="#visitor-cookie"') == 2
    # Privacy was materially updated again for the homepage composer's handoff.
    assert 'title="Privacy" toc={privacyToc} updated="Last updated: November 6, 2026"' in app
    assert 'title="Terms of Use" toc={termsToc} updated="Last updated: October 27, 2026"' in app

    # Every mention of the cookie is conditional on the allowance being active.
    assert text.count("__Host-kalillac_visitor") == 1
    assert text.count("When the anonymous daily allowance is active") == 3
    assert lower.count("when the anonymous daily allowance is active") == 5
    # No overstated guarantee about linking or deletion.
    for claim in ("cannot be linked", "can never be linked", "never be linked", "cannot be matched",
                  "impossible to link", "unlinkable", "deleted immediately", "immediately deleted",
                  "pruned immediately", "immediately pruned", "always deleted", "guaranteed"):
        assert claim not in lower, claim


HANDOFF_DISCLOSURE = (
    "The homepage has its own message box and does not load the chat application. Typing there sends nothing. "
    "When you press Send, the homepage places that one message in an envelope in this browser tab’s "
    "sessionStorage and opens the chat application at /app/, which normally removes the envelope when it reads it "
    "and then sends the message as the first message of a new temporary session, under the same temporary session "
    "behavior described above. A message the chat application does not pick up within a few seconds is not sent "
    "automatically. In rare cases, when this browser’s storage fails or it cannot be confirmed which page has the "
    "message, the envelope may remain in that tab’s sessionStorage for the life of the tab, so that the message is "
    "neither lost nor sent twice. It is not saved to an account or sent anywhere because of this. So that the "
    "same message is not sent twice, the chat application also keeps a bounded list of "
    "recently used random identifiers in that tab’s sessionStorage. The list contains no prompt or message content "
    "and may remain in the tab after the message is sent. Your messages and Kalillac’s responses are not passed "
    "through the page address, cookies, localStorage, or messages between browser windows.")


def test_privacy_describes_the_homepage_handoff_accurately():
    text = privacy_page_text()

    assert HANDOFF_DISCLOSURE in text
    # The superseded wording overstated what the old marker and timing guaranteed.
    assert "within a minute" not in text and "single-use marker" not in text
    # ...and called the envelope short-lived, though it is kept in rare storage or ownership failures.
    assert "short-lived envelope" not in text
    # The chat interface's own storage statement is unchanged and still true.
    assert ("The chat interface keeps the session identifier and the completed turns of the open conversation in "
            "page memory only. It does not write them to localStorage, sessionStorage, IndexedDB, cookies, or the "
            "page address.") in text
    # The embedded chat no longer exists, so nothing may describe it.
    assert "embeds the same chat" not in text and "embedded chat application" not in text
    assert "full-screen" not in text and "exchange only a signal" not in text

# --- no automatic third-party resources ---------------------------------------------------------


EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//", re.I)


def test_site_sources_load_nothing_from_another_origin():
    page = (SITE / "index.html").read_text(encoding="utf-8")
    css = SITE_CSS.read_text(encoding="utf-8")

    for tag in re.findall(r"<(?:link|script)\b[^>]*>", page):
        if 'rel="canonical"' in tag:
            assert tag == '<link rel="canonical" href="https://kalillac.ai/" />'
            continue
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
        if 'rel="canonical"' in tag:
            assert tag == '<link rel="canonical" href="https://kalillac.ai/" />'
            continue
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
    # Loading a page never loads the chat; only the homepage's Send opens it.
    assert not any(url.startswith(origin + "/app/") or url.startswith(origin + "/api/") for url in requests)
    for url in requests:
        assert url.startswith(origin + "/") or url.startswith("data:"), url


def test_backend_input_limit_is_unchanged():
    source = BACKEND.read_text(encoding="utf-8")
    assert re.search(r"^MAX_INPUT_CHARS = 4000$", source, re.M)
    # The chat client does not truncate or cap input itself; the backend's
    # message_too_long contract (test_frontend_error_contract) enforces it.
    assert "maxlength" not in INDEX_HTML.read_text(encoding="utf-8").lower()
    # The homepage applies the same limit before handing a draft over, in
    # code points of the trimmed text, and never truncates.
    app = APP_TSX.read_text(encoding="utf-8")
    assert "const MESSAGE_MAX_CHARS = 4000;" in app and "maxLength" not in app
    assert "function codePoints(text: string): number { return Array.from(text).length; }" in app


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
    "/site.webmanifest",
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


# --- the homepage-to-chat handoff (browser, trusted input) ------------------------------------------


class Tab:
    """One browser tab, driven over the DevTools protocol with trusted input."""

    def __init__(self, devtools, session, origin: str):
        self.dt, self.session, self.origin = devtools, session, origin

    def eval(self, body: str):
        result = self.dt.call("Runtime.evaluate", {"expression": f"(async () => {{ {body} }})()",
                                                   "awaitPromise": True, "returnByValue": True}, self.session)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")

    def wait(self, condition: str, timeout: float = 20) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.eval(f"return !!({condition});"):
                    return True
            except Exception:
                pass                       # the document is being replaced
            time.sleep(0.05)
        return False

    def open(self, path: str, ready: str = "true"):
        self.dt.call("Page.navigate", {"url": self.origin + path}, self.session)
        assert self.wait(f"location.pathname === {json.dumps(path)} && document.readyState === 'complete' && ({ready})")
        time.sleep(0.2)

    def center(self, selector: str) -> dict:
        # Instant, not the page's smooth scrolling, so the point is measured where it will stay.
        self.eval(f"document.querySelector({json.dumps(selector)}).scrollIntoView({{block: 'center', behavior: 'instant'}}); "
                  "await new Promise(r => setTimeout(r, 80)); return true;")
        return self.eval(f"const b = document.querySelector({json.dumps(selector)}).getBoundingClientRect(); "
                         "return {x: b.left + b.width / 2, y: b.top + b.height / 2};")

    def click(self, selector: str, count: int = 1):
        box = self.center(selector)
        for n in range(1, count + 1):
            for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                self.dt.call("Input.dispatchMouseEvent", {
                    "type": kind, "x": box["x"], "y": box["y"], "button": "left" if kind != "mouseMoved" else "none",
                    "buttons": 1 if kind == "mousePressed" else 0, "clickCount": n}, self.session)

    def tap(self, selector: str):
        box = self.center(selector)
        self.dt.call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [box]}, self.session)
        self.dt.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []}, self.session)

    def type(self, text: str):
        self.dt.call("Input.insertText", {"text": text}, self.session)

    def enter(self, shift: bool = False):
        down = {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r",
                "modifiers": 8 if shift else 0}
        self.dt.call("Input.dispatchKeyEvent", down, self.session)
        self.dt.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter",
                                                "windowsVirtualKeyCode": 13, "modifiers": 8 if shift else 0},
                     self.session)

    def history(self, step: int, path: str):
        self.eval(f"history.go({step}); return true;")
        assert self.wait(f"location.pathname === {json.dumps(path)} && document.readyState === 'complete'")
        time.sleep(0.4)

    def home(self) -> dict:
        return self.eval("""const i = document.getElementById('home-composer-input');
          const t = s => (document.querySelector(s) || {}).textContent || '';
          return { path: location.pathname, search: location.search, hash: location.hash,
                   value: i.value, placeholder: i.placeholder, readOnly: i.readOnly,
                   sendDisabled: document.querySelector('[data-testid="button-home-send"]').disabled,
                   status: t('[data-testid="home-composer-status"]'), alert: t('[data-testid="home-composer-alert"]'),
                   session: Object.entries(sessionStorage), localLength: localStorage.length, cookie: document.cookie,
                   restored: (window.__shows || []).includes(true), historyLength: history.length,
                   doc: window.__doc || null };""")

    def chat(self) -> dict:
        return self.eval("""const c = window.__HANDOFF_CHILD__ || {requests: [], events: []};
          const empty = document.getElementById('empty');
          return { path: location.pathname, search: location.search, hash: location.hash, href: location.href,
                   document: c.loadedAt || null,
                   users: [...document.querySelectorAll('#thread .msg-user .bubble')].map(b => b.textContent),
                   userMarkup: [...document.querySelectorAll('#thread .msg-user .bubble')].map(b => b.children.length),
                   assistants: document.querySelectorAll('#thread .msg-assistant').length,
                   requests: c.requests.map(r => r.body), events: c.events,
                   value: document.getElementById('composer-input').value,
                   readOnly: document.getElementById('composer-input').readOnly,
                   sendDisabled: document.getElementById('send-btn').disabled,
                   eraseDisabled: document.getElementById('erase-btn').disabled,
                   authority: document.getElementById('composer').getAttribute('data-authority'),
                   hintsDisabled: [...document.querySelectorAll('.hint')].map(h => h.disabled),
                   appScripts: document.querySelectorAll('script[src^="app.js"]').length,
                   notice: (n => n && !n.hidden ? document.getElementById('handoff-notice-text').textContent : '')(
                     document.getElementById('handoff-notice')),
                   emptyShown: !!(empty && empty.isConnected),
                   session: Object.entries(sessionStorage), localLength: localStorage.length,
                   cookie: document.cookie, historyLength: history.length };""")

    def on_new_documents(self, source: str) -> str:
        """Runs `source` in every document this tab opens from now on, before its own scripts."""
        return self.dt.call("Page.addScriptToEvaluateOnNewDocument", {"source": source}, self.session)["identifier"]

    def forget(self, identifier: str):
        self.dt.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier}, self.session)

    def network(self) -> list[dict]:
        self.eval("return 1;")
        return [{"url": e["params"]["request"]["url"],
                 "referer": e["params"]["request"].get("headers", {}).get("Referer", "")}
                for e in self.dt.events if e.get("method") == "Network.requestWillBeSent"]


HOME_READY = "document.getElementById('home-composer-input')"
CHAT_READY = "document.getElementById('composer-input') && window.__HANDOFF_CHILD__"
REPLIED = ("document.querySelectorAll('#thread .msg-assistant').length >= 1 && "
           "!document.getElementById('send-btn').classList.contains('is-stop')")
NONCE = "0123456789abcdef0123456789abcdef"


def envelope(**fields) -> str:
    """A JavaScript expression for one handoff envelope; fields override a valid one."""
    base = {"v": "1", "draft": "'Hello from an envelope'", "nonce": f"'{NONCE}'", "createdAt": "Date.now()"}
    base.update(fields)
    return "JSON.stringify({" + ", ".join(f"{k}: {v}" for k, v in base.items() if v is not None) + "})"


ENVELOPES = {
    # name: (JavaScript expression for the stored value, whether /app/ may send it)
    "valid": (envelope(), True),
    "valid-4000-code-points": (envelope(draft="'\U0001F600'.repeat(4000)"), True),
    "malformed-json": ("'{not json'", False),
    "not-an-object": ("JSON.stringify([1, 2])", False),
    "wrong-version": (envelope(v="2"), False),
    "extra-field": ("JSON.stringify({v: 1, draft: 'Hello', nonce: '" + NONCE + "', createdAt: Date.now(), url: '/x'})",
                    False),
    "missing-nonce": (envelope(nonce=None), False),
    "malformed-nonce": (envelope(nonce="'XYZ'"), False),
    "stale": (envelope(createdAt="Date.now() - 61000"), False),
    "fractional-time": (envelope(createdAt="Date.now() + 0.5"), False),
    "from-the-future": (envelope(createdAt="Date.now() + 60000"), False),
    "oversized": (envelope(draft="'x'.repeat(4001)"), False),
    "oversized-code-points": (envelope(draft="'\U0001F600'.repeat(4001)"), False),
    "whitespace-only": (envelope(draft="' \\n\\t '"), False),
    "non-string-draft": (envelope(draft="42"), False),
    "replayed": (envelope(), False),
}


# Presses the homepage's Send and returns the page's state 300 ms later. If the
# draft is later given back, the page reports its state once to the loopback
# server -- the DevTools protocol cannot read a page whose navigation is
# pending. A test's fault may run before this.
SEND_OBSERVED = """const st = document.querySelector('[data-testid="home-composer-status"]'),
        al = document.querySelector('[data-testid="home-composer-alert"]');
  const state = () => { const i = document.getElementById('home-composer-input');
    return { value: i.value, readOnly: i.readOnly, status: st.textContent, alert: al.textContent,
             session: Object.keys(sessionStorage),
             sendDisabled: document.querySelector('[data-testid="button-home-send"]').disabled }; };
  let reported = false;
  const report = () => { if (reported || !(st.textContent + al.textContent).includes('didn’t open')) return;
    reported = true;
    fetch('/__handoff__/report', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                  body: JSON.stringify(state())}); };
  for (const region of [st, al]) {
    new MutationObserver(report).observe(region, {childList: true, characterData: true, subtree: true});
  }
  window.__shows = []; addEventListener('pageshow', e => window.__shows.push(e.persisted));
  document.querySelector('[data-testid="button-home-send"]').click();
  await new Promise(resolve => setTimeout(resolve, 300));
  return state();"""


def storage_fault(method: str, key: str, after: int = 0, ignore: bool = False) -> str:
    """JavaScript: from call `after + 1` on, Storage.prototype[method] for `key`
    throws a SecurityError (or, with ignore, silently does nothing)."""
    effect = "return undefined;" if ignore else "throw new DOMException('denied', 'SecurityError');"
    return (f"(function () {{ const real = Storage.prototype.{method}; let n = 0; "
            f"Storage.prototype.{method} = function (key) {{ if (key === {json.dumps(key)} && ++n > {after}) "
            f"{{ {effect} }} return real.apply(this, arguments); }}; }})();")


def withdrawn_marker(stored: str | None, handoff_nonce: str) -> str | None:
    """The identity of the homepage document that wrote this handoff's withdrawn marker -- the marker holds only
    the version, the handoff's nonce and that random identity, never a message -- or None if it is not one."""
    if stored is None:
        return None
    marker = json.loads(stored)
    if (isinstance(marker, dict) and set(marker) == {"v", "withdrawn", "by"} and marker["v"] == 1
            and marker["withdrawn"] == handoff_nonce and re.fullmatch(r"[0-9a-f]{32}", str(marker["by"]))):
        return marker["by"]
    return None


def ledger_entries(raw: str) -> list[dict]:
    """The ledger's entries, checked to hold nothing but random nonces and their expiry times."""
    ledger = json.loads(raw)
    assert set(ledger) == {"v", "entries"} and ledger["v"] == 1, ledger
    for entry in ledger["entries"]:
        assert set(entry) == {"n", "x"} and re.fullmatch(r"[0-9a-f]{32}", entry["n"]), entry
        assert type(entry["x"]) is int and entry["x"] > 0, entry
    assert len({entry["n"] for entry in ledger["entries"]}) == len(ledger["entries"]) <= 32
    return ledger["entries"]


def run_handoff(tab: Tab, site: Site) -> dict:
    r: dict = {}
    first = "  Explain how encryption works."
    second = "Keep it brief \u2014 \u2713 \u65e5\u672c\u8a9e \U0001F642  "
    expected = (first + "\n" + second).strip()

    # ---- compose on "/", press Send, converse on "/app/" ------------------------------------------
    # This homepage may enter the back/forward cache, so Back restores the very
    # page that sent the draft (pageshow.persisted); the second cycle below
    # covers the reload path.
    site.http.cacheable = {"/"}
    tab.open("/", HOME_READY)
    tab.eval("window.__shows = []; addEventListener('pageshow', e => window.__shows.push(e.persisted)); return true;")
    tab.click("#home-composer-input")
    tab.type(first)
    tab.enter(shift=True)                       # Shift+Enter: a new line, never a send
    tab.type(second)
    time.sleep(0.2)
    r["composed"] = tab.home()
    tab.click('[data-testid="button-home-send"]')
    assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY} && {REPLIED}")
    time.sleep(0.4)
    r["sent"] = tab.chat()
    r["expected"] = expected

    # ---- Back: a clean homepage whose composer stays on "/" -----------------------------------------
    tab.history(-1, "/")
    assert tab.wait(HOME_READY)
    r["back"] = tab.home()
    tab.click("#home-composer-input")
    tab.type("typed after Back")
    time.sleep(0.4)
    r["typedAfterBack"] = tab.home()
    tab.click('[data-testid="button-home-clear"]')
    time.sleep(0.2)
    r["clearedAfterBack"] = tab.home()

    # ---- Forward: the conversation is never sent again ------------------------------------------------
    tab.history(1, "/app/")
    assert tab.wait(CHAT_READY)
    time.sleep(0.6)
    r["forward"] = tab.chat()

    # ---- a second cycle, sent with Enter, from a fresh homepage that may not be
    # cached ("no-store"), so this time Back reloads it ------------------------------------------------
    site.http.cacheable = set()
    tab.open("/", HOME_READY)
    tab.click("#home-composer-input")
    tab.type("Second cycle")
    tab.enter()
    assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY} && {REPLIED}")
    time.sleep(0.4)
    r["cycle2"] = tab.chat()
    # A reloaded homepage is a new document, with no record of an earlier showing.
    tab.history(-1, "/")
    assert tab.wait(HOME_READY)
    r["back2"] = tab.home()
    r["network"] = tab.network()

    # ---- a rapid double send: a trusted double-click, then two clicks and two Enters at once -------
    tab.open("/", HOME_READY)
    tab.click("#home-composer-input")
    tab.type("Only once, please")
    tab.click('[data-testid="button-home-send"]', count=2)
    assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY} && {REPLIED}")
    time.sleep(1.0)
    r["double"] = tab.chat()
    tab.open("/", HOME_READY)
    tab.click("#home-composer-input")
    tab.type("Still only once")
    tab.eval("""const b = document.querySelector('[data-testid="button-home-send"]'),
                      i = document.getElementById('home-composer-input');
      const enter = () => i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
      b.click(); b.click(); enter(); enter(); return true;""")
    assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY} && {REPLIED}")
    time.sleep(1.0)
    r["burst"] = tab.chat()

    # ---- the envelope: only a valid, fresh, unused one is ever sent --------------------------------
    ledger = dict(r["double"]["session"])[LEDGER_KEY]
    used = json.loads(ledger)["entries"][-1]["n"]
    r["envelopes"] = {}
    for name, (expression, _) in ENVELOPES.items():
        tab.open("/privacy")
        tab.eval("sessionStorage.clear(); return true;")
        if name == "replayed":
            # The exact envelope of a message already sent from this tab, offered again.
            tab.eval(f"sessionStorage.setItem({json.dumps(LEDGER_KEY)}, {json.dumps(ledger)}); return true;")
            expression = envelope(nonce=json.dumps(used))
        before = tab.eval(f"sessionStorage.setItem({json.dumps(HANDOFF_KEY)}, {expression}); "
                          f"return sessionStorage.getItem({json.dumps(HANDOFF_KEY)});")
        tab.open("/app/", CHAT_READY)
        time.sleep(0.9)
        r["envelopes"][name] = {**tab.chat(), "envelopeBefore": before}

    # ---- storage that refuses the envelope keeps the draft on "/" ------------------------------------
    r["storageFailure"] = {}
    for name, sabotage, repair in (
        ("setItem-throws", "window.__set = Storage.prototype.setItem; Storage.prototype.setItem = function () "
                           "{ throw new DOMException('full', 'QuotaExceededError'); };",
         "Storage.prototype.setItem = window.__set;"),
        ("storage-unavailable", "window.__ssd = Object.getOwnPropertyDescriptor(window, 'sessionStorage'); "
                                "Object.defineProperty(window, 'sessionStorage', {configurable: true, get() "
                                "{ throw new DOMException('denied', 'SecurityError'); }});",
         "if (window.__ssd) Object.defineProperty(window, 'sessionStorage', window.__ssd); "
         "else delete window.sessionStorage;"),
    ):
        tab.open("/", HOME_READY)
        tab.eval(sabotage + " return true;")
        tab.click("#home-composer-input")
        tab.type("Keep me safe")
        tab.click('[data-testid="button-home-send"]')
        time.sleep(1.2)
        failed = tab.eval("""const i = document.getElementById('home-composer-input');
          return { path: location.pathname, value: i.value, readOnly: i.readOnly,
                   alert: document.querySelector('[data-testid="home-composer-alert"]').textContent,
                   sendDisabled: document.querySelector('[data-testid="button-home-send"]').disabled };""")
        tab.eval(repair + " return true;")
        failed["stored"] = tab.eval("return Object.keys(sessionStorage);")
        tab.click('[data-testid="button-home-send"]')
        assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY} && {REPLIED}")
        failed["retried"] = tab.chat()
        r["storageFailure"][name] = failed

    # ---- an envelope the chat never took comes back to the homepage --------------------------------
    tab.open("/privacy")
    tab.eval("sessionStorage.clear(); return true;")
    tab.eval(f"sessionStorage.setItem({json.dumps(HANDOFF_KEY)}, {envelope(draft=json.dumps('An unsent draft'))}); "
             "return true;")
    tab.open("/", HOME_READY)
    time.sleep(0.4)
    r["returned"] = tab.home()

    # ---- a chat that does not open in time gives the draft back, and is never sent ----------------
    # While a navigation is pending the DevTools protocol cannot read the page,
    # so the homepage reports its own state: once in the same evaluation that
    # presses Send, and once -- through the loopback report endpoint -- when it
    # gives the draft back.
    site.app_delay = 12.0
    try:
        tab.open("/", HOME_READY)
        tab.click("#home-composer-input")
        tab.type("A slow chat")
        site.http.reports.clear()
        started = time.monotonic()
        r["stallPending"] = tab.eval(SEND_OBSERVED)
        deadline = time.monotonic() + 16
        while not site.http.reports and time.monotonic() < deadline:
            time.sleep(0.1)
        r["stalled"] = site.http.reports[0] if site.http.reports else None
        # The navigation was stopped: past the chat's 12 seconds, this page and its draft are still here.
        time.sleep(max(0.0, 13.5 - (time.monotonic() - started)))
        r["stallStayed"] = tab.home()
    finally:
        site.app_delay = 0.0
    # The chat, opened late by hand, has nothing to send.
    tab.open("/app/", CHAT_READY)
    time.sleep(0.8)
    r["stallArrived"] = tab.chat()
    r["serverPaths"] = list(site.http.paths)
    return r


@pytest.fixture(scope="module")
def handoff(tmp_path_factory):
    browser = require_browser_and_build()
    profile = tmp_path_factory.mktemp("edge-handoff")
    site = Site()
    site.child_passive = True

    with site.http:
        with browser_page(browser, profile, VIEWPORTS["desktop"], mobile=False) as (devtools, session):
            result = run_handoff(Tab(devtools, session, site.http.origin), site)

    shutil.rmtree(profile, ignore_errors=True)
    return result


@pytest.fixture(scope="module")
def touch_handoff(tmp_path_factory):
    """A touch phone: Enter is a new line in the message box; the Send button sends."""
    browser = require_browser_and_build()
    profile = tmp_path_factory.mktemp("edge-handoff-touch")
    site = Site()
    site.child_passive = True
    r: dict = {}

    with site.http:
        with browser_page(browser, profile, VIEWPORTS["mobile"], mobile=True) as (devtools, session):
            tab = Tab(devtools, session, site.http.origin)
            tab.open("/", HOME_READY)
            tab.tap("#home-composer-input")
            tab.type("line one")
            tab.enter()
            tab.type("line two")
            time.sleep(0.3)
            r["composed"] = tab.home()
            tab.tap('[data-testid="button-home-send"]')
            assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY} && {REPLIED}")
            r["sent"] = tab.chat()

    shutil.rmtree(profile, ignore_errors=True)
    return r


def test_send_opens_the_chat_and_submits_the_draft_exactly_once(handoff):
    composed, sent = handoff["composed"], handoff["sent"]
    # Typing, a new line and Unicode stayed on the homepage.
    assert composed["path"] == "/" and composed["value"] == "  Explain how encryption works.\n" + \
        "Keep it brief \u2014 \u2713 \u65e5\u672c\u8a9e \U0001F642  "
    assert composed["session"] == [] and composed["sendDisabled"] is False
    # One request, carrying the trimmed draft with its line break and Unicode intact.
    assert sent["path"] == "/app/" and sent["search"] == "" and sent["hash"] == ""
    assert sent["requests"] == [{"message": handoff["expected"], "history": [], "session_id": None}]
    assert sent["users"] == [handoff["expected"]] and sent["assistants"] == 1
    # Shown as text, never markup; the composer is empty again.
    assert sent["userMarkup"] == [0] and sent["value"] == "" and sent["emptyShown"] is False


def test_the_envelope_is_removed_before_the_request(handoff):
    events = handoff["sent"]["events"]
    ops = [(e["op"], e.get("key", e.get("url"))) for e in events]
    removed = ops.index(("Storage.removeItem", HANDOFF_KEY))
    recorded = ops.index(("Storage.setItem", LEDGER_KEY))
    request = ops.index(("fetch", "/api/chat"))
    assert removed < recorded < request, ops
    assert ops.count(("fetch", "/api/chat")) == 1
    assert not any(e.get("local") for e in events if e["op"].startswith("Storage."))
    session = dict(handoff["sent"]["session"])
    assert set(session) == {LEDGER_KEY} and len(ledger_entries(session[LEDGER_KEY])) == 1


def test_the_draft_never_reaches_the_address_cookies_localstorage_or_referer(handoff):
    fragments = ("Explain how encryption", "encryption", "Second cycle", "Keep it brief")
    for request in handoff["network"]:
        for fragment in fragments:
            assert fragment not in request["url"] and fragment.replace(" ", "%20") not in request["url"], request
            assert fragment not in request["referer"], request
    for state in (handoff["sent"], handoff["cycle2"], handoff["double"]):
        assert state["search"] == "" and state["hash"] == "" and state["href"].endswith("/app/")
        assert state["localLength"] == 0 and state["cookie"] == ""
    # The ledger never holds a message.
    for state in (handoff["sent"], handoff["cycle2"]):
        assert all("encryption" not in value and "cycle" not in value for _, value in state["session"])


def test_back_returns_a_clean_homepage_that_never_reopens_the_chat(handoff):
    back = handoff["back"]
    # Back restored the very page that sent the draft from the back/forward cache.
    assert back["restored"] is True
    assert back["path"] == "/" and back["search"] == "" and back["hash"] == ""
    # The sent draft never comes back as unsent; the composer is ready, not busy.
    assert back["value"] == "" and back["readOnly"] is False and back["status"] == "" and back["alert"] == ""
    # Only the ledger remains in this tab's sessionStorage -- never a message.
    assert back["sendDisabled"] is True and set(dict(back["session"])) == {LEDGER_KEY}
    typed = handoff["typedAfterBack"]
    assert typed["path"] == "/" and typed["value"] == "typed after Back" and typed["sendDisabled"] is False
    assert handoff["clearedAfterBack"]["value"] == "" and handoff["clearedAfterBack"]["path"] == "/"


def test_repeated_history_cycles_stay_consistent(handoff):
    forward = handoff["forward"]
    # Forward to the chat sends nothing again: either the cached conversation
    # (its one request) or a fresh, empty chat (none).
    if forward["users"]:
        assert forward["users"] == [handoff["expected"]] and len(forward["requests"]) == 1
    else:
        assert forward["requests"] == [] and forward["emptyShown"] is True
    cycle = handoff["cycle2"]
    assert cycle["requests"] == [{"message": "Second cycle", "history": [], "session_id": None}]
    assert cycle["users"] == ["Second cycle"]
    back = handoff["back2"]
    # A Back that reloads the homepage (not cacheable) is just as clean.
    assert back["restored"] is False
    assert back["path"] == "/" and back["value"] == "" and back["readOnly"] is False and back["sendDisabled"] is True


def test_rapid_double_send_submits_once(handoff):
    double = handoff["double"]
    assert double["requests"] == [{"message": "Only once, please", "history": [], "session_id": None}]
    assert double["users"] == ["Only once, please"]
    burst = handoff["burst"]
    assert burst["requests"] == [{"message": "Still only once", "history": [], "session_id": None}]
    assert burst["users"] == ["Still only once"]


@pytest.mark.parametrize("name", list(ENVELOPES))
def test_only_a_valid_fresh_unused_envelope_is_ever_sent(handoff, name):
    state = handoff["envelopes"][name]
    _, accepted = ENVELOPES[name]
    if name == "stale":
        # Well-formed but too old for /app/ ever to take: left exactly as it was, for the homepage.
        assert dict(state["session"]).get(HANDOFF_KEY) == state["envelopeBefore"], state["session"]
    else:
        # Whatever else it held, the envelope is gone once /app/ has looked at it.
        assert HANDOFF_KEY not in dict(state["session"]), state["session"]
    if accepted:
        assert len(state["requests"]) == 1 and len(state["users"]) == 1
        assert len(list(state["requests"][0]["message"])) == (4000 if "4000" in name else 22)
    else:
        assert state["requests"] == [] and state["users"] == [], name
        assert state["value"] == "" and state["emptyShown"] is True and state["notice"] == "", name


def test_storage_that_refuses_the_envelope_keeps_the_draft_on_the_homepage(handoff):
    for name, failed in handoff["storageFailure"].items():
        assert failed["path"] == "/" and failed["value"] == "Keep me safe", name
        assert failed["readOnly"] is False and failed["sendDisabled"] is False, name
        assert "wasn’t sent" in failed["alert"], name
        assert HANDOFF_KEY not in failed["stored"], name
        # Once storage works again, the same draft is sent, once.
        assert failed["retried"]["requests"] == [{"message": "Keep me safe", "history": [], "session_id": None}]


def test_an_unsent_envelope_returns_to_the_homepage_and_is_never_sent(handoff):
    returned = handoff["returned"]
    assert returned["path"] == "/" and returned["value"] == "An unsent draft"
    assert "wasn’t sent" in returned["status"] and returned["session"] == []


def test_a_chat_that_does_not_open_in_time_gives_the_draft_back(handoff):
    pending, stalled, arrived = handoff["stallPending"], handoff["stalled"], handoff["stallArrived"]
    # While the chat is opening, the composer is busy, the draft has left it and Send cannot repeat.
    assert pending["readOnly"] is True and pending["sendDisabled"] is True and pending["value"] == ""
    assert pending["session"] == [HANDOFF_KEY] and pending["status"] == "Opening the chat…"
    # Ten seconds later the draft comes back, the envelope is gone and the box is usable again.
    assert stalled is not None, "the homepage never gave the draft back"
    assert stalled["value"] == "A slow chat" and stalled["readOnly"] is False and stalled["session"] == []
    assert stalled["sendDisabled"] is False and stalled["status"] == COPY_STALLED and stalled["alert"] == ""
    # The navigation was stopped, so the page and the draft stay after /app/ would have answered.
    stayed = handoff["stallStayed"]
    assert stayed["path"] == "/" and stayed["value"] == "A slow chat" and stayed["status"] == COPY_STALLED
    assert stayed["readOnly"] is False and stayed["session"] == []
    # A chat opened afterwards has nothing to send.
    assert arrived["requests"] == [] and arrived["users"] == [] and arrived["emptyShown"] is True
    assert arrived["value"] == "" and arrived["notice"] == ""


def test_the_homepage_itself_never_calls_the_api(handoff):
    assert not any(path.startswith("/api/") or path.startswith("POST /api/") for path in handoff["serverPaths"])


def test_touch_keyboards_insert_a_new_line_and_send_only_with_the_button(touch_handoff):
    composed, sent = touch_handoff["composed"], touch_handoff["sent"]
    assert composed["path"] == "/" and composed["value"] == "line one\nline two"
    assert sent["requests"] == [{"message": "line one\nline two", "history": [], "session_id": None}]


# --- the ledger of taken nonces (browser) ------------------------------------------------------------

# A pinned clock for /app/: every Date.now() there returns FIXED, so an
# envelope's age when /app/ reads it is exact to the millisecond.
FIXED = 1_800_000_000_000
PINNED_CLOCK = f"<script>Date.now = function () {{ return {FIXED}; }};</script>"
LIVE = "Date.now() + 30000"


def nonce(i: int) -> str:
    return f"{i:032x}"


def ledger_js(entries) -> str:
    """JavaScript for a well-formed ledger of (nonce, expiry expression) entries."""
    return "JSON.stringify({v: 1, entries: [" + ", ".join(f"{{n: '{n}', x: {x}}}" for n, x in entries) + "]})"


def stored(tab: Tab, values: dict[str, str], clear: bool = True) -> dict:
    """On /privacy, which reads neither key, set this tab's sessionStorage
    items from JavaScript expressions (after clearing it, unless told not to).
    Returns the exact strings stored and the page's clock."""
    tab.open("/privacy")
    body = "sessionStorage.clear(); " if clear else ""
    body += "const out = {now: Date.now()}; "
    for key, expression in values.items():
        body += f"out[{json.dumps(key)}] = {expression}; sessionStorage.setItem({json.dumps(key)}, out[{json.dumps(key)}]); "
    return tab.eval(body + "return out;")


def arrive(tab: Tab, site: Site, prelude: str = "") -> dict:
    """Open /app/ (with an optional prelude) and return its state once any send has been answered."""
    site.app_prelude = prelude
    try:
        tab.open("/app/", CHAT_READY)
        time.sleep(0.9)
        return tab.chat()
    finally:
        site.app_prelude = ""


MALFORMED_LEDGERS = {
    "not-json": "'{oops'",
    "a-list": "'[]'",
    "null": "'null'",
    "wrong-version": "JSON.stringify({v: 2, entries: []})",
    "no-version": "JSON.stringify({entries: []})",
    "extra-field": "JSON.stringify({v: 1, entries: [], at: 1})",
    "entries-not-a-list": "JSON.stringify({v: 1, entries: {}})",
    "entry-not-an-object": f"JSON.stringify({{v: 1, entries: ['{nonce(1)}']}})",
    "entry-extra-field": f"JSON.stringify({{v: 1, entries: [{{n: '{nonce(1)}', x: {LIVE}, m: 'hello'}}]}})",
    "malformed-nonce": ledger_js([("XYZ", LIVE)]),
    "uppercase-nonce": ledger_js([(nonce(0xABC).upper(), LIVE)]),
    "fractional-expiry": ledger_js([(nonce(1), LIVE + " + 0.5")]),
    "string-expiry": f"JSON.stringify({{v: 1, entries: [{{n: '{nonce(1)}', x: String({LIVE})}}]}})",
    "zero-expiry": ledger_js([(nonce(1), "0")]),
    "duplicate-nonce": ledger_js([(nonce(1), LIVE), (nonce(1), LIVE)]),
    "over-capacity": ledger_js([(nonce(i), LIVE) for i in range(1, 34)]),
}

# Storage faults in /app/ only, each on an otherwise valid, fresh envelope.
LEDGER_FAULTS = {
    "ledger-read-fails": storage_fault("getItem", LEDGER_KEY),
    "ledger-write-fails": storage_fault("setItem", LEDGER_KEY),
    "ledger-write-ignored": storage_fault("setItem", LEDGER_KEY, ignore=True),
    "ledger-verification-read-fails": storage_fault("getItem", LEDGER_KEY, after=1),
    "envelope-removal-fails": storage_fault("removeItem", HANDOFF_KEY),
    "envelope-removal-ignored": storage_fault("removeItem", HANDOFF_KEY, ignore=True),
}

# Chat-side faults that leave the chat no way to show the homepage it took the
# message (both removal and recording fail), and one -- a removal whose
# read-back failed -- that may have taken effect, so the chat keeps the draft.
LEFT_CASES = {
    "removal-fails-and-ledger-write-fails": (
        lambda: {HANDOFF_KEY: envelope(draft=json.dumps("Left check"), nonce=f"'{nonce(0x700)}'")},
        storage_fault("removeItem", HANDOFF_KEY) + storage_fault("setItem", LEDGER_KEY)),
    "removal-ignored-and-ledger-unreadable": (
        lambda: {HANDOFF_KEY: envelope(draft=json.dumps("Left check"), nonce=f"'{nonce(0x701)}'")},
        storage_fault("removeItem", HANDOFF_KEY, ignore=True) + storage_fault("getItem", LEDGER_KEY)),
    "removal-fails-and-ledger-full": (
        lambda: {LEDGER_KEY: ledger_js([(nonce(0x800 + i), "Date.now() + 30000") for i in range(32)]),
                 HANDOFF_KEY: envelope(draft=json.dumps("Left check"), nonce=f"'{nonce(0x702)}'")},
        storage_fault("removeItem", HANDOFF_KEY)),
    "removal-unconfirmed-and-ledger-write-fails": (
        lambda: {HANDOFF_KEY: envelope(draft=json.dumps("Left check"), nonce=f"'{nonce(0x703)}'")},
        storage_fault("getItem", HANDOFF_KEY, after=1) + storage_fault("setItem", LEDGER_KEY)),
    "removal-unconfirmed-and-ledger-unreadable": (
        lambda: {HANDOFF_KEY: envelope(draft=json.dumps("Left check"), nonce=f"'{nonce(0x704)}'")},
        storage_fault("removeItem", HANDOFF_KEY, ignore=True) + storage_fault("getItem", HANDOFF_KEY, after=1)
        + storage_fault("getItem", LEDGER_KEY)),
}

# A message the chat took and sent -- its envelope's removal silently did nothing -- then the chat reloaded
# with the ledger unreadable: the faults of that reload, in /app/ only.
AFTER_SENT = {
    "removal-unconfirmed": storage_fault("removeItem", HANDOFF_KEY, ignore=True)
    + storage_fault("getItem", HANDOFF_KEY, after=1) + storage_fault("getItem", LEDGER_KEY),
    "removal-certainly-failed": storage_fault("removeItem", HANDOFF_KEY, ignore=True)
    + storage_fault("getItem", LEDGER_KEY),
}

# Capacity and pruning, on the pinned clock: an entry with x <= FIXED has expired.
FULL = [(nonce(0x100 + i), str(FIXED + 1)) for i in range(32)]
EXPIRED = [(nonce(0x200 + i), str(x)) for i, x in enumerate((1, FIXED - 65000, FIXED - 1, FIXED))]
LIVE_ENTRIES = [(nonce(0x300 + i), str(FIXED + 1 + i * 1000)) for i in range(28)]
ALMOST_FULL = [(nonce(0x500 + i), str(FIXED + 1)) for i in range(31)] + [(nonce(0x5FF), str(FIXED))]


def run_ledger(tab: Tab, site: Site) -> dict:
    r: dict = {}

    # ---- A, then B (a real homepage Send), then the unchanged original A again -------------------
    a = stored(tab, {HANDOFF_KEY: envelope(draft=json.dumps("Message A"), nonce=f"'{nonce(0xA)}'")})[HANDOFF_KEY]
    r["A"] = arrive(tab, site)
    tab.open("/", HOME_READY)
    tab.click("#home-composer-input")
    tab.type("Message B")
    tab.click('[data-testid="button-home-send"]')
    assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY} && {REPLIED}")
    time.sleep(0.4)
    r["B"] = tab.chat()
    again = stored(tab, {HANDOFF_KEY: json.dumps(a)}, clear=False)
    r["A-age-at-replay"] = again["now"] - json.loads(a)["createdAt"]
    r["A-again"] = arrive(tab, site)

    # ---- the latest one replayed at once, then older ones after further handoffs ----------------
    sent = {}
    for name in ("C", "D", "E"):
        expression = envelope(draft=json.dumps("Message " + name), nonce=f"'{nonce(int(name, 16))}'")
        sent[name] = stored(tab, {HANDOFF_KEY: expression}, clear=False)[HANDOFF_KEY]
        r[name] = arrive(tab, site)
    stored(tab, {HANDOFF_KEY: json.dumps(sent["E"])}, clear=False)
    r["E-again"] = arrive(tab, site)
    for name, value in (("A", a), ("C", sent["C"])):
        replay = stored(tab, {HANDOFF_KEY: json.dumps(value)}, clear=False)
        r[f"{name}-age-after-others"] = replay["now"] - json.loads(value)["createdAt"]
        r[f"{name}-after-others"] = arrive(tab, site)
    r["drafts"] = ["Message A", "Message B", "Message C", "Message D", "Message E"]

    # ---- a ledger that is not exactly the expected shape sends nothing --------------------------
    r["malformed"] = {}
    for name, expression in MALFORMED_LEDGERS.items():
        values = stored(tab, {LEDGER_KEY: expression, HANDOFF_KEY: envelope(draft=json.dumps("Ledger check"))})
        r["malformed"][name] = {**arrive(tab, site), "ledgerBefore": values[LEDGER_KEY]}

    # ---- storage that fails the ledger or the envelope's removal sends nothing ------------------
    r["faults"] = {}
    for name, fault in LEDGER_FAULTS.items():
        stored(tab, {HANDOFF_KEY: envelope(draft=json.dumps("Fault check"))})
        r["faults"][name] = arrive(tab, site, f"<script>{fault}</script>")

    # ---- a chat that can neither record nor remove the message leaves it to the homepage ------------
    r["left"] = {}
    for name, (values, fault) in LEFT_CASES.items():
        before = stored(tab, values())
        r["left"][name] = {**arrive(tab, site, f"<script>{fault}</script>"),
                           "envelopeBefore": before[HANDOFF_KEY], "ledgerBefore": before.get(LEDGER_KEY)}

    # ---- a message the chat took and sent while its envelope stayed; then the ledger is unreadable --------
    r["after-sent"] = {}
    for i, (name, later) in enumerate(AFTER_SENT.items()):
        stored(tab, {HANDOFF_KEY: envelope(draft=json.dumps("Sent before"), nonce=f"'{nonce(0x710 + i)}'")})
        site.app_prelude = f"<script>{storage_fault('removeItem', HANDOFF_KEY, ignore=True)}</script>"
        try:
            tab.open("/app/", CHAT_READY)
            time.sleep(0.6)
            taken = tab.chat()
            tab.click("#send-btn")
            assert tab.wait(REPLIED, timeout=10)
            time.sleep(0.4)
            answered = tab.chat()
            site.app_prelude = f"<script>{later}</script>"
            tab.dt.call("Page.reload", {}, tab.session)
            assert tab.wait(f"document.readyState === 'complete' && {CHAT_READY}")
            time.sleep(0.9)
            r["after-sent"][name] = {"taken": taken, "answered": answered, "reloaded": tab.chat()}
        finally:
            site.app_prelude = ""

    # ---- capacity: a list full of live nonces refuses, and never drops one -----------------------
    values = stored(tab, {LEDGER_KEY: ledger_js(FULL),
                          HANDOFF_KEY: envelope(draft=json.dumps("Capacity check"), createdAt=str(FIXED - 1000))})
    r["full"] = {**arrive(tab, site, PINNED_CLOCK), "ledgerBefore": values[LEDGER_KEY]}
    # The oldest live nonce -- the one an eviction would drop first -- is still refused.
    stored(tab, {HANDOFF_KEY: envelope(draft=json.dumps("Oldest again"), nonce=f"'{FULL[0][0]}'",
                                       createdAt=str(FIXED - 1000))}, clear=False)
    r["full-oldest-replayed"] = arrive(tab, site, PINNED_CLOCK)

    # ---- pruning: only expired entries go, and every live one stays ----------------------------
    stored(tab, {LEDGER_KEY: ledger_js(EXPIRED + LIVE_ENTRIES),
                 HANDOFF_KEY: envelope(draft=json.dumps("Pruning check"), nonce=f"'{nonce(0x400)}'",
                                       createdAt=str(FIXED - 2000))})
    r["pruned"] = arrive(tab, site, PINNED_CLOCK)
    stored(tab, {HANDOFF_KEY: envelope(draft=json.dumps("Live again"), nonce=f"'{LIVE_ENTRIES[0][0]}'",
                                       createdAt=str(FIXED - 1000))}, clear=False)
    r["pruned-live-replayed"] = arrive(tab, site, PINNED_CLOCK)
    stored(tab, {LEDGER_KEY: ledger_js(ALMOST_FULL),
                 HANDOFF_KEY: envelope(draft=json.dumps("Last place"), nonce=f"'{nonce(0x600)}'",
                                       createdAt=str(FIXED - 1000))})
    r["almost-full"] = arrive(tab, site, PINNED_CLOCK)
    return r


@pytest.fixture(scope="module")
def ledger(tmp_path_factory):
    browser = require_browser_and_build()
    profile = tmp_path_factory.mktemp("edge-handoff-ledger")
    site = Site()
    site.child_passive = True

    with site.http:
        with browser_page(browser, profile, VIEWPORTS["desktop"], mobile=False) as (devtools, session):
            result = run_ledger(Tab(devtools, session, site.http.origin), site)

    shutil.rmtree(profile, ignore_errors=True)
    return result


def sent_once(state: dict, draft: str):
    assert state["requests"] == [{"message": draft, "history": [], "session_id": None}], state["requests"]
    assert state["users"] == [draft] and state["notice"] == ""


def refused_silently(state: dict):
    assert state["requests"] == [] and state["users"] == [], state["requests"]
    assert state["value"] == "" and state["notice"] == "" and state["emptyShown"] is True


def placed_unsent(state: dict, draft: str, notice: str):
    assert state["requests"] == [] and state["users"] == [], state["requests"]
    assert not any(event["op"] == "fetch" for event in state["events"])
    assert state["value"] == draft and state["notice"] == notice and state["emptyShown"] is True
    # The chat owns it: an ordinary, editable draft.
    assert state["authority"] is None and state["readOnly"] is False and state["sendDisabled"] is False


def withheld(state: dict, draft: str):
    """The chat's copy of a message it could not show it took: kept whole for selecting and copying,
    and nothing on the page can send, edit, clear or replace it."""
    label = (state["value"][:40], state["notice"], state["requests"])
    assert state["requests"] == [] and state["users"] == [], label
    assert not any(event["op"] == "fetch" for event in state["events"]), label
    assert state["value"] == draft and len(list(state["value"])) == len(list(draft)), label
    assert state["notice"] == COPY_APP_UNVERIFIED and state["emptyShown"] is True, label
    assert state["authority"] == "withheld" and state["readOnly"] is True, label
    assert state["sendDisabled"] is True and state["eraseDisabled"] is True, label
    assert state["hintsDisabled"] and all(state["hintsDisabled"]), label


def test_an_older_envelope_restored_after_newer_handoffs_is_never_sent(ledger):
    sent_once(ledger["A"], "Message A")
    sent_once(ledger["B"], "Message B")
    # A was still fresh when restored, so only the ledger stopped it.
    assert 0 <= ledger["A-age-at-replay"] < 60000
    refused_silently(ledger["A-again"])


def test_an_immediate_replay_and_replays_after_other_handoffs_are_never_sent(ledger):
    for name in ("C", "D", "E"):
        sent_once(ledger[name], "Message " + name)
    refused_silently(ledger["E-again"])
    for name in ("A", "C"):
        assert 0 <= ledger[f"{name}-age-after-others"] < 60000
        refused_silently(ledger[f"{name}-after-others"])


def test_the_ledger_keeps_every_taken_nonce_and_no_message(ledger):
    raw = dict(ledger["C-after-others"]["session"])[LEDGER_KEY]
    entries = ledger_entries(raw)
    # Every handoff taken in this tab is still listed -- not just the latest.
    assert len(entries) == 5
    assert {nonce(0xA), nonce(0xC), nonce(0xD), nonce(0xE)} <= {entry["n"] for entry in entries}
    for draft in ledger["drafts"]:
        assert draft not in raw and draft.split()[0] not in raw
    assert "Message" not in raw and "draft" not in raw


@pytest.mark.parametrize("name", list(MALFORMED_LEDGERS))
def test_a_malformed_ledger_fails_closed_and_keeps_the_draft(ledger, name):
    state = ledger["malformed"][name]
    placed_unsent(state, "Ledger check", COPY_APP_UNCHECKED)
    session = dict(state["session"])
    # The envelope is gone; the ledger is left exactly as it was, never rebuilt.
    assert HANDOFF_KEY not in session and session[LEDGER_KEY] == state["ledgerBefore"]


@pytest.mark.parametrize("name", list(LEDGER_FAULTS))
def test_storage_faults_in_the_chat_fail_closed_and_keep_the_draft(ledger, name):
    state = ledger["faults"][name]
    placed_unsent(state, "Fault check", COPY_APP_UNCHECKED)
    session = dict(state["session"])
    if name.startswith("envelope-removal"):
        # Not removed, so never sent; the nonce is recorded, so a reload will not place it again.
        assert HANDOFF_KEY in session and len(ledger_entries(session[LEDGER_KEY])) == 1
    else:
        assert HANDOFF_KEY not in session


@pytest.mark.parametrize("name", list(LEFT_CASES))
def test_a_chat_that_cannot_show_it_took_the_message_leaves_it_to_the_homepage(ledger, name):
    state = ledger["left"][name]
    session = dict(state["session"])
    if name.startswith("removal-unconfirmed"):
        # The removal may have taken effect, so the chat keeps the draft rather than lose it -- but
        # nothing shows the homepage it was taken, so here it is only a read-only copy.
        withheld(state, "Left check")
        stored_nonce = json.loads(state["envelopeBefore"])["nonce"]
        assert LEDGER_KEY not in session or stored_nonce not in session[LEDGER_KEY]
        # Whether or not its removal took effect, the envelope is back exactly as it was, so the homepage
        # can still give the message back once this page is gone.
        assert session[HANDOFF_KEY] == state["envelopeBefore"]
        return
    # Nothing shows the homepage the chat took it, so the chat takes no copy: the
    # envelope stays exactly as it was, unrecorded, for the homepage to give back.
    assert state["requests"] == [] and state["users"] == [] and state["value"] == ""
    # With the ledger unreadable an earlier send cannot be ruled out, so the notice says nothing of the past.
    assert state["notice"] == (COPY_APP_LEFT_UNVERIFIED if "unreadable" in name else COPY_APP_LEFT)
    assert state["emptyShown"] is True
    assert session[HANDOFF_KEY] == state["envelopeBefore"]
    if state["ledgerBefore"] is None:
        assert LEDGER_KEY not in session or json.loads(state["envelopeBefore"])["nonce"] not in session[LEDGER_KEY]
    else:
        assert session[LEDGER_KEY] == state["ledgerBefore"]


@pytest.mark.parametrize("name", list(AFTER_SENT))
def test_a_message_already_sent_is_never_called_unsent_when_its_status_cannot_be_checked(ledger, name):
    run = ledger["after-sent"][name]
    # The chat took it (recorded), could not remove its envelope, and the user sent it: one request, answered.
    placed_unsent(run["taken"], "Sent before", COPY_APP_UNCHECKED)
    assert run["answered"]["requests"] == [{"message": "Sent before", "history": [], "session_id": None}]
    assert run["answered"]["assistants"] == 1
    reloaded = run["reloaded"]
    # Reloaded with the ledger unreadable: nothing is sent, and nothing claims it was never sent.
    assert reloaded["requests"] == [] and reloaded["users"] == []
    assert "wasn’t sent" not in reloaded["notice"]
    if name == "removal-unconfirmed":
        withheld(reloaded, "Sent before")
    else:
        assert reloaded["value"] == "" and reloaded["notice"] == COPY_APP_LEFT_UNVERIFIED
        assert reloaded["authority"] is None


def test_a_ledger_full_of_live_nonces_refuses_and_drops_none(ledger):
    full = ledger["full"]
    placed_unsent(full, "Capacity check", COPY_APP_UNCHECKED)
    assert dict(full["session"])[LEDGER_KEY] == full["ledgerBefore"]
    assert [entry["n"] for entry in ledger_entries(full["ledgerBefore"])] == [n for n, _ in FULL]
    # The oldest live nonce is still there: its envelope is refused, silently.
    refused_silently(ledger["full-oldest-replayed"])
    assert dict(ledger["full-oldest-replayed"]["session"])[LEDGER_KEY] == full["ledgerBefore"]


def test_only_expired_entries_are_pruned(ledger):
    sent_once(ledger["pruned"], "Pruning check")
    entries = ledger_entries(dict(ledger["pruned"]["session"])[LEDGER_KEY])
    # Entries at or before the clock are gone; every live one stays, in order,
    # and the new one is kept for the acceptance window plus the clock allowance.
    assert entries == [{"n": n, "x": int(x)} for n, x in LIVE_ENTRIES] + [
        {"n": nonce(0x400), "x": FIXED - 2000 + 65000}]
    refused_silently(ledger["pruned-live-replayed"])
    # Pruning one expired entry makes room for exactly one more.
    sent_once(ledger["almost-full"], "Last place")
    entries = ledger_entries(dict(ledger["almost-full"]["session"])[LEDGER_KEY])
    assert entries == [{"n": n, "x": int(x)} for n, x in ALMOST_FULL[:31]] + [
        {"n": nonce(0x600), "x": FIXED - 1000 + 65000}]


# --- /app/'s own send deadline (browser) -------------------------------------------------------------

# name: (the envelope's age when /app/ reads it, in ms; what /app/ does with it)
DEADLINES = {
    "fresh": (0, "sent"),
    "just-before-the-deadline": (7999, "sent"),
    "at-the-deadline": (8000, "late"),
    "just-after-the-deadline": (8001, "late"),
    "last-late-moment": (59999, "late"),
    "stale": (60000, "left"),
    "earliest-clock-allowance": (-5000, "sent"),
    "beyond-the-clock-allowance": (-5001, "discarded"),
}

# Seconds /app/ is held back after a real Send, and what must follow: sent
# before /app/'s deadline (8 s), placed unsent after it, given back by the
# homepage after its own (10 s), and at the homepage's deadline one of the two.
ARRIVALS = {"6.0": "sent", "9.0": "late", "10.0": "either", "11.0": "given-back"}


# Either /app/ opened, or the homepage gave the draft back (and stayed).
SETTLED = (f"(location.pathname === '/app/' && {CHAT_READY}) || (location.pathname === '/' && "
           "[...document.querySelectorAll('[data-testid=\"home-composer-status\"], "
           "[data-testid=\"home-composer-alert\"]')].some(region => region.textContent.includes('didn’t open')))")


def send_and_hold(tab: Tab, site: Site, draft: str, delay: float, fault: str = "") -> dict:
    """Type `draft` on a cacheable homepage and press Send while /app/ is held
    back `delay` seconds; `fault` runs in the homepage first. If the homepage
    gives the draft back ("home"), it is read again after /app/ would have
    answered, and then /app/ is opened by hand -- the late chat ("arrived")."""
    site.http.cacheable = {"/"}
    site.app_delay = delay
    try:
        tab.open("/", HOME_READY)
        tab.click("#home-composer-input")
        tab.type(draft)
        site.http.reports.clear()
        started = time.monotonic()
        run = {"pending": tab.eval(fault + SEND_OBSERVED), "started": started}
        assert tab.wait(SETTLED, timeout=delay + 20)
        if tab.eval("return location.pathname;") == "/app/":
            time.sleep(0.9)
            run["arrived"] = tab.chat()
        else:
            run["home"] = tab.home()
            time.sleep(max(0.0, delay + 1.5 - (time.monotonic() - started)))
            run["homeLater"] = tab.home()
    finally:
        site.app_delay = 0.0
    run["givenBack"] = site.http.reports[0] if site.http.reports else None
    if "home" in run:
        tab.open("/app/", CHAT_READY)
        time.sleep(0.9)
        run["arrived"] = tab.chat()
    return run


def run_deadline(tab: Tab, site: Site) -> dict:
    r: dict = {"boundaries": {}, "arrivals": {}}

    # ---- exact boundaries on a pinned clock ------------------------------------------------------
    for name, (age, _) in DEADLINES.items():
        draft = "Deadline " + name
        before = stored(tab, {HANDOFF_KEY: envelope(draft=json.dumps(draft), createdAt=str(FIXED - age))})
        r["boundaries"][name] = {**arrive(tab, site, PINNED_CLOCK), "envelopeBefore": before[HANDOFF_KEY]}
        if name == "at-the-deadline":
            # The same late envelope offered again is never shown or sent again.
            stored(tab, {HANDOFF_KEY: envelope(draft=json.dumps(draft), createdAt=str(FIXED - age))}, clear=False)
            r["late-replayed"] = arrive(tab, site, PINNED_CLOCK)

    # ---- real arrivals around both deadlines -------------------------------------------------------
    for delay, _ in ARRIVALS.items():
        draft = f"Arriving after {delay} seconds"
        run = send_and_hold(tab, site, draft, float(delay))
        tab.history(-1, "/")
        assert tab.wait(HOME_READY)
        run["back"] = tab.home()
        r["arrivals"][delay] = run

    # ---- a message consumed normally leaves a clean homepage, even after the homepage's deadline --
    site.http.cacheable = {"/"}
    tab.open("/", HOME_READY)
    tab.click("#home-composer-input")
    tab.type("Consumed normally")
    sent = time.monotonic()
    tab.eval("window.__shows = []; addEventListener('pageshow', e => window.__shows.push(e.persisted)); "
             "document.querySelector('[data-testid=\"button-home-send\"]').click(); return true;")
    assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY} && {REPLIED}")
    time.sleep(0.4)
    r["normal"] = tab.chat()
    tab.history(-1, "/")
    assert tab.wait(HOME_READY)
    r["normalBack"] = tab.home()
    time.sleep(max(0.0, 11.5 - (time.monotonic() - sent)))
    r["normalBackLater"] = tab.home()
    tab.history(1, "/app/")
    assert tab.wait(CHAT_READY)
    time.sleep(0.6)
    r["normalForward"] = tab.chat()
    return r


@pytest.fixture(scope="module")
def deadline(tmp_path_factory):
    browser = require_browser_and_build()
    profile = tmp_path_factory.mktemp("edge-handoff-deadline")
    site = Site()
    site.child_passive = True

    with site.http:
        with browser_page(browser, profile, VIEWPORTS["desktop"], mobile=False) as (devtools, session):
            result = run_deadline(Tab(devtools, session, site.http.origin), site)

    shutil.rmtree(profile, ignore_errors=True)
    return result


@pytest.mark.parametrize("name", list(DEADLINES))
def test_the_chat_sends_only_before_its_own_deadline(deadline, name):
    state = deadline["boundaries"][name]
    age, outcome = DEADLINES[name]
    draft = "Deadline " + name
    session = dict(state["session"])
    if outcome == "left":
        # Too old for /app/ ever to take: not its to remove, so left exactly as it was for the homepage.
        assert session.get(HANDOFF_KEY) == state["envelopeBefore"]
    else:
        assert HANDOFF_KEY not in session
    if outcome == "sent":
        sent_once(state, draft)
    elif outcome == "late":
        placed_unsent(state, draft, COPY_APP_LATE)
    else:
        refused_silently(state)
    if outcome in ("discarded", "left"):
        assert LEDGER_KEY not in session
    else:
        # Taken -- sent or placed -- so recorded, until the window and the clock allowance have passed.
        assert ledger_entries(session[LEDGER_KEY]) == [{"n": NONCE, "x": FIXED - age + 65000}]


def test_a_late_envelope_is_placed_once_and_never_sent(deadline):
    refused_silently(deadline["late-replayed"])


@pytest.mark.parametrize("delay", list(ARRIVALS))
def test_a_real_arrival_is_sent_or_kept_but_never_both(deadline, delay):
    run = deadline["arrivals"][delay]
    draft = f"Arriving after {delay} seconds"
    pending, given_back, arrived, back = run["pending"], run["givenBack"], run["arrived"], run["back"]
    assert pending["value"] == "" and pending["readOnly"] is True and HANDOFF_KEY in pending["session"]
    outcome = ARRIVALS[delay]
    if outcome == "either":
        # At the homepage's deadline either side may act first; the draft ends up in exactly one place.
        outcome = "given-back" if "home" in run else "late"
    if outcome == "sent":
        assert "home" not in run and given_back is None
        sent_once(arrived, draft)
    elif outcome == "late":
        assert "home" not in run and given_back is None
        placed_unsent(arrived, draft, COPY_APP_LATE)
    else:
        # The homepage gave the draft back and stopped the navigation: it stays, with the draft.
        assert "home" in run, "the chat opened after the homepage's deadline"
        for state in (run["home"], run["homeLater"]):
            assert state["path"] == "/" and state["value"] == draft and state["status"] == COPY_STALLED
            assert state["readOnly"] is False and HANDOFF_KEY not in dict(state["session"])
        assert given_back is not None and given_back["value"] == draft
        # The chat opened afterwards has nothing to send.
        refused_silently(arrived)
    # Back: a usable homepage with no stale notice. Only a page the browser kept
    # (the back/forward cache) can still hold a draft it gave back.
    assert back["readOnly"] is False and back["status"] == "" and back["alert"] == ""
    assert back["value"] == (draft if outcome == "given-back" and back["restored"] else "")


def test_a_message_consumed_normally_leaves_a_clean_homepage(deadline):
    sent_once(deadline["normal"], "Consumed normally")
    for state in (deadline["normalBack"], deadline["normalBackLater"]):
        # Well past the homepage's 10-second deadline, nothing comes back --
        # whether the browser kept the page (its timer would resume) or reloaded it.
        assert state["value"] == "" and state["readOnly"] is False
        assert state["status"] == "" and state["alert"] == "" and state["path"] == "/"
    assert deadline["normalBackLater"]["restored"] == deadline["normalBack"]["restored"]
    forward = deadline["normalForward"]
    assert len(forward["requests"]) <= 1 and forward["notice"] == ""
    if forward["users"]:
        assert forward["users"] == ["Consumed normally"]


# --- recovery that never depends on reading storage (browser) ----------------------------------------

READ_FAILS = storage_fault("getItem", HANDOFF_KEY, after=1)      # Send's own check still succeeds
REMOVAL_FAILS = storage_fault("removeItem", HANDOFF_KEY)
OVERWRITE_FAILS = storage_fault("setItem", HANDOFF_KEY, after=1)  # Send's own write still succeeds
TIMER_SUSPENDED = ("(function () { const real = window.setTimeout; window.setTimeout = function (fn, ms, ...rest) "
                   "{ return ms === 10000 ? 0 : real.call(window, fn, ms, ...rest); }; })();")
TIMER_DELAYED = ("(function () { const real = window.setTimeout; window.setTimeout = function (fn, ms, ...rest) "
                 "{ return real.call(window, fn, ms === 10000 ? 20000 : ms, ...rest); }; })();")

STALLS = {
    # name: (homepage fault, the recovery -- (notice region, text, read-only) -- the late chat's
    # outcome, and what Back to the cached homepage shows)
    "nominal": ("", ("status", COPY_STALLED, False), "clean", "given-back"),
    # The removal may have worked, but it cannot be confirmed: the draft stays read-only; Back then
    # cannot tell who has it.
    "recovery-read-fails": (READ_FAILS, ("alert", COPY_STALLED_HELD, True), "clean", "held-unknown"),
    # A removal that fails is replaced by the withdrawn marker, which the chat discards.
    "removal-fails": (REMOVAL_FAILS, ("status", COPY_STALLED, False), "clean", "given-back"),
    "read-and-removal-fail": (READ_FAILS + REMOVAL_FAILS, ("alert", COPY_STALLED_HELD, True), "clean",
                              "held-unknown"),
    # Neither removed nor replaced: the late chat may take it -- and then it is the chat's.
    "removal-and-overwrite-fail": (REMOVAL_FAILS + OVERWRITE_FAILS, ("alert", COPY_STALLED_HELD, True), "late",
                                   "clean"),
    "timer-suspended": (TIMER_SUSPENDED, None, "late", "clean"),
    "timer-delayed": (TIMER_DELAYED, None, "late", "clean"),
}

def run_stalls(tab: Tab, site: Site) -> dict:
    r: dict = {}
    for name, (fault, _, _, _) in STALLS.items():
        draft = f"  Stalled: {name} — 日本語 \U0001F642  "
        run = send_and_hold(tab, site, draft, 12.0, fault)
        run["draft"] = draft.strip()
        # From the chat, Back to the homepage that sent the draft, then Forward again.
        tab.history(-1, "/")
        assert tab.wait(HOME_READY)
        run["back"] = tab.home()
        if name == "timer-delayed":
            # Past the delayed timer's 20 seconds: a homepage the chat opened from never wakes up.
            time.sleep(max(0.0, 21.5 - (time.monotonic() - run["started"])))
            run["backLater"] = tab.home()
            run["reportsLater"] = list(site.http.reports)
        tab.history(1, "/app/")
        assert tab.wait(CHAT_READY)
        time.sleep(0.6)
        run["forward"] = tab.chat()
        r[name] = run
    return r


@pytest.fixture(scope="module")
def stalls(tmp_path_factory):
    browser = require_browser_and_build()
    profile = tmp_path_factory.mktemp("edge-handoff-stalls")
    site = Site()
    site.child_passive = True

    with site.http:
        with browser_page(browser, profile, VIEWPORTS["desktop"], mobile=False) as (devtools, session):
            result = run_stalls(Tab(devtools, session, site.http.origin), site)

    shutil.rmtree(profile, ignore_errors=True)
    return result


@pytest.mark.parametrize("name", list(STALLS))
def test_a_stalled_chat_never_sends_and_the_draft_is_never_lost(stalls, name):
    run = stalls[name]
    _, notice, late, _ = STALLS[name]
    draft, pending, given_back, arrived = run["draft"], run["pending"], run["givenBack"], run["arrived"]
    # Send's own write and check succeeded: the draft left the box for the chat.
    assert pending["value"] == "" and pending["readOnly"] is True and HANDOFF_KEY in pending["session"]
    assert pending["status"] == "Opening the chat…" and pending["alert"] == ""
    if notice is None:
        # The homepage's timer never ran before the chat opened: nothing was given back there.
        assert given_back is None and "home" not in run
    else:
        # The exact draft came back from page memory, announced -- in a usable box, or read-only
        # while the chat could still take an envelope this page could not withdraw.
        region, text, read_only = notice
        assert given_back is not None, "the homepage never gave the draft back"
        assert given_back["value"] == draft and given_back[region] == text
        assert given_back[{"alert": "status", "status": "alert"}[region]] == ""
        assert given_back["readOnly"] is read_only and given_back["sendDisabled"] is read_only
        # Withdrawn unless the removal itself failed (the ledger of earlier handoffs may remain).
        assert (HANDOFF_KEY in given_back["session"]) == ("removal" in name)
        # The navigation was stopped: after /app/ would have answered, the page and the draft are still here.
        assert "home" in run
        for state in (run["home"], run["homeLater"]):
            assert state["path"] == "/" and state["value"] == draft and state[region] == text
            assert state["readOnly"] is read_only and state["sendDisabled"] is read_only
    # The chat that opens late -- by itself, or by hand after the draft came back -- never sends it.
    if late == "late":
        placed_unsent(arrived, draft, COPY_APP_LATE)
    else:
        refused_silently(arrived)
    assert HANDOFF_KEY not in dict(arrived["session"])


@pytest.mark.parametrize("name", list(STALLS))
def test_back_forward_and_the_cache_after_a_stall_send_nothing(stalls, name):
    run = stalls[name]
    _, _, _, after = STALLS[name]
    back, forward, arrived = run["back"], run["forward"], run["arrived"]
    assert back["path"] == "/" and back["status"] == ""
    if after == "held-unknown" and back["restored"]:
        # The page kept the read-only draft; whether the chat took it cannot be told, so it stays read-only.
        assert back["value"] == run["draft"] and back["readOnly"] is True and back["alert"] == COPY_HELD_UNKNOWN
    else:
        # A usable homepage with no stale notice; only a page the browser kept can hold a draft it gave back,
        # and none at all once the chat took the message.
        assert back["readOnly"] is False and back["alert"] == ""
        assert back["value"] == (run["draft"] if after == "given-back" and back["restored"] else "")
    if name == "timer-delayed":
        # Past the delayed timer's 20 seconds, a homepage the chat opened from never wakes up.
        later = run["backLater"]
        assert later["value"] == "" and later["status"] == "" and later["alert"] == "" and run["reportsLater"] == []
    # Forward: the chat as it was (kept by the browser) or a fresh one -- either way nothing is sent.
    assert forward["requests"] == [] and forward["users"] == []
    assert (forward["value"], forward["notice"]) in ((arrived["value"], arrived["notice"]), ("", ""))


# --- Back before the chat took the draft (browser) ------------------------------------------------------

# Storage faults in homepage documents only: (method, calls to allow first,
# do nothing instead of throwing[, key -- the envelope's unless given]). They
# arm once the homepage has been left -- so Send itself works -- or at once in
# a homepage opened by Back, Forward or reload.
EARLY_FAULTS = {
    "none": [],
    "removal-throws": [("removeItem", 0, False)],
    "removal-ignored": [("removeItem", 0, True)],
    "read-throws": [("getItem", 0, False)],
    "confirmation-read-throws": [("getItem", 1, False)],
    "removal-and-overwrite-fail": [("removeItem", 0, False), ("setItem", 0, False)],
    "removal-and-overwrite-ignored": [("removeItem", 0, True), ("setItem", 0, True)],
    "read-and-removal-throw": [("getItem", 0, False), ("removeItem", 0, False)],
}


def homepage_prelude(faults) -> str:
    """A script for every new document: in homepage documents, record each
    showing (pageshow.persisted) and install the armed `faults`."""
    wrappers = []
    for method, allow, ignore, *key in faults:
        effect = "return undefined;" if ignore else "throw new DOMException('denied', 'SecurityError');"
        wrappers.append(
            f"(function () {{ const real = Storage.prototype.{method}; let n = 0; "
            f"Storage.prototype.{method} = function (key) {{ if (armed && key === {json.dumps((key or [HANDOFF_KEY])[0])} "
            f"&& ++n > {allow}) {{ {effect} }} return real.apply(this, arguments); }}; }})();")
    return ("(function () { if (location.pathname !== '/') return; "
            "window.__shows = []; addEventListener('pageshow', e => window.__shows.push(e.persisted)); "
            "const nav = performance.getEntriesByType('navigation')[0]; "
            "let armed = !!nav && (nav.type === 'back_forward' || nav.type === 'reload'); "
            "addEventListener('pagehide', () => { armed = true; }); " + " ".join(wrappers) + " })();")


# What each step must find. Homepage steps: (step, box value, read-only, status, alert, envelope key);
# "draft" is the flow's exact draft, and the envelope is "intact" (the very envelope Send stored),
# "marker" (its withdrawn marker), "absent" or None (not checked). Chat steps: (step, outcome) --
# "sent" (one request, of the draft), "none" (nothing shown or sent), "late" (placed unsent),
# "no-new" (no request beyond those already made).
HELD = ("draft", True, "", COPY_HELD)
HELD_UNKNOWN = ("draft", True, "", COPY_HELD_UNKNOWN)
CLEAN = ("", False, "", "")

# flow: (fault, seconds app.js is held, steps, requests the flow may make in all)
EARLY_FLOWS = {
    "removal-works": ("none", 6.0, [
        ("back", "draft", False, COPY_RETURNED, "", "absent"), ("forward", "none"),
        ("back", "draft", False, "", "", "absent"), ("forward", "none")], 0),
    "removal-throws": ("removal-throws", 6.0, [
        ("back", "draft", False, COPY_RETURNED, "", "marker"), ("forward", "none"),
        ("back", "draft", False, "", "", "absent"), ("forward", "none")], 0),
    "removal-ignored": ("removal-ignored", 6.0, [
        ("back", "draft", False, COPY_RETURNED, "", "marker"), ("forward", "none"),
        ("back", "draft", False, "", "", "absent"), ("forward", "none")], 0),
    # Held while the chat could take it; the chat then did, so it is the chat's.
    "kept-then-forward": ("removal-and-overwrite-fail", 6.0, [
        ("back", *HELD, "intact"), ("forward", "sent"),
        ("back", *CLEAN, "absent"), ("wait", *CLEAN, "absent"), ("forward", "no-new")], 1),
    "unreadable-then-forward": ("read-throws", 6.0, [
        ("back", *HELD_UNKNOWN, "intact"), ("forward", "sent"),
        ("back", *CLEAN, "absent"), ("wait", *CLEAN, "absent"), ("forward", "no-new")], 1),
    # Past the chat's 8-second send deadline the draft is still held: that deadline does not
    # show the chat could no longer take it.
    "unconfirmed-then-wait": ("confirmation-read-throws", 6.0, [
        ("back", *HELD, "absent"), ("wait", *HELD, "absent"), ("forward", "none"),
        ("back", *HELD_UNKNOWN, "absent")], 0),
    "ignored-then-wait": ("removal-and-overwrite-ignored", 6.0, [
        ("back", *HELD, "intact"), ("wait", *HELD, "intact"), ("forward", "late"), ("back", *CLEAN, "absent")], 0),
    "unreadable-unremovable-then-wait": ("read-and-removal-throw", 6.0, [
        ("back", *HELD_UNKNOWN, "intact"), ("wait", *HELD_UNKNOWN, "intact"), ("forward", "late"),
        ("back", *CLEAN, "absent")], 0),
    "kept-then-reload": ("removal-and-overwrite-fail", 6.0, [
        ("back", *HELD, "intact"), ("reload", *HELD, "intact"), ("wait", *HELD, "intact"), ("forward", "late"),
        ("back", *CLEAN, "absent")], 0),
    "returned-then-reload": ("removal-throws", 6.0, [
        ("back", "draft", False, COPY_RETURNED, "", "marker"), ("reload", *CLEAN, "marker"),
        ("forward", "none")], 0),
    "unreadable-then-reload": ("read-throws", 6.0, [
        ("back", *HELD_UNKNOWN, "intact"), ("reload", *CLEAN, "intact"), ("forward", "sent")], 1),
    "kept-after-the-deadline": ("removal-and-overwrite-fail", 10.0, [
        ("wait-in-chat",), ("back", *HELD, "intact"), ("forward", "late"), ("back", *CLEAN, "absent")], 0),
    "returned-after-the-deadline": ("none", 10.0, [
        ("wait-in-chat",), ("back", "draft", False, COPY_RETURNED, "", "absent"), ("forward", "none")], 0),
    "consumed-before-back": ("none", 0.0, [
        ("back", *CLEAN, "absent"), ("wait", *CLEAN, "absent"), ("forward", "no-new")], 1),
    # The chat's ledger shows it took the message, so a homepage that cannot read the envelope is still clean.
    "consumed-before-back-unreadable": ("read-throws", 0.0, [
        ("back", *CLEAN, "absent"), ("wait", *CLEAN, "absent"), ("forward", "no-new")], 1),
    # A draft this page could not withdraw is not released by time -- not even once the chat can no
    # longer take it, as its envelope is still here for another page to give back. The chat, too old to
    # take it, leaves that envelope alone, and Back still holds the draft.
    "held-past-the-chat-window": ("removal-and-overwrite-fail", 6.0, [
        ("back", *HELD, "intact"), ("wait", *HELD, "intact"), ("forward", "none", "intact"),
        ("back", *HELD, "intact")], 0),
    # A draft whose owner cannot be told is never released by time alone.
    "unknown-never-released-by-time": ("read-throws", 6.0, [
        ("back", *HELD_UNKNOWN, "intact"), ("wait", *HELD_UNKNOWN, "intact"), ("forward", "none", "intact"),
        ("back", *HELD_UNKNOWN, "intact")], 0),
}
# Seconds after Send at which a flow's "wait" step looks again (9.5 by default: past the chat's
# 8-second send deadline).
EARLY_WAITS = {"consumed-before-back": 11.0, "held-past-the-chat-window": 61.5,
               "unknown-never-released-by-time": 61.5}

OPENED = (f"location.pathname === '/app/' && document.getElementById('composer-input') "
          f"&& document.readyState === 'loading'")


def early_draft(flow: str) -> str:
    if flow == "removal-throws":
        return "\U0001F642" * 4000                         # exactly the limit: nothing may be cut
    return f"  Early Back: {flow} — 日本語 \U0001F642  "


def run_early_flow(tab: Tab, site: Site, flow: str) -> dict:
    fault, held_for, steps, _ = EARLY_FLOWS[flow]
    prelude = tab.on_new_documents(homepage_prelude(EARLY_FAULTS[fault]))
    run: dict = {"draft": early_draft(flow).strip(), "steps": []}
    try:
        # A clean tab: an earlier flow's envelope, which a fresh homepage would rightly give back, is not about.
        stored(tab, {})
        site.http.cacheable = {"/"}
        site.script_delay = held_for
        tab.open("/", HOME_READY)
        tab.click("#home-composer-input")
        tab.type(early_draft(flow))
        started = time.monotonic()
        tab.click('[data-testid="button-home-send"]')
        # The chat's page has opened; with app.js held, nothing has taken the envelope yet.
        assert tab.wait(OPENED if held_for else f"location.pathname === '/app/' && {CHAT_READY} && {REPLIED}",
                        timeout=8)
        if not held_for:
            time.sleep(0.4)
        run["opened"] = tab.chat()
        for step in steps:
            kind = step[0]
            if kind == "wait-in-chat":
                time.sleep(max(0.0, started + 8.6 - time.monotonic()))
                continue
            if kind == "back":
                tab.history(-1, "/")
                assert tab.wait(HOME_READY)
                state = tab.home()
            elif kind == "reload":
                tab.dt.call("Page.reload", {}, tab.session)
                assert tab.wait(f"document.readyState === 'complete' && {HOME_READY}")
                time.sleep(0.4)
                state = tab.home()
            elif kind == "wait":
                time.sleep(max(0.0, started + EARLY_WAITS.get(flow, 9.5) - time.monotonic()))
                state = tab.home()
            else:
                site.script_delay = 0.0
                tab.history(1, "/app/")
                assert tab.wait(CHAT_READY)
                time.sleep(0.9)
                state = tab.chat()
            run["steps"].append({"step": kind, **state})
    finally:
        site.script_delay = 0.0
        tab.forget(prelude)
    return run


@pytest.fixture(scope="module", params=["desktop", "mobile"])
def early_back(request, tmp_path_factory):
    browser = require_browser_and_build()
    viewport = request.param
    profile = tmp_path_factory.mktemp(f"edge-handoff-early-back-{viewport}")
    site = Site()
    site.child_passive = True
    flows = {}

    with site.http:
        with browser_page(browser, profile, VIEWPORTS[viewport], viewport == "mobile") as (devtools, session):
            # Back here must exercise the back/forward cache: keep a machine's
            # memory pressure from emptying it.
            devtools.call("Memory.setPressureNotificationsSuppressed", {"suppressed": True})
            tab = Tab(devtools, session, site.http.origin)
            for flow in EARLY_FLOWS:
                flows[flow] = run_early_flow(tab, site, flow)

    shutil.rmtree(profile, ignore_errors=True)
    return flows


@pytest.mark.parametrize("flow", list(EARLY_FLOWS))
def test_a_draft_given_back_on_early_back_has_one_owner_and_is_never_sent_twice(early_back, flow):
    run = early_back[flow]
    fault, held_for, steps, allowed = EARLY_FLOWS[flow]
    draft, opened = run["draft"], run["opened"]
    if held_for:
        # The chat's page opened, but nothing had taken the envelope: it is exactly what Send stored.
        assert opened["requests"] == [] and opened["path"] == "/app/"
        envelope_raw = dict(opened["session"])[HANDOFF_KEY]
        sent = json.loads(envelope_raw)
        assert sent["draft"] == draft and len(list(sent["draft"])) == len(list(draft))
    else:
        sent_once(opened, draft)
        envelope_raw, sent = None, None
    documents = {opened["document"]: len(opened["requests"])}
    first_back = True

    for expected, state in zip(steps[[s[0] for s in steps].count("wait-in-chat"):], run["steps"]):
        kind = expected[0]
        assert state["step"] == kind
        if kind in ("back", "reload", "wait"):
            _, value, read_only, status, alert, envelope = expected
            label = (flow, kind, state)
            if kind == "back" and first_back:
                # A genuine Back to the cached homepage that pressed Send.
                assert state["restored"] is True, label
                first_back = False
            if kind == "reload":
                assert state["restored"] is False, label
            assert state["path"] == "/", label
            assert state["value"] == (draft if value == "draft" else ""), label
            assert state["readOnly"] is read_only and state["sendDisabled"] is (read_only or value == ""), label
            assert state["status"] == status and state["alert"] == alert, label
            stored = dict(state["session"]).get(HANDOFF_KEY)
            if envelope == "absent":
                assert stored is None, label
            elif envelope == "intact":
                assert stored == envelope_raw, label
            elif envelope == "marker":
                assert withdrawn_marker(stored, sent["nonce"]), label
                assert draft[:8] not in stored, label
        else:
            outcome = expected[1]
            label = (flow, outcome, state["requests"], state["value"][:40], state["notice"])
            new = state["document"] not in documents
            documents[state["document"]] = len(state["requests"])
            if outcome == "sent":
                assert new, label
                sent_once(state, draft)
            elif outcome == "late":
                assert new, label
                placed_unsent(state, draft, COPY_APP_LATE)
            elif outcome == "none":
                assert state["requests"] == [] and state["users"] == [], label
                assert state["value"] == "" and state["notice"] == "", label
            else:
                assert not new or state["requests"] == [], label
            if expected[2:] == ("intact",):
                # Too old for the chat ever to take: it leaves the envelope exactly as it was.
                assert dict(state["session"]).get(HANDOFF_KEY) == envelope_raw, label
            else:
                # The chat never keeps an envelope it could take, once it has looked at it.
                assert HANDOFF_KEY not in dict(state["session"]), label
    # Never a duplicate: across every chat page this flow opened, at most the one allowed request.
    assert sum(documents.values()) == allowed, documents


# --- the chat took the message but could not remove its envelope (browser) ------------------------------

# Chat-side storage faults, in /app/ documents only. With the envelope's
# removal failing the chat still takes the message -- its nonce goes into the
# ledger -- and the envelope stays behind; with recording failing too, the
# chat cannot show it took it, and leaves it to the homepage.
APP_FAULTS = {
    "removal-throws": storage_fault("removeItem", HANDOFF_KEY),
    "removal-ignored": storage_fault("removeItem", HANDOFF_KEY, ignore=True),
    "removal-and-recording-fail": storage_fault("removeItem", HANDOFF_KEY) + storage_fault("setItem", LEDGER_KEY),
}
APP_DRAFTS = {
    "plain": "  The chat owns this one — 日本語 \U0001F642  ",
    "error": "ERROR: the chat owns this one too",
    "hang": "HANG: the chat is still sending this one",
    "emoji": "\U0001F642" * 4000,
}
OWNED_CLEAN = ("back", "", False, "", "", "intact")      # the chat's envelope, left exactly where it was

# flow: (chat fault, homepage faults, draft, steps, requests the user starts)
# Steps: homepage ("back", "reload", "wait") as in EARLY_FLOWS; ("send", until) presses Send in the chat;
# ("forward" | "app-reload", what the chat shows) -- "owned": its unsent copy if the browser kept the
# page, else nothing (a fresh chat ignores an envelope it already took); "kept": the same page as before
# or a fresh, empty one; "left": the chat's notice that it left the message to the homepage, or nothing.
APP_FLOWS = {
    "removal-throws-back-before-send": ("removal-throws", [], "plain", [
        OWNED_CLEAN, ("forward", "owned"), OWNED_CLEAN, ("reload", "", False, "", "", "intact"),
        ("forward", "owned")], 0),
    "removal-ignored-back-before-send": ("removal-ignored", [], "plain", [
        OWNED_CLEAN, ("forward", "owned"), OWNED_CLEAN, ("forward", "owned"), OWNED_CLEAN], 0),
    "send-then-back": ("removal-throws", [], "plain", [
        ("send", "answered"), OWNED_CLEAN, ("forward", "kept"), OWNED_CLEAN, ("wait", "", False, "", "", "intact")], 1),
    "send-then-back-removal-ignored": ("removal-ignored", [], "plain", [
        ("send", "answered"), OWNED_CLEAN, ("forward", "kept"), OWNED_CLEAN], 1),
    "error-then-back": ("removal-ignored", [], "error", [
        ("send", "answered"), OWNED_CLEAN, ("forward", "kept"), OWNED_CLEAN], 1),
    "sending-then-back": ("removal-throws", [], "hang", [
        ("send", "in-flight"), OWNED_CLEAN, ("forward", "kept"), OWNED_CLEAN], 1),
    "emoji-4000-sent-then-back": ("removal-ignored", [], "emoji", [
        ("send", "answered"), OWNED_CLEAN, ("forward", "kept")], 1),
    # Whether the chat took it cannot be told: the draft stays read-only, as no second copy, and the
    # 8-second deadline does not release it.
    "homepage-cannot-read-the-ledger": ("removal-throws", [("getItem", 0, False, LEDGER_KEY)], "plain", [
        ("back", "draft", True, "", COPY_HELD_UNKNOWN, "intact"),
        ("wait", "draft", True, "", COPY_HELD_UNKNOWN, "intact"), ("forward", "owned"),
        ("back", "draft", True, "", COPY_HELD_UNKNOWN, "intact")], 0),
    # The ledger alone decides: an unreadable envelope does not matter.
    "homepage-cannot-read-the-envelope": ("removal-throws", [("getItem", 0, False)], "plain", [
        OWNED_CLEAN, ("forward", "owned"), OWNED_CLEAN], 0),
    "repeated-initialization": ("removal-ignored", [], "plain", [
        ("app-reload", "kept"), ("app-reload", "kept"), OWNED_CLEAN, ("reload", "", False, "", "", "intact"),
        ("reload", "", False, "", "", "intact"), ("forward", "kept")], 0),
    # The chat could neither record nor remove it, so it took no copy: the homepage gives it back.
    "chat-cannot-take-it": ("removal-and-recording-fail", [], "plain", [
        ("back", "draft", False, COPY_RETURNED, "", "absent"), ("forward", "left"),
        ("back", "kept-draft", False, "", "", "absent")], 0),
}
APP_VIEWS = {"desktop": ("desktop", ()), "mobile": ("mobile", ()),
             "desktop-without-cache": ("desktop", ("--disable-back-forward-cache",))}


def run_app_flow(tab: Tab, site: Site, flow: str) -> dict:
    fault, home_faults, kind, steps, _ = APP_FLOWS[flow]
    typed = APP_DRAFTS[kind]
    prelude = tab.on_new_documents(homepage_prelude(home_faults))
    run: dict = {"draft": typed.strip(), "steps": []}
    try:
        site.http.cacheable = {"/", "/app/"}
        site.app_prelude = f"<script>{APP_FAULTS[fault]}</script>"
        tab.open("/", HOME_READY)
        tab.click("#home-composer-input")
        tab.type(typed)
        started = time.monotonic()
        tab.click('[data-testid="button-home-send"]')
        assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY}", timeout=10)
        time.sleep(0.6)
        run["opened"] = tab.chat()
        for step in steps:
            what = step[0]
            if what == "send":
                tab.click("#send-btn")
                if step[1] == "in-flight":
                    time.sleep(0.6)
                else:
                    assert tab.wait("!document.getElementById('send-btn').classList.contains('is-stop') && "
                                    "document.querySelectorAll('#thread .msg-assistant').length >= 1", timeout=10)
                    time.sleep(0.4)
                state = tab.chat()
            elif what == "back":
                tab.history(-1, "/")
                assert tab.wait(HOME_READY)
                state = tab.home()
            elif what == "reload":
                tab.dt.call("Page.reload", {}, tab.session)
                assert tab.wait(f"document.readyState === 'complete' && {HOME_READY}")
                time.sleep(0.4)
                state = tab.home()
            elif what == "wait":
                time.sleep(max(0.0, started + 9.5 - time.monotonic()))
                state = tab.home()
            elif what == "app-reload":
                tab.dt.call("Page.reload", {}, tab.session)
                assert tab.wait(f"document.readyState === 'complete' && {CHAT_READY}")
                time.sleep(0.6)
                state = tab.chat()
            else:
                tab.history(1, "/app/")
                assert tab.wait(CHAT_READY)
                time.sleep(0.6)
                state = tab.chat()
            run["steps"].append({"step": what, **state})
    finally:
        site.app_prelude = ""
        tab.forget(prelude)
    return run


@pytest.fixture(scope="module", params=list(APP_VIEWS))
def app_owned(request, tmp_path_factory):
    browser = require_browser_and_build()
    viewport, switches = APP_VIEWS[request.param]
    profile = tmp_path_factory.mktemp(f"edge-handoff-app-owned-{request.param}")
    site = Site()
    site.child_passive = True
    flows = {}

    with site.http:
        with browser_page(browser, profile, VIEWPORTS[viewport], viewport == "mobile",
                          switches=switches) as (devtools, session):
            devtools.call("Memory.setPressureNotificationsSuppressed", {"suppressed": True})
            tab = Tab(devtools, session, site.http.origin)
            for flow in APP_FLOWS:
                flows[flow] = run_app_flow(tab, site, flow)

    shutil.rmtree(profile, ignore_errors=True)
    return {"cached": not switches, "flows": flows}


@pytest.mark.parametrize("flow", list(APP_FLOWS))
def test_a_message_the_chat_took_has_one_owner_and_is_never_offered_back(app_owned, flow):
    run, cached = app_owned["flows"][flow], app_owned["cached"]
    fault, _, kind, steps, user_sends = APP_FLOWS[flow]
    draft, opened = run["draft"], run["opened"]
    session = dict(opened["session"])
    envelope_raw = session.get(HANDOFF_KEY)
    sent = json.loads(envelope_raw)
    assert sent["draft"] == draft and len(list(sent["draft"])) == len(list(draft))
    left = fault == "removal-and-recording-fail"
    # The chat looked at the envelope but could not remove it; nothing was sent automatically.
    assert opened["requests"] == [] and opened["path"] == "/app/"
    if left:
        # No way to show the homepage it took the message, so the chat took no copy.
        assert opened["value"] == "" and opened["notice"] == COPY_APP_LEFT
        assert LEDGER_KEY not in session or sent["nonce"] not in session[LEDGER_KEY]
    else:
        # The chat took it: its nonce is in the ledger, and its unsent copy is in the chat's box.
        assert opened["value"] == draft and opened["notice"] == COPY_APP_UNCHECKED
        assert sent["nonce"] in [entry["n"] for entry in ledger_entries(session[LEDGER_KEY])]
    documents = {opened["document"]: 0}
    last_chat = opened
    first_back = True

    for expected, state in zip(steps, run["steps"]):
        what = expected[0]
        label = (flow, what, state.get("value", "")[:40], state.get("status"), state.get("alert"),
                 state.get("notice"), state.get("requests"))
        assert state["step"] == what
        if what in ("back", "reload", "wait"):
            _, value, read_only, status, alert, envelope = expected
            assert state["path"] == "/", label
            if what == "back" and first_back:
                # Back really did (or, without the cache, really did not) restore the cached homepage.
                assert state["restored"] is cached, label
                first_back = False
            if value == "kept-draft":
                value = "draft" if state["restored"] else ""
            assert state["value"] == (draft if value == "draft" else ""), label
            assert state["readOnly"] is read_only, label
            assert state["status"] == status and state["alert"] == alert, label
            if not left:
                # Never a copy offered back, never "wasn't sent", for a message the chat took.
                assert not (state["value"] == draft and not state["readOnly"]), label
                assert "wasn’t sent" not in state["status"] + state["alert"], label
            stored = dict(state["session"]).get(HANDOFF_KEY)
            if envelope == "intact":
                # The chat's envelope is never withdrawn or overwritten by the homepage.
                assert stored == envelope_raw, label
            elif envelope == "absent":
                assert stored is None, label
        else:
            if what == "send":
                assert state["document"] == last_chat["document"], label
                assert state["requests"] == [{"message": draft, "history": [], "session_id": None}], label
            else:
                same = state["document"] == last_chat["document"]
                outcome = expected[1]
                if what == "app-reload":
                    assert not same, label
                if outcome == "owned" and same:
                    # The browser kept the chat's page: its unsent copy is still there, still unsent.
                    assert state["value"] == draft and state["notice"] == COPY_APP_UNCHECKED, label
                elif outcome == "left" and same:
                    assert state["value"] == "" and state["notice"] == COPY_APP_LEFT, label
                elif same:
                    assert state["requests"] == last_chat["requests"], label
                else:
                    # A fresh chat ignores an envelope the chat already took: nothing shown, nothing sent.
                    assert state["requests"] == [] and state["users"] == [], label
                    assert state["value"] == "" and state["notice"] == "", label
                if not same and not left:
                    stored = dict(state["session"]).get(HANDOFF_KEY)
                    assert stored == envelope_raw, label          # still there, still ignored
            documents[state["document"]] = len(state["requests"])
            last_chat = state
    # Never a duplicate: at most the one request the user started, from the chat's own copy.
    assert sum(documents.values()) == user_sends, documents


# --- the chat could not show it took the message: a read-only copy (browser) ------------------------------

# Chat-side storage faults, in /app/ documents only; the homepage's storage works throughout.
CHAT_FAULTS = {
    "silent-removal": storage_fault("removeItem", HANDOFF_KEY, ignore=True),
    # The envelope itself is read; the read that would confirm its removal throws.
    "confirmation-throws": storage_fault("getItem", HANDOFF_KEY, after=1),
    "ledger-write-throws": storage_fault("setItem", LEDGER_KEY),
    "ledger-write-ignored": storage_fault("setItem", LEDGER_KEY, ignore=True),
    "ledger-read-throws": storage_fault("getItem", LEDGER_KEY),
}
# The exact unverified combination: removal silently does nothing and the read that would confirm it
# throws, so the chat cannot tell whether the envelope is gone -- and the ledger cannot show it took it.
EXACT = ("silent-removal", "confirmation-throws")
MALFORMED_LEDGER = "{not a ledger"
UNVERIFIED_DRAFTS = {
    "plain": "  Which page has this one? — 日本語 \U0001F642  ",
    "error": "ERROR: which page has this one?",
    "emoji": "\U0001F642" * 4000,
}

# case: (chat faults, a malformed ledger stored first, what the chat does, draft, steps, requests the
# user starts). The chat "owns" the message (an ordinary unsent draft), "leaves" it to the homepage (a
# notice and no copy) or "withholds": keeps a read-only copy it never sends. Steps:
#   ("attempts",)              every way a user or a script can send, edit, clear or replace the chat's copy;
#   ("back", home, envelope)   the homepage: "clean"; "returned" (given back, editable); "held-unknown"
#                              (read-only); "kept-draft" (the draft it gave back, where the browser kept the
#                              page, else clean); "held-if-kept" / "returned-if-kept" (that, where the
#                              homepage that pressed Send was kept -- a fresh one does not know about it,
#                              else clean); and the envelope, "intact" (exactly as Send stored it) or "absent";
#   ("forward", fresh)         the chat: the very page it left, where the browser kept it, else a fresh page
#                              that shows "none" or the read-only copy "withheld" again;
#   ("app-reload", fresh)      a reload of the chat: a fresh page, which finds what is stored;
#   ("home-send",)             the homepage's own Send of the draft it gave back, to a chat whose storage works;
#   ("wait-in-chat",)          stay in the chat until 61.5 s after Send: past its own 8-second deadline and the
#                              60-second acceptance window.
UNVERIFIED_CASES = {
    # One fault alone: the chat can still show the homepage it took the message -- confirmed removal or a
    # verified ledger record -- so the chat owns it and the homepage is never offered it back.
    "silent-removal": (("silent-removal",), False, "owns", "plain", [
        ("back", "clean", "intact"), ("forward", "none"), ("back", "clean", "intact")], 0),
    "confirmation-throws": (("confirmation-throws",), False, "owns", "plain", [
        ("back", "clean", "absent"), ("forward", "none"), ("back", "clean", "absent")], 0),
    "ledger-write-throws": (("ledger-write-throws",), False, "owns", "plain", [
        ("back", "clean", "absent"), ("forward", "none"), ("back", "clean", "absent")], 0),
    "ledger-write-ignored": (("ledger-write-ignored",), False, "owns", "plain", [
        ("back", "clean", "absent"), ("forward", "none"), ("back", "clean", "absent")], 0),
    "ledger-read-throws": (("ledger-read-throws",), False, "owns", "plain", [
        ("back", "clean", "absent"), ("forward", "none"), ("back", "clean", "absent")], 0),
    # A malformed ledger: the homepage cannot tell who has it, so it only holds its copy read-only.
    "malformed-ledger": ((), True, "owns", "plain", [
        ("back", "held-if-kept", "absent"), ("forward", "none"), ("back", "held-if-kept", "absent")], 0),
    # Removal certainly failed and nothing was recorded: left to the homepage, which gives it back.
    "known-kept": (("silent-removal", "ledger-write-throws"), False, "leaves", "plain", [
        ("back", "returned", "absent"), ("forward", "none"), ("back", "kept-draft", "absent")], 0),
    # The exact combination: never actionable in the chat; the homepage withdraws it and is its one owner.
    "exact-repeated-back-and-forward": ((*EXACT, "ledger-write-throws"), False, "withholds", "plain", [
        ("attempts",), ("back", "returned", "absent"), ("forward", "none"), ("attempts",),
        ("back", "kept-draft", "absent"), ("forward", "none"), ("attempts",), ("back", "kept-draft", "absent")], 0),
    "exact-homepage-sends-it": ((*EXACT, "ledger-write-ignored"), False, "withholds", "plain", [
        ("attempts",), ("back", "returned", "absent"), ("home-send",), ("back", "clean", "absent"),
        ("forward", "none")], 1),
    "exact-homepage-sends-it-and-it-fails": ((*EXACT, "ledger-read-throws"), False, "withholds", "error", [
        ("attempts",), ("back", "returned", "absent"), ("home-send",), ("back", "clean", "absent"),
        ("forward", "none")], 1),
    "exact-emoji-4000": ((*EXACT, "ledger-write-throws"), False, "withholds", "emoji", [
        ("attempts",), ("back", "returned", "absent"), ("home-send",), ("back", "clean", "absent")], 1),
    # With a malformed ledger neither page can tell: both keep a read-only copy, and neither sends it.
    "exact-malformed-ledger": (EXACT, True, "withholds", "plain", [
        ("attempts",), ("back", "held-unknown", "intact"), ("forward", "withheld"), ("attempts",),
        ("back", "held-unknown", "intact")], 0),
    "exact-chat-reloaded": ((*EXACT, "ledger-write-ignored"), False, "withholds", "plain", [
        ("app-reload", "withheld"), ("attempts",), ("app-reload", "withheld"), ("back", "returned", "absent"),
        ("forward", "none")], 0),
    # A second copy of app.js in the same page reads the same authority: it re-enables and sends nothing.
    "exact-script-loaded-twice": ((*EXACT, "ledger-write-throws"), False, "withholds", "plain", [
        ("attempts",), ("back", "returned", "absent"), ("forward", "none"), ("attempts",),
        ("back", "kept-draft", "absent")], 0),
    # Neither the chat's deadline nor the acceptance window grants it; past the window the homepage --
    # kept or fresh -- still finds the envelope, withdraws it and gives it back.
    "exact-never-granted-by-time": ((*EXACT, "ledger-write-throws"), False, "withholds", "plain", [
        ("wait-in-chat",), ("attempts",), ("back", "returned", "absent"), ("forward", "none")], 0),
    # Its removal did take effect, unconfirmed: the chat puts the envelope back, so the homepage still
    # gives it back -- even where the chat's page does not survive Back.
    "removal-unconfirmed": (("confirmation-throws", "ledger-write-throws"), False, "withholds", "plain", [
        ("attempts",), ("back", "returned", "absent"), ("forward", "none"), ("attempts",),
        ("back", "kept-draft", "absent")], 0),
}
HOME_STATES = {"clean": ("", False, "", ""), "returned": ("draft", False, COPY_RETURNED, ""),
               "held-unknown": ("draft", True, "", COPY_HELD_UNKNOWN), "kept-draft": ("draft", False, "", ""),
               "held": ("draft", True, "", COPY_HELD), "held-too-old": ("draft", True, "", COPY_HELD_TOO_OLD)}
CHAT_KEYS = ("value", "notice", "readOnly", "sendDisabled", "eraseDisabled", "authority", "hintsDisabled",
             "requests", "users")

# From script, with the page's own locks lifted first -- the box read-only; Send, End & erase and the
# example prompts disabled -- so only the send authority is left to refuse; the locks are then put back.
# Send's click handler, Enter's keydown handler, the form's submit handler (requestSubmit and a submit
# event), an example prompt, and End & erase with its confirmation.
SCRIPTED_ATTEMPTS = """const input = document.getElementById('composer-input'), send = document.getElementById('send-btn'),
        form = document.getElementById('composer'), erase = document.getElementById('erase-btn'),
        dialog = document.getElementById('erase-dialog'), hints = [...document.querySelectorAll('.hint')];
  const locks = [input.readOnly, send.disabled, erase.disabled, hints.map(h => h.disabled)];
  input.readOnly = false; send.disabled = false; erase.disabled = false;
  hints.forEach(h => { h.disabled = false; });
  send.click();
  input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', bubbles: true, cancelable: true}));
  form.requestSubmit();
  form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
  if (hints.length) hints[0].click();
  erase.click();
  if (dialog.open) document.getElementById('erase-dialog-confirm').click();
  [input.readOnly, send.disabled, erase.disabled] = locks;
  hints.forEach((h, i) => { h.disabled = locks[3][i]; });
  await new Promise(resolve => setTimeout(resolve, 500));
  return true;"""


def attempt_everything(tab: Tab, mobile: bool):
    """Trusted input first -- typing, Enter, Send (tapped, too, on a phone), End & erase and an example
    prompt -- then every route again from script."""
    tab.click("#composer-input")
    tab.type(" (edited)")
    tab.enter()
    if mobile:
        tab.tap("#send-btn")
    tab.click("#send-btn")
    tab.click("#erase-btn")
    # The example prompts leave the page with its first message -- which only a broken lock would send.
    if tab.eval("return !!document.querySelector('.hint');"):
        tab.click(".hint")
    time.sleep(0.3)
    tab.eval(SCRIPTED_ATTEMPTS)


def collected_cases(request, test: str, fixture: str, cases: dict) -> list[str]:
    """The cases of `test` this session collected for this fixture's parameter, in order -- every case in a
    full run; a narrowed one (-k) need not wait for the others."""
    view = request.param
    chosen = {item.callspec.params["case"] for item in request.session.items
              if getattr(item, "originalname", None) == test and hasattr(item, "callspec")
              and item.callspec.params.get(fixture) == view}
    return [case for case in cases if case in chosen] or list(cases)


def run_unverified(tab: Tab, site: Site, case: str, mobile: bool) -> dict:
    faults, malformed, _, kind, steps, _ = UNVERIFIED_CASES[case]
    typed = UNVERIFIED_DRAFTS[kind]
    run: dict = {"draft": typed.strip(), "steps": []}
    prelude = tab.on_new_documents(homepage_prelude([]))
    try:
        # A clean tab -- or one holding a malformed ledger -- so no earlier case's envelope is about.
        stored(tab, {LEDGER_KEY: json.dumps(MALFORMED_LEDGER)} if malformed else {})
        site.http.cacheable = {"/", "/app/"}
        site.app_prelude = f"<script>{''.join(CHAT_FAULTS[f] for f in faults)}</script>" if faults else ""
        site.app_script_twice = case == "exact-script-loaded-twice"
        tab.open("/", HOME_READY)
        tab.click("#home-composer-input")
        tab.type(typed)
        started = time.monotonic()
        tab.click('[data-testid="button-home-send"]')
        assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY}", timeout=10)
        time.sleep(0.6)
        run["opened"] = tab.chat()
        for step in steps:
            what = step[0]
            if what == "attempts":
                if tab.chat()["value"]:
                    attempt_everything(tab, mobile)
                state = tab.chat()
            elif what == "back":
                tab.history(-1, "/")
                assert tab.wait(HOME_READY)
                state = tab.home()
            elif what == "forward":
                tab.history(1, "/app/")
                assert tab.wait(CHAT_READY)
                time.sleep(0.6)
                state = tab.chat()
            elif what == "app-reload":
                tab.dt.call("Page.reload", {}, tab.session)
                assert tab.wait(f"document.readyState === 'complete' && {CHAT_READY}")
                time.sleep(0.6)
                state = tab.chat()
            elif what == "home-send":
                site.app_prelude = ""                      # the chat's storage works now
                tab.click('[data-testid="button-home-send"]')
                assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY} && {REPLIED}", timeout=15)
                time.sleep(0.4)
                state = tab.chat()
            else:
                time.sleep(max(0.0, started + 61.5 - time.monotonic()))
                state = tab.chat()
            run["steps"].append({"step": what, **state})
    finally:
        site.app_prelude = ""
        site.app_script_twice = False
        tab.forget(prelude)
    return run


@pytest.fixture(scope="module", params=list(APP_VIEWS))
def unverified(request, tmp_path_factory):
    browser = require_browser_and_build()
    viewport, switches = APP_VIEWS[request.param]
    profile = tmp_path_factory.mktemp(f"edge-handoff-unverified-{request.param}")
    site = Site()
    site.child_passive = True
    cases = {}

    with site.http:
        with browser_page(browser, profile, VIEWPORTS[viewport], viewport == "mobile",
                          switches=switches) as (devtools, session):
            devtools.call("Memory.setPressureNotificationsSuppressed", {"suppressed": True})
            tab = Tab(devtools, session, site.http.origin)
            for case in collected_cases(request, "test_a_message_only_one_page_can_send_and_none_discards",
                                        "unverified", UNVERIFIED_CASES):
                try:
                    cases[case] = run_unverified(tab, site, case, viewport == "mobile")
                except Exception as error:     # that case's own test reports it; the others still run
                    cases[case] = {"error": f"{type(error).__name__}: {error}"}

    shutil.rmtree(profile, ignore_errors=True)
    return {"cached": not switches, "cases": cases}


@pytest.mark.parametrize("case", list(UNVERIFIED_CASES))
def test_a_message_only_one_page_can_send_and_none_discards(unverified, case):
    run, cached = unverified["cases"][case], unverified["cached"]
    assert "error" not in run, run.get("error")
    _, malformed, outcome, _, steps, user_sends = UNVERIFIED_CASES[case]
    draft, opened = run["draft"], run["opened"]
    session = dict(opened["session"])
    envelope_raw, ledger_raw = session.get(HANDOFF_KEY), session.get(LEDGER_KEY)
    assert opened["path"] == "/app/" and opened["requests"] == []
    assert opened["appScripts"] == (2 if case == "exact-script-loaded-twice" else 1)
    if outcome == "owns":
        placed_unsent(opened, draft, COPY_APP_UNCHECKED)
    elif outcome == "leaves":
        assert opened["value"] == "" and opened["notice"] == COPY_APP_LEFT and opened["authority"] is None
        assert json.loads(envelope_raw)["draft"] == draft
    else:
        # Kept whole, for copying -- read-only, Send, End & erase and the example prompts disabled.
        withheld(opened, draft)
    if outcome != "owns":
        # Nothing the homepage can see shows the chat took it: no ledger record.
        assert ledger_raw == (MALFORMED_LEDGER if malformed else None) or ledger_entries(ledger_raw) == []

    documents = {opened["document"]: len(opened["requests"])}
    chat, home, sent = opened, None, False
    losses = PromptLosses(draft, cached, opened)
    first_back = True
    for expected, state in zip(steps, run["steps"]):
        what = expected[0]
        assert state["step"] == what
        label = (case, what, state.get("value", "")[:30], state.get("status"), state.get("alert"),
                 state.get("notice"), state.get("authority"), state.get("requests"))
        if what == "back":
            _, expect, envelope = expected
            assert state["path"] == "/", label
            if first_back:
                # A genuine restore of the homepage that pressed Send -- or, without the cache, a fresh one.
                assert state["restored"] is cached, label
                first_back = False
            if expect in ("kept-draft", "held-if-kept"):
                expect = {"kept-draft": "kept-draft", "held-if-kept": "held-unknown"}[expect] \
                    if state["restored"] else "clean"
            value, read_only, status, alert = HOME_STATES[expect]
            assert state["value"] == (draft if value == "draft" else ""), label
            assert state["readOnly"] is read_only and state["sendDisabled"] is (read_only or value == ""), label
            assert state["status"] == status and state["alert"] == alert, label
            stored_envelope = dict(state["session"]).get(HANDOFF_KEY)
            if envelope == "intact":
                assert stored_envelope is not None and stored_envelope == envelope_raw, label
            else:
                assert stored_envelope is None, label
            if outcome == "owns":
                assert "wasn’t sent" not in state["status"] + state["alert"], label
            # Never two copies anyone can send: a kept chat page with an editable copy and this one.
            chat_actionable = chat["value"] == draft and not chat["readOnly"] and chat["authority"] is None
            home_actionable = state["value"] == draft and not state["readOnly"]
            assert not (cached and chat_actionable and home_actionable), label
            home = state
        else:
            assert state["path"] == "/app/", label
            if what == "attempts":
                # Nothing a user or a script did changed, cleared or sent the chat's copy.
                assert state["document"] == chat["document"], label
                for key in CHAT_KEYS:
                    assert state[key] == chat[key], (key, label)
                if chat["authority"] == "withheld":
                    withheld(state, draft)
            elif what == "home-send":
                # Only the homepage's own later Send, from the copy it withdrew, produces a request.
                assert state["document"] not in documents, label
                assert state["requests"] == [{"message": draft, "history": [], "session_id": None}], label
                assert state["users"] == [draft], label
                assert len(list(state["requests"][0]["message"])) == len(list(draft)), label
                sent = True
            elif what == "wait-in-chat":
                assert state["document"] == chat["document"], label
                withheld(state, draft)
            else:
                same = state["document"] == chat["document"]
                # Forward genuinely restores the kept chat page (without the cache, a fresh one); a reload
                # is always a fresh page.
                assert same is (cached and what == "forward"), label
                if same:
                    # The kept page is exactly as it was left: a withheld copy is not reactivated.
                    for key in CHAT_KEYS:
                        assert state[key] == chat[key], (key, label)
                    if chat["authority"] == "withheld":
                        withheld(state, draft)
                elif expected[1] == "withheld":
                    withheld(state, draft)
                else:
                    assert state["value"] == "" and state["notice"] == "" and state["authority"] is None, label
                    assert state["requests"] == [] and state["users"] == [], label
            documents[state["document"]] = len(state["requests"])
            chat = state
        losses.check(what, state, chat, home, sent, label)
    # Never more than one request for the message: only the one the user started from the homepage.
    assert sum(documents.values()) == user_sends, documents


class PromptLosses:
    """No prompt loss: after every step the exact message is still somewhere -- the page shown, a page the
    browser keeps (with the cache), or the envelope in storage, which any later showing finds -- or it was
    sent. The one exception is the user's own: leaving, without the cache, a page where the message was
    theirs to edit and send, as any typed draft would be left."""

    def __init__(self, draft: str, cached: bool, opened: dict):
        self.draft, self.cached, self.previous, self.abandoned = draft, cached, opened, False

    def check(self, what: str, state: dict, chat: dict, home: dict | None, sent: bool, label):
        draft, previous = self.draft, self.previous
        if (not self.cached and what in ("back", "forward", "reload", "app-reload")
                and previous["value"] == draft and not previous["readOnly"]):
            self.abandoned = True
        stored_now = dict(state["session"]).get(HANDOFF_KEY)
        in_storage = stored_now is not None and json.loads(stored_now).get("draft") == draft
        kept = self.cached and (chat["value"] == draft or (home is not None and home["value"] == draft))
        assert sent or self.abandoned or state["value"] == draft or kept or in_storage, ("lost",) + label
        self.previous = state


# --- an envelope past the chat's acceptance window, without the back/forward cache (browser) --------------

# A newer message the chat took after this one: its ledger entry expires after this envelope's would.
NEWER_ENTRY = ledger_js([(nonce(0x900), "Date.now() + 90000")])


def age_envelope(tab: Tab, ms: int) -> dict:
    """Ages the envelope stored in this tab as if `ms` more had passed since Send: its createdAt moves back
    and nothing else changes. Both pages compute an envelope's age from createdAt alone, so this reaches the
    very condition a real wait does, and production timing is untouched. It reads and writes by property
    access, so the page's injected storage faults are neither triggered nor removed: they stay active for
    everything the pages do next. Returns the stored value before and after."""
    key = json.dumps(HANDOFF_KEY)
    return tab.eval(f"const before = sessionStorage[{key}]; const envelope = JSON.parse(before); "
                    f"envelope.createdAt -= {ms}; sessionStorage[{key}] = JSON.stringify(envelope); "
                    f"return {{before, after: sessionStorage[{key}]}};")


# case: (chat faults, storage set before Send, homepage faults, seconds app.js is held, draft, what the chat
# first does with it -- "withholds" (a read-only copy), "owns" (an unsent draft it recorded) or "pending"
# (app.js has not run) -- steps, requests the user starts). Steps:
#   ("age", seconds)                 the stored envelope, that much older (age_envelope): no timer is under
#                                    test here, only what each page does with an envelope of that age;
#   ("back", home, envelope)         Back to a fresh homepage: what it shows (HOME_STATES) and the envelope --
#   ("reload", home, envelope)       or reload it -- "intact" (exactly as stored), "marker" or "absent";
#   ("forward", "none", envelope[, "working"])  Forward to a fresh chat -- with its faults, or "working" storage
#                                    -- which shows and sends nothing;
#   ("home-send",)                   the homepage's own Send of the draft it gave back, to a chat whose storage works.
STALE_CASES = {
    # The ledger shows the chat never took it: the homepage withdraws it and is its one owner.
    "returned-and-sent-from-the-homepage": ((*EXACT, "ledger-write-throws"), {}, [], 0.0, "plain", "withholds", [
        ("age", 61.5), ("back", "returned", "absent"), ("home-send",), ("back", "clean", "absent"),
        ("forward", "none", "absent")], 1),
    "emoji-4000-returned-exactly": ((*EXACT, "ledger-write-ignored"), {}, [], 0.0, "emoji", "withholds", [
        ("age", 61.5), ("back", "returned", "absent"), ("home-send",)], 1),
    # The chat's removal took effect, unconfirmed: it put the envelope back, so it is still given back.
    "chat-removal-took-effect": (("confirmation-throws", "ledger-write-throws"), {}, [], 0.0, "plain",
                                 "withholds", [
        ("age", 61.5), ("back", "returned", "absent"), ("forward", "none", "absent")], 0),
    # Who has it cannot be told: kept read-only, never deleted -- through reloads and repeated Back/Forward,
    # with the chat leaving alone an envelope too old for it to take.
    "ledger-malformed": (EXACT, {LEDGER_KEY: json.dumps(MALFORMED_LEDGER)}, [], 0.0, "plain", "withholds", [
        ("age", 61.5), ("back", "held-unknown", "intact"), ("forward", "none", "intact"),
        ("back", "held-unknown", "intact"), ("reload", "held-unknown", "intact"), ("forward", "none", "intact"),
        ("back", "held-unknown", "intact")], 0),
    "ledger-unreadable-on-the-homepage": ((*EXACT, "ledger-write-throws"), {}, [("getItem", 0, False, LEDGER_KEY)],
                                          0.0, "plain", "withholds", [
        ("age", 61.5), ("back", "held-unknown", "intact"), ("reload", "held-unknown", "intact"),
        ("forward", "none", "intact"), ("back", "held-unknown", "intact")], 0),
    # A newer entry that could have pruned this one's: harmless before this one's own entry would expire...
    "newer-entry-within-retention": ((*EXACT, "ledger-write-throws"), {LEDGER_KEY: NEWER_ENTRY}, [], 0.0, "plain",
                                     "withholds", [("age", 61.5), ("back", "returned", "absent")], 0),
    # ...but past that, the ledger no longer shows the chat never took it: kept read-only, saying why.
    "newer-entry-past-retention": ((*EXACT, "ledger-write-throws"), {LEDGER_KEY: NEWER_ENTRY}, [], 0.0, "plain",
                                   "withholds", [
        ("age", 66.0), ("back", "held-too-old", "intact"), ("reload", "held-too-old", "intact"),
        ("forward", "none", "intact"), ("back", "held-too-old", "intact")], 0),
    # The homepage cannot withdraw it, so it holds it read-only; a chat whose storage works opens later,
    # finds an envelope too old for it to take, and leaves it exactly where it is -- so Back still holds it.
    "a-working-chat-leaves-it": ((*EXACT, "ledger-write-throws"), {}, EARLY_FAULTS["removal-and-overwrite-fail"],
                                 0.0, "plain", "withholds", [
        ("age", 61.5), ("back", "held", "intact"), ("forward", "none", "intact", "working"),
        ("back", "held", "intact"), ("reload", "held", "intact")], 0),
    # Controls. A consumed nonce still wins: the chat's, so the homepage is clean and leaves its envelope.
    "consumed-nonce": (("silent-removal",), {}, [], 0.0, "plain", "owns", [
        ("age", 61.5), ("back", "clean", "intact"), ("forward", "none", "intact"), ("back", "clean", "intact")], 0),
    # A withdrawn marker keeps its behavior: it is the homepage's own, holds no message, and is discarded.
    "withdrawn-marker": ((), {}, [("removeItem", 0, True)], 6.0, "plain", "pending", [
        ("back", "returned", "marker"), ("reload", "clean", "marker"), ("forward", "none", "absent")], 0),
}
STALE_VIEWS = ("desktop", "mobile")


def run_stale(tab: Tab, site: Site, case: str) -> dict:
    faults, seed, home_faults, held_for, kind, _, steps, _ = STALE_CASES[case]
    typed = UNVERIFIED_DRAFTS[kind]
    run: dict = {"draft": typed.strip(), "steps": []}
    prelude = tab.on_new_documents(homepage_prelude(home_faults))
    try:
        stored(tab, seed)                                  # a clean tab, with only what the case stores
        # The chat is never served from the HTTP cache (Back and Forward would reuse a cached page as it was):
        # every chat page is fetched afresh, with exactly the faults the case gives it at that moment.
        site.http.cacheable = {"/"}
        # The faults mark the page they are installed in, so a test can see which chat ran with them.
        site.app_prelude = (f"<script>window.__STALE_FAULTS__ = true; {''.join(CHAT_FAULTS[f] for f in faults)}"
                            "</script>") if faults else ""
        site.script_delay = held_for
        tab.open("/", HOME_READY)
        tab.click("#home-composer-input")
        tab.type(typed)
        tab.click('[data-testid="button-home-send"]')
        assert tab.wait(OPENED if held_for else f"location.pathname === '/app/' && {CHAT_READY}", timeout=10)
        time.sleep(0.6 if not held_for else 0.0)
        run["opened"] = tab.chat()
        for step in steps:
            what = step[0]
            if what == "age":
                run["aged"] = age_envelope(tab, round(step[1] * 1000))
                state = tab.chat() if run["steps"] == [] or run["steps"][-1]["path"] == "/app/" else tab.home()
            elif what == "back":
                tab.history(-1, "/")
                assert tab.wait(HOME_READY)
                state = tab.home()
            elif what == "reload":
                tab.dt.call("Page.reload", {}, tab.session)
                assert tab.wait(f"document.readyState === 'complete' && {HOME_READY}")
                time.sleep(0.4)
                state = tab.home()
            elif what == "forward":
                site.script_delay = 0.0
                if step[3:] == ("working",):
                    site.app_prelude = ""                  # the chat's storage works from now on
                tab.history(1, "/app/")
                assert tab.wait(CHAT_READY)
                time.sleep(0.6)
                state = tab.chat()
                # Whether the faults were served for this chat, and whether this page really runs with them.
                state["faultsServed"] = site.app_prelude != ""
                state["faulted"] = tab.eval("return window.__STALE_FAULTS__ === true;")
            else:
                site.app_prelude, site.script_delay = "", 0.0      # the chat's storage works now
                tab.click('[data-testid="button-home-send"]')
                assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY} && {REPLIED}", timeout=15)
                time.sleep(0.4)
                state = tab.chat()
            run["steps"].append({"step": what, **state})
    finally:
        site.app_prelude, site.script_delay = "", 0.0
        tab.forget(prelude)
    return run


@pytest.fixture(scope="module", params=STALE_VIEWS)
def stale(request, tmp_path_factory):
    """Every case in its own Edge, profile and loopback site, so that nothing one case does -- not even a
    lost browser connection -- can reach the next."""
    browser = require_browser_and_build()
    viewport = request.param
    cases = {}
    for case in collected_cases(request, "test_a_message_past_the_chat_window_is_never_discarded", "stale",
                                STALE_CASES):
        profile = tmp_path_factory.mktemp(f"edge-handoff-stale-{viewport}")
        site = Site()
        site.child_passive = True
        try:
            with site.http:
                with browser_page(browser, profile, VIEWPORTS[viewport], viewport == "mobile",
                                  switches=("--disable-back-forward-cache",)) as (devtools, session):
                    cases[case] = run_stale(Tab(devtools, session, site.http.origin), site, case)
        except Exception as error:     # that case's own test reports it
            cases[case] = {"error": f"{type(error).__name__}: {error}"}
        finally:
            shutil.rmtree(profile, ignore_errors=True)
    return cases


@pytest.mark.parametrize("case", list(STALE_CASES))
def test_a_message_past_the_chat_window_is_never_discarded(stale, case):
    run = stale[case]
    assert "error" not in run, run.get("error")
    _, _, _, _, _, outcome, steps, user_sends = STALE_CASES[case]
    draft, opened = run["draft"], run["opened"]
    envelope_raw = dict(opened["session"]).get(HANDOFF_KEY)
    assert opened["path"] == "/app/" and opened["requests"] == []
    if outcome == "withholds":
        withheld(opened, draft)
    elif outcome == "owns":
        placed_unsent(opened, draft, COPY_APP_UNCHECKED)
    # The envelope Send stored is still there, exactly: the chat removed nothing it could not take for good.
    sent_envelope = json.loads(envelope_raw)
    assert sent_envelope["draft"] == draft and len(list(sent_envelope["draft"])) == len(list(draft))

    documents = {opened["document"]: len(opened["requests"])}
    chat, home, sent = opened, None, False
    losses = PromptLosses(draft, False, opened)
    for expected, state in zip(steps, run["steps"]):
        what = expected[0]
        assert state["step"] == what
        label = (case, what, state.get("value", "")[:30], state.get("status"), state.get("alert"),
                 state.get("notice"), state.get("authority"), state.get("requests"))
        if what == "age":
            # Only its time moved: the version, nonce and exact message -- and the key order -- are unchanged.
            before, after = run["aged"]["before"], run["aged"]["after"]
            assert before == envelope_raw, label
            old, new = json.loads(before), json.loads(after)
            assert list(new) == list(old) == ["v", "draft", "nonce", "createdAt"], label
            assert new["v"] == old["v"] == 1 and new["nonce"] == old["nonce"] == sent_envelope["nonce"], label
            assert new["draft"] == old["draft"] == draft, label
            assert new["createdAt"] == old["createdAt"] - round(expected[1] * 1000), label
            envelope_raw = after
        if state["path"] == "/":
            if what == "back":
                # Without the cache, Back always opens a fresh homepage.
                assert state["restored"] is False, label
            if what in ("back", "reload"):
                _, expect, envelope = expected
                value, read_only, status, alert = HOME_STATES[expect]
                assert state["value"] == (draft if value == "draft" else ""), label
                assert state["readOnly"] is read_only and state["sendDisabled"] is (read_only or value == ""), label
                assert state["status"] == status and state["alert"] == alert, label
                stored_envelope = dict(state["session"]).get(HANDOFF_KEY)
                if envelope == "intact":
                    assert stored_envelope == envelope_raw, label
                elif envelope == "marker":
                    assert withdrawn_marker(stored_envelope, sent_envelope["nonce"]), label
                else:
                    assert stored_envelope is None, label
                if outcome == "owns":
                    # The chat took it: no "wasn't sent", and no copy offered back.
                    assert "wasn’t sent" not in state["status"] + state["alert"] and state["value"] == "", label
            home = state
        else:
            if what == "age":
                # The page shown is untouched by the ageing.
                assert state["document"] == chat["document"], label
                for key in CHAT_KEYS:
                    assert state[key] == chat[key], (key, label)
            elif what == "home-send":
                # Only the homepage's own Send, of the exact draft it gave back, produces a request.
                assert state["document"] not in documents, label
                assert state["requests"] == [{"message": draft, "history": [], "session_id": None}], label
                assert len(list(state["requests"][0]["message"])) == len(list(draft)), label
                sent = True
            else:
                # A fresh chat shows and sends nothing -- and leaves an envelope too old for it to take.
                assert state["document"] not in documents, label
                assert state["value"] == "" and state["notice"] == "" and state["authority"] is None, label
                assert state["requests"] == [] and state["users"] == [], label
                stored_envelope = dict(state["session"]).get(HANDOFF_KEY)
                assert stored_envelope == (envelope_raw if expected[2] == "intact" else None), label
                # The chat runs with exactly the faults served for it -- never a cached page's -- and one said
                # to work has none.
                assert state["faulted"] is state["faultsServed"], label
                if expected[3:] == ("working",):
                    assert state["faulted"] is False, label
            documents[state["document"]] = len(state["requests"])
            chat = state
        losses.check(what, state, chat, home, sent, label)
    # Never more than one request for the message.
    assert sum(documents.values()) == user_sends, documents


# --- two homepage documents and one handoff (browser) ------------------------------------------------------

def two_homes_prelude(faults) -> str:
    """For every homepage document: record each showing and give it a test-side identity (window.__doc). In the
    second homepage (/?second), and only there, install `faults` from the start."""
    wrappers = []
    for method, allow, ignore, *key in faults:
        effect = "return undefined;" if ignore else "throw new DOMException('denied', 'SecurityError');"
        wrappers.append(
            f"(function () {{ const real = Storage.prototype.{method}; let n = 0; "
            f"Storage.prototype.{method} = function (key) {{ if (key === {json.dumps((key or [HANDOFF_KEY])[0])} "
            f"&& ++n > {allow}) {{ {effect} }} return real.apply(this, arguments); }}; }})();")
    return ("(function () { if (location.pathname !== '/') return; "
            "window.__doc = Math.random().toString(36).slice(2) + Date.now().toString(36); "
            "window.__shows = []; addEventListener('pageshow', e => window.__shows.push(e.persisted)); "
            "if (location.search !== '?second') return; " + " ".join(wrappers) + " })();")


def traverse_to(tab: Tab, search: str) -> bool:
    """Back or Forward straight to the history entry of the homepage with this query ('' or '?second') -- never
    through the chat's entry between them -- as a real history traversal. False if no such entry is left (a Send
    from the other homepage replaces the entries after it)."""
    found = tab.eval("const target = navigation.entries().find(e => { const u = new URL(e.url); "
                     f"return u.pathname === '/' && u.search === {json.dumps(search)}; }}); "
                     "if (!target) return false; navigation.traverseTo(target.key); return true;")
    if not found:
        return False
    assert tab.wait(f"location.pathname === '/' && location.search === {json.dumps(search)} && "
                    f"document.readyState === 'complete' && {HOME_READY}")
    time.sleep(0.5)
    return True


HOME_STATES["held-elsewhere"] = ("draft", True, "", (
    "Another Kalillac page in this tab took this message back. So that it can’t be sent twice, it stays here "
    "read-only. You can still select and copy it."))

# flow: (faults in the second homepage, draft, steps, requests). The first homepage H1 sends the message A while
# the chat's script is held, so nothing takes it; then a fresh homepage H2 opens. Steps:
#   ("h2", home, envelope)       open H2, which finds the stored envelope;
#   ("to-h1" | "to-h2", home, envelope)  Back or Forward to that homepage, restored by the back/forward cache;
#   ("reload", home, envelope)   reload the homepage shown: a new document, with a new identity;
#   ("send", sent)               press Send on the homepage shown: whether it could send (then the chat answers).
# Envelope: "envelope" (A's, as Send stored it), "marker" (H2's withdrawn marker), "absent" or None (not checked).
TWO_HOMES = {
    # H2 withdraws it with its own marker and is its one owner: H1, finding H2's marker, keeps a read-only copy.
    "removal-ignored": ([("removeItem", 0, True)], "plain", [
        ("h2", "returned", "marker"), ("to-h1", "held-elsewhere", "marker"), ("send", False),
        ("to-h2", "kept-draft", "marker"), ("to-h1", "held-elsewhere", "marker"), ("to-h2", "kept-draft", "marker"),
        ("send", True), ("to-h1", "clean", None), ("send", False)], 1),
    "removal-throws": ([("removeItem", 0, False)], "error", [
        ("h2", "returned", "marker"), ("to-h1", "held-elsewhere", "marker"), ("send", False),
        ("to-h2", "kept-draft", "marker"), ("send", True), ("to-h1", "clean", None)], 1),
    # Healthy removal leaves no marker: H2 is the one owner, and H1 is clean.
    "healthy-removal": ([], "emoji", [
        ("h2", "returned", "absent"), ("to-h1", "clean", "absent"), ("send", False),
        ("to-h2", "kept-draft", "absent"), ("send", True)], 1),
    # H2 can neither remove nor mark it, so holds it read-only; H1 then withdraws it and owns it -- sending first.
    "marker-fails": ([("removeItem", 0, True), ("setItem", 0, True)], "plain", [
        ("h2", "held", "envelope"), ("to-h1", "returned", "absent"), ("to-h2", "clean", "absent"), ("send", False),
        ("to-h1", "kept-draft", "absent"), ("send", True)], 1),
    # A reload is a new document with a new identity: it never takes over another document's withdrawal.
    "reloads": ([("removeItem", 0, True)], "plain", [
        ("h2", "returned", "marker"), ("to-h1", "held-elsewhere", "marker"), ("reload", "clean", "absent"),
        ("to-h2", "kept-draft", "absent"), ("reload", "clean", "absent")], 0),
}


def run_two_homes(tab: Tab, site: Site, flow: str) -> dict:
    faults, kind, steps, _ = TWO_HOMES[flow]
    typed = UNVERIFIED_DRAFTS[kind]
    run: dict = {"draft": typed.strip(), "steps": []}
    prelude = tab.on_new_documents(two_homes_prelude(faults))
    try:
        stored(tab, {})                                    # a clean tab
        site.http.cacheable = {"/"}
        site.script_delay = 30.0                           # H1's chat opens, but nothing takes the envelope
        tab.open("/", HOME_READY)
        tab.click("#home-composer-input")
        tab.type(typed)
        run["h1"] = tab.home()                             # H1, the document that sends
        tab.click('[data-testid="button-home-send"]')
        assert tab.wait(OPENED, timeout=8)
        run["opened"] = tab.chat()
        for step in steps:
            what = step[0]
            if what == "h2":
                tab.dt.call("Page.navigate", {"url": tab.origin + "/?second"}, tab.session)
                assert tab.wait(f"location.search === '?second' && document.readyState === 'complete' && {HOME_READY}")
                time.sleep(0.5)
                state = tab.home()
            elif what in ("to-h1", "to-h2"):
                if not traverse_to(tab, "" if what == "to-h1" else "?second"):
                    run["stopped"] = f"{what}: that homepage's history entry is gone"
                    break
                state = tab.home()
            elif what == "reload":
                tab.dt.call("Page.reload", {}, tab.session)
                assert tab.wait(f"document.readyState === 'complete' && {HOME_READY}")
                time.sleep(0.5)
                state = tab.home()
            else:
                before = tab.home()
                site.script_delay = 0.0
                if not before["sendDisabled"]:
                    tab.click('[data-testid="button-home-send"]')
                    assert tab.wait(f"location.pathname === '/app/' && {CHAT_READY} && {REPLIED}", timeout=15)
                    time.sleep(0.4)
                    state = {**tab.chat(), "before": before}
                else:
                    tab.click('[data-testid="button-home-send"]')      # a disabled Send: nothing may happen
                    time.sleep(1.0)
                    state = {**tab.home(), "before": before}
            run["steps"].append({"step": what, **state})
    finally:
        site.script_delay = 0.0
        tab.forget(prelude)
    return run


@pytest.fixture(scope="module", params=["desktop", "mobile"])
def two_homes(request, tmp_path_factory):
    browser = require_browser_and_build()
    viewport = request.param
    flows = {}
    for flow in TWO_HOMES:
        profile = tmp_path_factory.mktemp(f"edge-handoff-two-homes-{viewport}")
        site = Site()
        site.child_passive = True
        try:
            with site.http:
                with browser_page(browser, profile, VIEWPORTS[viewport], viewport == "mobile") as (devtools, session):
                    # Back and Forward here must use the back/forward cache: keep memory pressure from emptying it.
                    devtools.call("Memory.setPressureNotificationsSuppressed", {"suppressed": True})
                    flows[flow] = run_two_homes(Tab(devtools, session, site.http.origin), site, flow)
        except Exception as error:     # that flow's own test reports it
            flows[flow] = {"error": f"{type(error).__name__}: {error}"}
        finally:
            shutil.rmtree(profile, ignore_errors=True)
    return flows


@pytest.mark.parametrize("flow", list(TWO_HOMES))
def test_only_the_homepage_that_withdrew_the_message_may_send_it(two_homes, flow):
    run = two_homes[flow]
    assert "error" not in run, run.get("error")
    _, _, steps, sends = TWO_HOMES[flow]
    draft, opened = run["draft"], run["opened"]
    # H1's Send stored the exact message; the chat's page opened, but its script never took the envelope.
    envelope_raw = dict(opened["session"])[HANDOFF_KEY]
    sent_envelope = json.loads(envelope_raw)
    assert sent_envelope["draft"] == draft and len(list(sent_envelope["draft"])) == len(list(draft))
    assert opened["requests"] == []

    page = {"h1": run["h1"]["doc"], "h2": None}   # each homepage entry's current document identity
    assert page["h1"] is not None
    copies: dict[str, bool] = {}               # live homepage document -> holds an editable copy of the message
    holds: dict[str, bool] = {}                # live homepage document -> holds the message at all
    requests, marker_by, abandoned, current = [], None, False, "h1"
    for expected, state in zip(steps, run["steps"]):
        what = expected[0]
        assert state["step"] == what
        label = (flow, what, current, state.get("value", "")[:30], state.get("status"), state.get("alert"),
                 state.get("readOnly"), state.get("requests"))
        if what == "send":
            before = state["before"]
            assert before["sendDisabled"] is (not expected[1]), label
            if expected[1]:
                # Only an editable, owning copy sends -- the exact message, once.
                assert before["value"] == draft and before["readOnly"] is False, label
                assert state["requests"] == [{"message": draft, "history": [], "session_id": None}], label
                assert len(list(state["requests"][0]["message"])) == len(list(draft)), label
                requests += state["requests"]
                copies[page[current]] = holds[page[current]] = False      # it left the box when it was sent
            else:
                assert state["path"] == "/" and state["doc"] == before["doc"], label
                assert state["value"] == before["value"] and state["readOnly"] is before["readOnly"], label
            continue

        _, expect, envelope = expected
        assert state["path"] == "/", label
        if what in ("h2", "to-h2"):
            current = "h2"
        elif what == "to-h1":
            current = "h1"
        if what == "reload":
            # A new document, with a new identity, in place of the one reloaded.
            assert state["doc"] != page[current] and state["restored"] is False, label
            old = copies.pop(page[current], False)
            holds.pop(page[current], None)
            abandoned = abandoned or old
        elif what == "h2":
            assert state["doc"] != page["h1"] and state["restored"] is False, label
        else:
            # Genuine Back/Forward: the very document left there, restored by the back/forward cache.
            assert state["restored"] is True and state["doc"] == page[current], label
        page[current] = state["doc"]
        value, read_only, status, alert = HOME_STATES[expect]
        assert state["value"] == (draft if value == "draft" else ""), label
        assert state["readOnly"] is read_only and state["sendDisabled"] is (read_only or value == ""), label
        assert state["status"] == status and state["alert"] == alert, label
        stored_envelope = dict(state["session"]).get(HANDOFF_KEY)
        if envelope == "envelope":
            assert stored_envelope == envelope_raw, label
        elif envelope == "marker":
            by = withdrawn_marker(stored_envelope, sent_envelope["nonce"])
            assert by is not None and draft[:8] not in stored_envelope, label
            # One document's marker, written once and never rewritten by another.
            assert marker_by in (None, by), label
            marker_by = by
        elif envelope == "absent":
            assert stored_envelope is None, label
        copies[state["doc"]] = state["value"] == draft and not state["readOnly"]
        holds[state["doc"]] = state["value"] == draft
        # At most one homepage document anywhere in this tab's history holds a copy anyone can send.
        assert sum(copies.values()) <= 1, (copies, label)
        # No prompt loss: a live document or storage still has it, or it was sent -- unless the user reloaded
        # away an editable copy of their own.
        in_storage = stored_envelope is not None and json.loads(stored_envelope).get("draft") == draft
        assert requests or abandoned or in_storage or any(holds.values()), label
    # Every step ran: no unexpected Send replaced a homepage's history entry.
    assert "stopped" not in run and len(run["steps"]) == len(steps), run.get("stopped")
    # Never more than one request for the message, and only the one the flow expects.
    assert len(requests) == sends, requests
