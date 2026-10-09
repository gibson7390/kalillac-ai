"""Homepage site and its embedded chat: shared browser harness, the direct
/app/ visit, and static checks.

The browser harness serves, from loopback only, the BUILT marketing site
(site/dist/public) at "/" and the canonical chat (frontend/) at "/app/" --
the routing production's web server provides -- with
frontend_harness/handoff_child.js injected into the chat (scripted fetch,
storage-write recording). Headless Microsoft Edge (or another
Chromium-family browser) is driven over the DevTools protocol at an exact
1440 x 900 desktop and 390 x 844 touch-phone viewport. Nothing leaves the
machine: the chat's fetch is scripted, every non-loopback connection goes to a
dead proxy, and no other host name resolves. test_homepage_interaction.py
uses this harness for the homepage chat workspace itself.

The homepage chat is one stable in-page workspace: there is no full-screen
handoff, no URL change and no message between the homepage and the chat.

Build the site first (cd site && npm run build). Without a build the browser
tests skip; with a build older than the site sources they fail.

The static checks always run.
"""

from __future__ import annotations

import base64
import contextlib
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
SITE_CSS = SITE_SRC / "index.css"
BACKEND = REPO / "candidate" / "app_fastapi_candidate.py"

VIEWPORTS = {"desktop": (1440, 900), "mobile": (390, 844)}
# The compact card's fixed height (site/src/index.css), by viewport.
# The homepage chat workspace's one stable height (site/src/index.css):
# clamp(560px, 70vh, 720px) on desktop, clamp(520px, 72svh, 680px) on phones.
WORKSPACE_HEIGHT = {"desktop": round(0.70 * 900), "mobile": round(0.72 * 844)}


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
        # When set, a top-level /app/ does not run handoff_child.js's own
        # standalone check; the test drives the page instead.
        self.child_passive = False
        self.http = LoopbackServer(self.route)

    def site_page(self) -> bytes:
        return (SITE_DIST / "index.html").read_bytes()

    def chat_page(self) -> bytes:
        page = INDEX_HTML.read_text(encoding="utf-8")
        marker = "<!-- Local vendored libraries only"
        assert page.count(marker) == 1
        passive = "<script>window.__HANDOFF_PASSIVE__ = true;</script>" if self.child_passive else ""
        return page.replace(
            marker, passive + '<script src="/__handoff__/child.js"></script>\n  ' + marker, 1
        ).encode("utf-8")

    def route(self, raw_path: str):
        path = raw_path.split("?", 1)[0]

        if path in ("/", "/privacy", "/terms"):
            return 200, "text/html; charset=utf-8", self.site_page()
        if path == "/app/":
            return 200, "text/html; charset=utf-8", self.chat_page()
        if path.startswith("/app/"):
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
                 reduced_motion: bool = False):
    """Launch an isolated headless browser with one blank page at exactly
    `size` CSS pixels (touch emulation when mobile; prefers-reduced-motion
    emulated on request). Yields (devtools, session); always closes it."""
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

    with open(profile / "browser-stderr.txt", "wb") as stderr:
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr)

    devtools = None

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
    sources += [SITE / "index.html", SITE / "vite.config.ts", SITE / "package.json"]
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


def test_homepage_and_chat_exchange_no_messages_and_change_no_url():
    app = APP_TSX.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")

    for source in (app, js):
        assert "postMessage" not in source
        assert 'addEventListener("message"' not in source and "addEventListener('message'" not in source
    assert "pushState" not in app and "replaceState" not in app and "popstate" not in app
    assert "is-expanded" not in app and "is-active" not in app and "chat-expanded" not in app
    assert "is-expanded" not in APP_CSS.read_text(encoding="utf-8")
    # The iframe is the canonical chat at the fixed /app/ path.
    assert "const CHAT_PATH = '/app/';" in app
    assert not (SITE_SRC / "embed-protocol.ts").exists()
    assert not (HARNESS / "handoff_parent.js").exists()
    assert "<iframe ref={frameRef} src={CHAT_PATH}" in app


def test_no_mutation_observer_storage_or_url_transfer():
    sources = {APP_JS: APP_JS.read_text(encoding="utf-8"), **site_sources()}

    for path, source in sources.items():
        assert "MutationObserver" not in source, path
        # API use, not the words (comments and the Privacy page name them).
        for forbidden in (r"\blocalStorage\s*[.\[]", r"\bsessionStorage\s*[.\[]", r"\bindexedDB\s*\.",
                          r"document\.cookie", r"\bcookieStore\s*\.", r"URLSearchParams",
                          r"location\.hash\s*=", r"location\.search\s*=", r"[?&#]prompt="):
            assert not re.search(forbidden, source), (path, forbidden)



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
    assert ('<span className="trust-item"><span className="live-dot" /> Temporary sessions</span>' in app
            and '<span className="trust-item">No account required</span>' in app
            and ">Clear provider disclosure<" in app)
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



def test_privacy_describes_the_embedded_chat_accurately():
    app = APP_TSX.read_text(encoding="utf-8")
    text = re.sub(r"<[^>]+>", "", app)

    assert ("The homepage embeds the same chat application that is served at /app/. Your messages and "
            "Kalillac’s responses are not passed through the page address, browser storage, or messages between "
            "browser windows. The embedded chat application sends each chat request itself, under the same "
            "temporary session behavior described above.") in text
    # The full-screen handoff no longer exists, so nothing may describe it.
    assert "full-screen" not in text and "open full-screen" not in text
    assert "exchange only a signal" not in text

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
