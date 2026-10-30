"""Account UI: the built site's /account pages against a deterministic fake API.

Everything is served from one loopback origin: the built site (site/dist/public)
plus a fake implementation of the account, recovery, verification, entitlement
and billing contracts. The browser runs through the repository's isolated CDP
harness, which resolves no host name except loopback and sends every
non-loopback connection to a dead local proxy. A Stripe redirect is therefore
observed as the navigation the page requests (it fails at that proxy); nothing
reaches Resend, Stripe, DNS, production or any other origin.

Navigations are deliberately not paused with the DevTools Fetch domain: while
a cross-origin navigation request is paused, Chromium holds the page session's
commands, so a test waiting on that session can never answer the pause.
"""
from __future__ import annotations

import http.server
import json
import re
import shutil
import threading
import time
import uuid

import pytest

from test_homepage_handoff import (
    APP_TSX, SITE_DIST, SITE_SRC, Site, browser_page, require_browser_and_build,
)
from test_route_navigation import Page

ACCOUNT_SRC = SITE_SRC / "account"
EMAIL = "member@example.com"
PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "another long passphrase 42"
# Canonical 32-byte URL-safe tokens (43 characters; the last carries 4 bits).
TOKEN = ("Kx9_-Qm3Tz" * 5)[:42] + "g"
OTHER_TOKEN = ("bR7-pLw2_N" * 5)[:42] + "A"
CREATED_AT = "2026-03-14T09:30:00+00:00"
USER_ID = str(uuid.UUID(int=0x1234567890ABCDEF1234567890ABCDEF))
DESKTOP, PHONE, NARROW = (1440, 900), (390, 844), (320, 640)

# Records every fetch: what it asked for, and whether the address still held a
# fragment when it was made. Test instrumentation only.
FETCH_RECORDER = """
(() => {
  const original = window.fetch.bind(window);
  window.__fetches = [];
  window.fetch = (input, init = {}) => {
    let keys = null;
    try { keys = init.body ? Object.keys(JSON.parse(init.body)).sort() : null; } catch { keys = 'unparsed'; }
    window.__fetches.push({ url: String(input), method: init.method || 'GET', credentials: init.credentials,
      cache: init.cache, redirect: init.redirect, keys, hadFragment: window.location.hash !== '',
      contentType: (init.headers || {})['Content-Type'] || null });
    return original(input, init);
  };
  // Completed JSON reads: once one resolves, the app's await continuation runs
  // in the same task, so a test can wait for a response to be fully handled.
  window.__jsonDone = 0;
  const json = Response.prototype.json;
  Response.prototype.json = function () { return json.call(this).finally(() => { window.__jsonDone += 1; }); };
})();
"""


# --- fake same-origin API ---------------------------------------------------------------------------


class Raw:
    """A response body sent exactly as given (an HTML proxy page, malformed JSON)."""

    def __init__(self, content_type, data):
        self.content_type, self.data = content_type, data


class Gate:
    """Holds one request until the test releases it: `arrived` is set when the
    request reaches the server, and the reply is `response` (or the default)."""

    def __init__(self, response=None):
        self.arrived, self.released, self.response = threading.Event(), threading.Event(), response

    def release(self):
        self.released.set()


class FakeAccountApi:
    """The account contracts, in memory. Sessions use an HttpOnly cookie, so a
    request is authenticated only if the browser sent its credentials."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self, *, enabled=True, non_json_404=False, signed_in=False, tier="free",
              verification=None, billing=None):
        with self.lock:
            self.enabled, self.non_json_404 = enabled, non_json_404
            self.password = PASSWORD
            self.sessions: set[str] = set()
            self.pending_cookie = None
            if signed_in:
                cookie = uuid.uuid4().hex
                self.sessions.add(cookie)
                self.pending_cookie = cookie
            self.tier = tier
            # None: verification off (no email_verified field); else verified?
            self.verified = verification
            self.billing = billing
            self.recovery_tokens = {TOKEN}
            self.verification_tokens = {TOKEN}
            self.overrides: dict[tuple[str, str], list] = {}
            self.holds: dict[tuple[str, str], list[Gate]] = {}
            self.calls: list[dict] = []
            self.registered: list[str] = []

    def override(self, method, path, status, body=None, headers=None):
        self.overrides.setdefault((method, path), []).append((status, body, headers or {}))

    def hold(self, method, path, response=None):
        gate = Gate(response)
        with self.lock:
            self.holds.setdefault((method, path), []).append(gate)
        return gate

    def user(self):
        user = {"id": USER_ID, "email": EMAIL, "created_at": CREATED_AT}
        if self.verified is not None:
            user["email_verified"] = self.verified
        return {"user": user}

    def handle(self, method, path, headers, raw):
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = "invalid"
        cookie = re.search(r"kalillac_account=([0-9a-f]+)", headers.get("Cookie") or "")
        session = cookie.group(1) if cookie else None
        with self.lock:
            self.calls.append({"method": method, "path": path, "body": body,
                               "content_type": headers.get("Content-Type"),
                               "cookie": bool(session), "origin": headers.get("Origin"),
                               "fetch_site": headers.get("Sec-Fetch-Site"),
                               "cache": headers.get("Cache-Control")})
            waiting = self.holds.get((method, path))
            gate = waiting.pop(0) if waiting else None
        if gate:
            # Outside the lock: other requests are served while this one waits.
            gate.arrived.set()
            assert gate.released.wait(10), "a held request was never released"
            if gate.response:
                return gate.response
        with self.lock:
            if not self.enabled:
                return (404, None, {}) if self.non_json_404 else (404, {"detail": "Not Found"}, {})
            queued = self.overrides.get((method, path))
            if queued:
                return queued.pop(0)
            authed = session in self.sessions
            return self.default(method, path, body, authed, session)

    def default(self, method, path, body, authed, session):
        unauth = (401, {"error": "not_authenticated"}, {})
        exact = lambda *keys: isinstance(body, dict) and set(body) == set(keys)  # noqa: E731
        if (method, path) == ("GET", "/api/account/me"):
            return (200, self.user(), {}) if authed else unauth
        if (method, path) == ("POST", "/api/account/login"):
            if not exact("email", "password"):
                return 400, {"error": "invalid_body"}, {}
            if body["email"] == EMAIL and body["password"] == self.password:
                cookie = uuid.uuid4().hex
                self.sessions.add(cookie)
                return 200, self.user(), {"Set-Cookie": f"kalillac_account={cookie}; HttpOnly; Path=/; SameSite=Lax"}
            return 401, {"error": "invalid_credentials"}, {}
        if (method, path) == ("POST", "/api/account/register"):
            if not exact("email", "password"):
                return 400, {"error": "invalid_body"}, {}
            if body["email"] == EMAIL or body["email"] in self.registered:
                return 409, {"error": "email_already_registered"}, {}
            self.registered.append(body["email"])
            return 201, {"user": {"id": USER_ID, "email": body["email"], "created_at": CREATED_AT}}, {}
        if (method, path) == ("POST", "/api/account/logout"):
            self.sessions.discard(session)
            return 200, {"ok": True}, {"Set-Cookie": "kalillac_account=; Max-Age=0; Path=/; HttpOnly"}
        if (method, path) == ("GET", "/api/account/entitlements"):
            if not authed:
                return unauth
            paid = self.tier == "paid"
            return 200, {"entitlements": {"tier": self.tier, "saved_mode_access": paid,
                                          "higher_usage_access": paid, "persistent_memory_access": False}}, {}
        if (method, path) == ("POST", "/api/account/recovery/request"):
            return (202, {"ok": True}, {}) if exact("email") else (400, {"error": "invalid_body"}, {})
        if (method, path) == ("POST", "/api/account/recovery/confirm"):
            if not exact("token", "password"):
                return 400, {"error": "invalid_body"}, {}
            if body["token"] in self.recovery_tokens:
                self.recovery_tokens.discard(body["token"])
                self.password = body["password"]
                self.sessions.clear()
                return 200, {"ok": True}, {}
            return 400, {"error": "invalid_or_expired_token"}, {}
        if (method, path) == ("POST", "/api/account/email-verification/request"):
            if body != {}:
                return 400, {"error": "invalid_body"}, {}
            return (202, {"ok": True}, {}) if authed else unauth
        if (method, path) == ("POST", "/api/account/email-verification/confirm"):
            if not exact("token"):
                return 400, {"error": "invalid_body"}, {}
            if body["token"] in self.verification_tokens:
                self.verification_tokens.discard(body["token"])
                self.verified = True
                return 200, {"ok": True}, {}
            return 400, {"error": "invalid_or_expired_token"}, {}
        if path.startswith("/api/account/billing"):
            if self.billing is None:
                return 404, {"detail": "Not Found"}, {}
            if not authed:
                return unauth
            if (method, path) == ("GET", "/api/account/billing"):
                return 200, {"billing": self.billing}, {}
            if (method, path) == ("POST", "/api/account/billing/checkout"):
                return 200, {"checkout_url": "https://checkout.stripe.com/c/pay/cs_test_fake"}, {}
            if (method, path) == ("POST", "/api/account/billing/portal"):
                return 200, {"portal_url": "https://billing.stripe.com/p/session/test_fake"}, {}
        return 404, {"detail": "Not Found"}, {}


class AccountSite(Site):
    """The built site (with its flat account.html entry for every /account
    route) and the fake API on one loopback origin."""

    def __init__(self):
        super().__init__()
        self.api = FakeAccountApi()
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self, status, ctype, body, extra=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _api(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                status, body, extra = site.api.handle(method, self.path.split("?", 1)[0], self.headers, raw)
                if isinstance(body, Raw):
                    self._reply(status, body.content_type, body.data, extra)
                elif body is None:
                    self._reply(status, "text/html; charset=utf-8", b"<!doctype html><p>Not found</p>", extra)
                else:
                    self._reply(status, "application/json", json.dumps(body).encode(), extra)

            def do_GET(self):
                site.http_paths.append(self.path)
                if self.path.startswith("/api/"):
                    return self._api("GET")
                if self.path == "/__session__" and site.api.pending_cookie:
                    cookie, site.api.pending_cookie = site.api.pending_cookie, None
                    return self._reply(204, "text/plain", b"",
                                       {"Set-Cookie": f"kalillac_account={cookie}; HttpOnly; Path=/; SameSite=Lax"})
                self._reply(*site.route(self.path))

            def do_POST(self):
                site.http_paths.append("POST " + self.path)
                if self.path.startswith("/api/"):
                    return self._api("POST")
                self._reply(404, "text/plain", b"")

            def log_message(self, *args):
                pass

        self.http_paths: list[str] = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def route(self, raw_path):
        path = raw_path.split("?", 1)[0]
        if path == "/account" or path.startswith("/account/"):
            return 200, "text/html; charset=utf-8", (SITE_DIST / "account.html").read_bytes()
        return super().route(raw_path)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


class AccountPage(Page):
    def __init__(self, dt, session, site):
        super().__init__(dt, session)
        self.site = site
        # Off-origin URLs a test expects the page to request (Stripe redirects).
        self.expected_offsite: list[str] = []

    def offsite(self):
        origin = self.site.origin + "/"
        return [u for u in self.requests() if not (u.startswith(origin) or u.startswith("data:"))]

    def wait_for_offsite(self, timeout=15):
        """Wait for the page to request an off-origin URL (the request goes
        nowhere: the harness resolves no host and proxies to a dead port)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.try_eval("return 1;")          # keeps CDP events flowing
            if self.offsite():
                return self.offsite()
            time.sleep(0.05)
        raise AssertionError("timed out waiting for an off-origin navigation")

    def open(self, path, *, ready="document.querySelector('main')"):
        self.dt.events.clear()
        self.expected_offsite.clear()
        if self.site.api.pending_cookie:
            self.dt.call("Page.navigate", {"url": self.site.origin + "/__session__"}, self.session)
            self.wait_for("document.readyState === 'complete'")
        self.dt.call("Page.navigate", {"url": self.site.origin + path}, self.session)
        self.wait_for(ready)

    def text(self, selector="main"):
        # innerText puts a blank line after each paragraph; one break is enough here.
        value = self.eval(f"return document.querySelector({json.dumps(selector)})?.innerText ?? null;")
        return None if value is None else re.sub(r"\n\s*\n+", "\n", value).strip()

    def type_into(self, selector, value):
        self.eval(f"document.querySelector({json.dumps(selector)}).focus();")
        self.dt.call("Input.insertText", {"text": value}, self.session)

    def click_selector(self, selector):
        self.eval(f"document.querySelector({json.dumps(selector)}).click();")

    def key(self, key, code, vk, text=None, shift=False):
        for kind in ("keyDown", "keyUp"):
            params = {"type": kind, "key": key, "code": code, "windowsVirtualKeyCode": vk,
                      "modifiers": 8 if shift else 0}
            if text and kind == "keyDown":
                params["text"] = text
            self.dt.call("Input.dispatchKeyEvent", params, self.session)

    def active(self):
        return self.eval("const e = document.activeElement; return e.dataset.testid || e.id || "
                         "(e.getAttribute('aria-controls') ? 'reveal:' + e.getAttribute('aria-controls') : '') || "
                         "e.textContent.trim();")

    def requests(self):
        return [e["params"]["request"]["url"] for e in self.dt.events
                if e.get("method") == "Network.requestWillBeSent"]

    def fetches(self):
        return self.eval("return window.__fetches || [];")

    def json_reads(self):
        return self.eval("return window.__jsonDone;")

    def after_json_reads(self, count):
        """Wait until `count` JSON reads have completed, then one task and one
        frame, so the app's continuation and any render it causes are done."""
        self.wait_for(f"window.__jsonDone >= {count}")
        self.eval("await new Promise(r => setTimeout(() => requestAnimationFrame(() => setTimeout(r, 0)), 0));")

    def console(self):
        return [e for e in self.dt.events if e.get("method") in ("Runtime.consoleAPICalled", "Log.entryAdded")]

    def resize(self, size, mobile):
        self.dt.call("Emulation.setDeviceMetricsOverride", {
            "width": size[0], "height": size[1], "deviceScaleFactor": 1, "mobile": mobile}, self.session)
        self.dt.call("Emulation.setTouchEmulationEnabled", {"enabled": mobile, "maxTouchPoints": 5}, self.session)


@pytest.fixture(scope="module")
def browser_site(tmp_path_factory):
    browser = require_browser_and_build()
    assert (SITE_DIST / "account.html").is_file(), "Build the site (account.html entry missing)"
    profile = tmp_path_factory.mktemp("edge-account-ui")
    site = AccountSite()
    with site:
        with browser_page(browser, profile, DESKTOP, False) as (dt, session):
            dt.call("Runtime.enable", {}, session)
            dt.call("Log.enable", {}, session)
            dt.call("Page.addScriptToEvaluateOnNewDocument", {"source": FETCH_RECORDER}, session)
            # Dates are shown in the visitor's own time zone; pin one so the
            # expected local dates do not depend on the machine running this.
            dt.call("Emulation.setTimezoneOverride", {"timezoneId": "America/New_York"}, session)
            yield AccountPage(dt, session, site)
    shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture
def page(browser_site):
    browser_site.site.api.reset()
    browser_site.resize(DESKTOP, False)
    browser_site.dt.call("Emulation.setEmulatedMedia", {"features": [
        {"name": "prefers-reduced-motion", "value": "no-preference"}]}, browser_site.session)
    browser_site.dt.call("Network.clearBrowserCookies", {}, browser_site.session)
    yield browser_site
    # Every test leaves the browser exactly as clean as it found it.
    assert browser_site.eval("return localStorage.length + sessionStorage.length;") == 0
    assert browser_site.eval("return document.cookie;") == ""
    assert browser_site.eval("return (await indexedDB.databases()).length;") == 0
    assert [u for u in browser_site.offsite() if u not in browser_site.expected_offsite] == []
    for event in browser_site.console():
        rendered = json.dumps(event)
        for secret in (PASSWORD, NEW_PASSWORD, TOKEN, OTHER_TOKEN):
            assert secret not in rendered


def api_calls(page, path=None):
    return [c for c in page.site.api.calls if path is None or c["path"] == path]


# --- static contracts -----------------------------------------------------------------------------------


def account_sources():
    return {p.name: p.read_text(encoding="utf-8") for p in ACCOUNT_SRC.glob("*") if p.is_file()}


def test_account_module_uses_only_fixed_same_origin_paths_and_safe_fetch_options():
    api = (ACCOUNT_SRC / "account-api.ts").read_text(encoding="utf-8")
    assert re.findall(r"fetch\(", api) == ["fetch("]
    assert "credentials: 'same-origin'" in api and "cache: 'no-store'" in api and "redirect: 'error'" in api
    paths = set(re.findall(r"'(/api/[^']*)'", api))
    assert paths == {
        "/api/account/me", "/api/account/register", "/api/account/login", "/api/account/logout",
        "/api/account/entitlements", "/api/account/recovery/request", "/api/account/recovery/confirm",
        "/api/account/email-verification/request", "/api/account/email-verification/confirm",
        "/api/account/billing", "/api/account/billing/checkout", "/api/account/billing/portal",
    }
    assert "'checkout.stripe.com'" in api and "'billing.stripe.com'" in api
    for name, source in account_sources().items():
        for forbidden in (r"\bconsole\.", r"\blocalStorage\b", r"\bsessionStorage\b", r"\bindexedDB\b",
                          r"document\.cookie", r"\bpushState\b", r"https?://(?!checkout\.stripe\.com|billing\.stripe\.com)",
                          r"dangerouslySetInnerHTML", r"\bpostMessage\b", r"\bsendBeacon\b"):
            assert not re.search(forbidden, source), (name, forbidden)
    pages = (ACCOUNT_SRC / "account-pages.tsx").read_text(encoding="utf-8")
    # The only history change scrubs the fragment in place.
    assert pages.count("replaceState") == 1
    assert "window.history.replaceState(window.history.state, '', window.location.pathname + window.location.search)" in pages
    # Password inputs are uncontrolled: React never mirrors them into an attribute.
    assert "value={" not in pages and "defaultValue" not in pages


def test_homepage_source_gains_no_account_entry_point():
    app = APP_TSX.read_text(encoding="utf-8")
    home = app[app.index("function HomePage()"):app.index("type DocSectionProps")]
    header = app[app.index("function SiteHeader()"):app.index("function SiteFooter()")]
    footer = app[app.index("function SiteFooter()"):app.index("function OrbitalAssistant()")]
    for part in (home, header, footer):
        assert "/account" not in part
    assert "lazy(() => import('@/account/account-pages'))" in app


def test_built_account_entry_is_noindex_and_outside_the_sitemap():
    html = (SITE_DIST / "account.html").read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex, nofollow" />' in html
    assert '<link rel="canonical" href="https://kalillac.ai/account" />' in html
    assert "<title>Your account — Kalillac AI</title>" in html
    assert "/account" not in (SITE_DIST / "sitemap.xml").read_text(encoding="utf-8")
    index = (SITE_DIST / "index.html").read_text(encoding="utf-8")
    assert "account-pages" not in index and '<meta name="robots" content="index, follow" />' in index
    main_css = [p for p in (SITE_DIST / "assets").glob("index-*.css")]
    assert len(main_css) == 1 and ".acct" not in main_css[0].read_text(encoding="utf-8")


# --- homepage and availability ------------------------------------------------------------------------------


def test_homepage_makes_no_account_request_and_loads_no_account_code(page):
    page.open("/", ready="document.querySelector('[data-testid=\"section-hero\"]')")
    page.wait_for("document.readyState === 'complete'")
    time.sleep(1.5)
    assert [c for c in api_calls(page) if c["path"].startswith("/api/account")] == []
    assert not any("account" in url for url in page.requests())
    assert page.eval("return document.querySelectorAll('a[href^=\"/account\"]').length;") == 0
    assert page.eval("return document.querySelector('meta[name=robots]').content;") == "index, follow"


@pytest.mark.parametrize("non_json", [False, True])
def test_unavailable_accounts_show_no_form(page, non_json):
    page.site.api.reset(enabled=False, non_json_404=non_json)
    for path in ("/account/sign-in", "/account/create", "/account", "/account/recovery"):
        page.open(path)
        page.wait_for("document.querySelector('[data-testid=\"account-unavailable\"]')")
        assert page.eval("return document.querySelectorAll('main form, main input').length;") == 0
        assert page.text("h1") == "Accounts aren’t available here yet"
        assert "Temporary chat remains available." in page.text()
        assert page.eval("return document.querySelector('[data-testid=\"account-unavailable\"] a').getAttribute('href');") == "/#chat"


@pytest.mark.parametrize("status,body,state", [
    (200, Raw("text/html; charset=utf-8", b"<!doctype html><p>static fallback</p>"), "account-unavailable"),
    (404, Raw("text/html; charset=utf-8", b"<!doctype html><p>not found</p>"), "account-unavailable"),
    (502, Raw("text/html; charset=utf-8", b"<!doctype html><h1>502 Bad Gateway</h1>"), "account-unreachable"),
    (503, Raw("text/html; charset=utf-8", b"<!doctype html><h1>503 Service Unavailable</h1>"), "account-unreachable"),
    (200, Raw("application/json", b'{"user": {"email": '), "account-unreachable"),
    (200, {"unexpected": True}, "account-unreachable"),
], ids=["html-200", "html-404", "html-502", "html-503", "malformed-json-200", "unexpected-json-200"])
def test_session_responses_are_classified_exactly(page, status, body, state):
    page.site.api.override("GET", "/api/account/me", status, body)
    page.open("/account/sign-in")
    page.wait_for(f"document.querySelector('[data-testid=\"{state}\"]')")
    other = "account-unreachable" if state == "account-unavailable" else "account-unavailable"
    assert page.eval(f"return document.querySelector('[data-testid=\"{other}\"]');") is None
    assert page.eval("return document.querySelectorAll('main form').length;") == 0
    for leaked in ("Bad Gateway", "Service Unavailable", "static fallback", "unexpected"):
        assert leaked not in page.text("body")


def test_unreachable_server_offers_a_retry(page):
    page.site.api.override("GET", "/api/account/me", 503, {"detail": "boom"})
    page.open("/account/sign-in")
    page.wait_for("document.querySelector('[data-testid=\"account-unreachable\"]')")
    assert "boom" not in page.text()
    page.click_selector("[data-testid='account-unreachable'] button")
    page.wait_for("document.querySelector('#email')")


# --- sign in ----------------------------------------------------------------------------------------------------


def sign_in_form(page):
    page.open("/account/sign-in")
    page.wait_for("document.querySelector('#email')")


def test_sign_in_sends_exact_json_with_safe_fetch_options_and_reaches_the_account(page):
    sign_in_form(page)
    page.type_into("#email", "  Member@Example.com ")
    page.type_into("#password", PASSWORD)
    page.key("Enter", "Enter", 13, "\r")
    page.wait_for("location.pathname === '/account' && document.querySelector('[data-testid=\"account-email\"]')")
    # The plan row fills in after its own request; wait for it to settle.
    page.wait_for("!document.querySelector('[data-testid=\"account-plan\"]').textContent.startsWith('Loading')")
    [login] = api_calls(page, "/api/account/login")
    assert login["body"] == {"email": EMAIL, "password": PASSWORD}
    assert login["content_type"] == "application/json"
    assert login["fetch_site"] == "same-origin"
    for call in page.fetches():
        assert call["url"].startswith("/api/account/")
        assert (call["credentials"], call["cache"], call["redirect"]) == ("same-origin", "no-store", "error")
        assert call["contentType"] == ("application/json" if call["method"] == "POST" and call["keys"] is not None else None)
    # Later requests carry the HttpOnly session cookie the browser received.
    assert all(c["cookie"] for c in api_calls(page, "/api/account/entitlements"))
    assert page.text("[data-testid='account-email']") == EMAIL
    assert page.text("[data-testid='account-member-since']") == "March 2026"
    assert page.text("[data-testid='account-plan']") == "Free account"
    main = page.text()
    assert USER_ID not in main and USER_ID not in page.eval("return document.documentElement.outerHTML;")
    assert PASSWORD not in page.eval("return document.documentElement.outerHTML;")


def test_unknown_email_and_wrong_password_share_one_message(page):
    messages = []
    for email, password in (("someone@example.com", PASSWORD), (EMAIL, "not the right password")):
        sign_in_form(page)
        page.type_into("#email", email)
        page.type_into("#password", password)
        page.click_selector("button[type=submit]")
        page.wait_for("document.querySelector('[data-testid=\"account-notice\"]')")
        assert page.active() == "account-notice"
        messages.append(page.text("[data-testid='account-notice']"))
    assert messages[0] == messages[1] == (
        "That email and password don’t match\n"
        "Check both and try again. If you’ve forgotten your password, you can reset it.")


def test_throttling_uses_retry_after_without_naming_the_account(page):
    page.site.api.override("POST", "/api/account/login", 429, {"error": "too_many_attempts"}, {"Retry-After": "130"})
    sign_in_form(page)
    page.type_into("#email", EMAIL)
    page.type_into("#password", PASSWORD)
    page.click_selector("button[type=submit]")
    page.wait_for("document.querySelector('[data-testid=\"account-notice\"]')")
    assert page.text("[data-testid='account-notice']") == (
        "Too many sign-in attempts\nWait about 3 minutes, then try again.")


def test_client_validation_is_inline_and_focuses_the_first_problem(page):
    sign_in_form(page)
    height = page.eval("return document.querySelector('form').getBoundingClientRect().height;")
    page.click_selector("button[type=submit]")
    page.wait_for("document.querySelector('#email-error').textContent !== ''")
    assert page.active() == "email"
    assert page.eval("return document.querySelector('#email').getAttribute('aria-invalid');") == "true"
    assert "email-error" in page.eval("return document.querySelector('#email').getAttribute('aria-describedby');")
    assert page.text("#password-error") == "Enter your password."
    # Reserved error lines: the form does not change height.
    assert page.eval("return document.querySelector('form').getBoundingClientRect().height;") == height
    assert api_calls(page, "/api/account/login") == []


def test_duplicate_submissions_are_ignored_while_pending(page):
    page.site.api.override("POST", "/api/account/login", 401, {"error": "invalid_credentials"})
    sign_in_form(page)
    page.type_into("#email", EMAIL)
    page.type_into("#password", PASSWORD)
    page.eval("const b = document.querySelector('button[type=submit]'); b.click(); b.click(); b.click();")
    page.wait_for("document.querySelector('[data-testid=\"account-notice\"]')")
    assert len(api_calls(page, "/api/account/login")) == 1


def test_pending_submission_is_marked_and_blocks_repeat_activation(page):
    gate = page.site.api.hold("POST", "/api/account/login", (401, {"error": "invalid_credentials"}, {}))
    sign_in_form(page)
    page.type_into("#email", EMAIL)
    page.type_into("#password", PASSWORD)
    page.click_selector("button[type=submit]")
    assert gate.arrived.wait(5)
    page.wait_for("document.querySelector('button[type=submit]').getAttribute('aria-disabled') === 'true'")
    assert page.text("button[type=submit]") == "Signing in…"
    # Repeat activation by click and by Enter is ignored while pending.
    page.click_selector("button[type=submit]")
    page.eval("document.querySelector('#password').focus();")
    page.key("Enter", "Enter", 13, "\r")
    reads = page.json_reads()
    gate.release()
    page.after_json_reads(reads + 1)
    page.wait_for("document.querySelector('[data-testid=\"account-notice\"]')")
    assert len(api_calls(page, "/api/account/login")) == 1
    assert page.eval("return document.querySelector('button[type=submit]').getAttribute('aria-disabled');") is None


def test_signed_in_visitors_skip_the_sign_in_form(page):
    page.site.api.reset(signed_in=True)
    page.open("/account/sign-in")
    page.wait_for("location.pathname === '/account' && document.querySelector('[data-testid=\"account-email\"]')")


def test_password_reveal_toggle_is_accessible_and_keeps_the_value_out_of_attributes(page):
    sign_in_form(page)
    page.type_into("#password", PASSWORD)
    toggle = "button[aria-controls='password']"
    assert page.eval(f"return document.querySelector(\"{toggle}\").getAttribute('aria-pressed');") == "false"
    assert page.eval(f"return document.querySelector(\"{toggle}\").innerText;").startswith("Show")
    page.click_selector(toggle)
    page.wait_for("document.querySelector('#password').type === 'text'")
    assert page.eval(f"return document.querySelector(\"{toggle}\").getAttribute('aria-pressed');") == "true"
    page.click_selector(toggle)
    page.wait_for("document.querySelector('#password').type === 'password'")
    html = page.eval("return document.documentElement.outerHTML;")
    assert PASSWORD not in html
    assert page.eval("return document.querySelector('#password').autocomplete;") == "current-password"
    assert page.eval("return document.querySelector('#email').autocomplete;") == "email"
    assert page.eval("return [...document.querySelectorAll('label')].map(l => l.htmlFor);") == ["email", "password"]


# --- create account -----------------------------------------------------------------------------------------


def test_registration_sends_only_email_and_password_and_does_not_sign_in(page):
    page.open("/account/create")
    page.wait_for("document.querySelector('#confirm-password')")
    page.type_into("#email", "new@example.com")
    page.type_into("#new-password", PASSWORD)
    page.type_into("#confirm-password", PASSWORD + "x")
    page.click_selector("button[type=submit]")
    page.wait_for("document.querySelector('#confirm-password-error').textContent !== ''")
    assert page.text("#confirm-password-error") == "The passwords don’t match."
    assert api_calls(page, "/api/account/register") == []
    page.eval("document.querySelector('#confirm-password').value = '';")
    page.type_into("#confirm-password", PASSWORD)
    page.click_selector("button[type=submit]")
    page.wait_for("document.querySelector('[data-testid=\"account-created\"]')")
    [call] = api_calls(page, "/api/account/register")
    assert call["body"] == {"email": "new@example.com", "password": PASSWORD}
    assert page.active() == "account-created"
    assert "Creating an account doesn’t sign you in." in page.text()
    assert page.site.api.sessions == set()
    page.click_selector("[data-testid='account-created'] a")
    page.wait_for("location.pathname === '/account/sign-in' && document.querySelector('#email')")
    assert page.eval("return document.querySelector('#email').value;") == "new@example.com"
    assert page.eval("return document.querySelector('#password').value;") == ""


@pytest.mark.parametrize("password,problem", [("short", "Use 12–256 characters."), ("x" * 257, "Use 12–256 characters.")])
def test_registration_enforces_only_the_approved_length(page, password, problem):
    page.open("/account/create")
    page.wait_for("document.querySelector('#new-password')")
    page.type_into("#email", "new@example.com")
    page.type_into("#new-password", password)
    page.type_into("#confirm-password", password)
    page.click_selector("button[type=submit]")
    page.wait_for("document.querySelector('#new-password-error').textContent !== ''")
    assert page.text("#new-password-error") == problem
    assert page.eval("return document.querySelector('#new-password-hint').textContent;") == "Use 12–256 characters."
    assert api_calls(page, "/api/account/register") == []


def test_duplicate_email_is_explained_with_next_steps(page):
    page.open("/account/create")
    page.wait_for("document.querySelector('#new-password')")
    page.type_into("#email", EMAIL)
    page.type_into("#new-password", PASSWORD)
    page.type_into("#confirm-password", PASSWORD)
    page.click_selector("button[type=submit]")
    page.wait_for("document.querySelector('[data-testid=\"account-notice\"]')")
    assert page.text("#email-error") == "An account already uses this email address."
    assert page.eval("return [...document.querySelectorAll('[data-testid=\"account-notice\"] a')].map(a => a.getAttribute('href'));") == [
        "/account/sign-in", "/account/recovery"]


# --- recovery ---------------------------------------------------------------------------------------------------


def test_recovery_request_is_identical_for_every_address(page):
    texts = []
    for email in (EMAIL, "nobody@example.com"):
        page.open("/account/recovery")
        page.wait_for("document.querySelector('#email')")
        page.type_into("#email", email)
        page.click_selector("button[type=submit]")
        page.wait_for("document.querySelector('[data-testid=\"recovery-requested\"]')")
        assert page.active() == "recovery-requested"
        texts.append(page.text("main .acct-body"))
    assert texts[0] == texts[1]
    assert "If an account can be reset for that address" in texts[0]
    assert [c["body"] for c in api_calls(page, "/api/account/recovery/request")] == [
        {"email": EMAIL}, {"email": "nobody@example.com"}]


def test_recovery_request_throttling_and_invalid_email(page):
    page.site.api.override("POST", "/api/account/recovery/request", 429, {"error": "too_many_attempts"}, {"Retry-After": "40"})
    page.open("/account/recovery")
    page.wait_for("document.querySelector('#email')")
    page.type_into("#email", "not-an-address")
    page.click_selector("button[type=submit]")
    page.wait_for("document.querySelector('#email-error').textContent !== ''")
    assert page.text("#email-error") == "Enter an email address like name@example.com."
    page.eval("document.querySelector('#email').value = '';")
    page.type_into("#email", EMAIL)
    page.click_selector("button[type=submit]")
    page.wait_for("document.querySelector('[data-testid=\"account-notice\"]')")
    assert page.text("[data-testid='account-notice']") == "Too many reset requests\nWait about a minute, then try again."


def open_with_fragment(page, path, fragment):
    page.open(path + fragment, ready="document.querySelector('main .acct-body') && !document.querySelector('.acct-quiet')")


def test_recovery_link_is_scrubbed_before_any_request_and_never_signs_in(page):
    open_with_fragment(page, "/account/recovery/confirm", "#token=" + TOKEN)
    page.wait_for("document.querySelector('#new-password')")
    assert page.eval("return location.href;") == page.site.origin + "/account/recovery/confirm"
    # The password form carries an empty, hidden username field, so the browser
    # logs no diagnostic (which would carry the page address) about the form.
    assert page.eval("""const u = document.querySelector('[data-testid="recovery-username-field"]');
      return [u.hidden, u.value, u.autocomplete, u.tabIndex, u.form === document.querySelector('main form')];""") == [
        True, "", "username", -1, True]
    assert page.fetches() and all(not call["hadFragment"] for call in page.fetches())
    assert TOKEN not in page.eval("return document.documentElement.outerHTML + document.title;")
    page.type_into("#new-password", NEW_PASSWORD)
    page.type_into("#confirm-password", NEW_PASSWORD)
    page.click_selector("button[type=submit]")
    page.wait_for("document.querySelector('[data-testid=\"recovery-complete\"]')")
    [call] = api_calls(page, "/api/account/recovery/confirm")
    assert call["body"] == {"token": TOKEN, "password": NEW_PASSWORD}
    text = page.text()
    assert "Your password has been changed." in text
    assert "Every device that was signed in to this account has been signed out." in text
    assert "You haven’t been signed in here" in text
    assert page.site.api.sessions == set()
    html = page.eval("return document.documentElement.outerHTML;")
    assert TOKEN not in html and NEW_PASSWORD not in html
    # Reloading the scrubbed address cannot replay the credential.
    page.open("/account/recovery/confirm", ready="document.querySelector('[data-testid=\"invalid-link\"]')")
    assert len(api_calls(page, "/api/account/recovery/confirm")) == 1


@pytest.mark.parametrize("kind", ["missing", "malformed", "wrong-key", "unknown", "replayed"])
@pytest.mark.parametrize("path,endpoint", [
    ("/account/recovery/confirm", "/api/account/recovery/confirm"),
    ("/account/verify", "/api/account/email-verification/confirm"),
])
def test_every_unusable_link_gets_the_same_state(page, kind, path, endpoint):
    fragment = {"missing": "", "malformed": "#token=" + TOKEN[:-1] + "!", "wrong-key": "#code=" + TOKEN,
                "unknown": "#token=" + OTHER_TOKEN, "replayed": "#token=" + TOKEN}[kind]
    if kind == "replayed":
        page.site.api.recovery_tokens.clear()
        page.site.api.verification_tokens.clear()
    open_with_fragment(page, path, fragment)
    if path == "/account/recovery/confirm" and kind in ("unknown", "replayed"):
        page.wait_for("document.querySelector('#new-password')")
        page.type_into("#new-password", NEW_PASSWORD)
        page.type_into("#confirm-password", NEW_PASSWORD)
        page.click_selector("button[type=submit]")
    page.wait_for("document.querySelector('[data-testid=\"invalid-link\"]')")
    assert page.eval("return location.hash;") == ""
    assert "This link is invalid or has expired." in page.text()
    sent = api_calls(page, endpoint)
    assert len(sent) == (1 if kind in ("unknown", "replayed") else 0)
    assert all(not call["hadFragment"] for call in page.fetches())


@pytest.mark.parametrize("path,endpoint", [
    ("/account/recovery/confirm", "/api/account/recovery/confirm"),
    ("/account/verify", "/api/account/email-verification/confirm"),
])
def test_a_link_opened_on_the_same_page_is_taken_and_scrubbed(page, path, endpoint):
    """A second link opened in a tab already on the page changes only the
    fragment: no reload happens, so the page must handle it itself."""
    open_with_fragment(page, path, "")
    page.wait_for("document.querySelector('[data-testid=\"invalid-link\"]')")
    page.dt.call("Page.navigate", {"url": page.site.origin + path + "#token=" + TOKEN}, page.session)
    page.wait_for("location.hash === '' && !document.querySelector('[data-testid=\"invalid-link\"]')")
    assert page.eval("return location.href;") == page.site.origin + path
    if path == "/account/recovery/confirm":
        page.wait_for("document.querySelector('#new-password')")
        page.type_into("#new-password", NEW_PASSWORD)
        page.type_into("#confirm-password", NEW_PASSWORD)
        page.click_selector("button[type=submit]")
        page.wait_for("document.querySelector('[data-testid=\"recovery-complete\"]')")
    else:
        page.wait_for("document.querySelector('[data-testid=\"verification-complete\"]')")
    assert [c["body"]["token"] for c in api_calls(page, endpoint)] == [TOKEN]
    assert all(not call["hadFragment"] for call in page.fetches())
    assert TOKEN not in page.eval("return document.documentElement.outerHTML;")


@pytest.mark.parametrize("stale_reply", [
    (400, {"error": "invalid_or_expired_token"}, {}), (200, {"ok": True}, {}),
], ids=["stale-invalid", "stale-success"])
def test_a_stale_recovery_response_cannot_touch_a_newer_link(page, stale_reply):
    api = page.site.api
    api.recovery_tokens = {TOKEN, OTHER_TOKEN}
    gate = api.hold("POST", "/api/account/recovery/confirm", stale_reply)
    open_with_fragment(page, "/account/recovery/confirm", "#token=" + TOKEN)
    page.wait_for("document.querySelector('#new-password')")
    page.type_into("#new-password", NEW_PASSWORD)
    page.type_into("#confirm-password", NEW_PASSWORD)
    page.click_selector("button[type=submit]")
    assert gate.arrived.wait(5)                                   # request A is in flight
    # Link B opens in the same tab: a same-document fragment change.
    page.dt.call("Page.navigate", {"url": page.site.origin + "/account/recovery/confirm#token=" + OTHER_TOKEN}, page.session)
    page.wait_for("location.hash === '' && document.querySelector('#new-password').value === ''")
    second = PASSWORD + " for link b"
    page.type_into("#new-password", second)
    page.type_into("#confirm-password", second)
    page.eval("document.querySelector('#confirm-password').focus();")
    reads = page.json_reads()
    gate.release()                                                # A answers late
    page.after_json_reads(reads + 1)
    # A changed nothing: B's form, typed fields, focus and credential remain.
    assert page.eval("""return {
      form: !!document.querySelector('#new-password'), invalid: !!document.querySelector('[data-testid="invalid-link"]'),
      done: !!document.querySelector('[data-testid="recovery-complete"]'),
      notice: !!document.querySelector('[data-testid="account-notice"]'),
      typed: document.querySelector('#new-password').value.length, focus: document.activeElement.id };""") == {
        "form": True, "invalid": False, "done": False, "notice": False, "typed": len(second), "focus": "confirm-password"}
    page.click_selector("button[type=submit]")
    page.wait_for("document.querySelector('[data-testid=\"recovery-complete\"]')")
    assert [(c["body"]["token"], c["body"]["password"]) for c in api_calls(page, "/api/account/recovery/confirm")] == [
        (TOKEN, NEW_PASSWORD), (OTHER_TOKEN, second)]
    exposed = page.eval("return location.href + document.title + document.head.outerHTML + document.documentElement.outerHTML;")
    for secret in (TOKEN, OTHER_TOKEN, NEW_PASSWORD, second):
        assert secret not in exposed


@pytest.mark.parametrize("second_token", [OTHER_TOKEN, TOKEN], ids=["different-token", "same-token-again"])
def test_a_stale_verification_response_cannot_replace_a_newer_result(page, second_token):
    api = page.site.api
    api.reset(verification=False)
    api.verification_tokens = {TOKEN, OTHER_TOKEN}
    gate = api.hold("POST", "/api/account/email-verification/confirm", (400, {"error": "invalid_or_expired_token"}, {}))
    page.open("/account/verify#token=" + TOKEN)
    assert gate.arrived.wait(5)                                   # verification A is in flight
    page.dt.call("Page.navigate", {"url": page.site.origin + "/account/verify#token=" + second_token}, page.session)
    page.wait_for("location.hash === '' && document.querySelector('[data-testid=\"verification-complete\"]')")
    before = page.eval("return document.activeElement.dataset.testid || document.activeElement.tagName;")
    reads = page.json_reads()
    gate.release()                                                # A finishes after B
    page.after_json_reads(reads + 1)
    assert page.eval("""return {
      done: !!document.querySelector('[data-testid="verification-complete"]'),
      invalid: !!document.querySelector('[data-testid="invalid-link"]'),
      notice: !!document.querySelector('[data-testid="account-notice"]'),
      focus: document.activeElement.dataset.testid || document.activeElement.tagName };""") == {
        "done": True, "invalid": False, "notice": False, "focus": before}
    assert [c["body"]["token"] for c in api_calls(page, "/api/account/email-verification/confirm")] == [TOKEN, second_token]
    exposed = page.eval("return location.href + document.title + document.head.outerHTML + document.documentElement.outerHTML;")
    assert TOKEN not in exposed and OTHER_TOKEN not in exposed


# --- email verification ----------------------------------------------------------------------------------------


def test_verification_link_confirms_without_signing_in(page):
    page.site.api.reset(verification=False)
    open_with_fragment(page, "/account/verify", "#token=" + TOKEN)
    page.wait_for("document.querySelector('[data-testid=\"verification-complete\"]')")
    assert page.eval("return location.href;") == page.site.origin + "/account/verify"
    assert [c["body"] for c in api_calls(page, "/api/account/email-verification/confirm")] == [{"token": TOKEN}]
    assert "This doesn’t sign you in or change your password." in page.text()
    assert page.site.api.sessions == set() and page.site.api.verified is True
    assert TOKEN not in page.eval("return document.documentElement.outerHTML;")


def test_unverified_account_can_request_verification(page):
    page.site.api.reset(signed_in=True, verification=False)
    page.open("/account")
    page.wait_for("document.querySelector('[data-testid=\"button-request-verification\"]')")
    assert page.text("[data-testid='account-verification']").startswith("Not verified yet")
    page.click_selector("[data-testid='button-request-verification']")
    page.wait_for("document.querySelector('[data-testid=\"account-notice\"]')")
    assert [c["body"] for c in api_calls(page, "/api/account/email-verification/request")] == [{}]
    notice = page.text("[data-testid='account-notice']")
    assert notice.startswith("Verification email requested") and "sent" not in notice
    assert page.active() == "account-notice"


def test_verification_state_appears_only_when_the_server_reports_it(page):
    page.site.api.reset(signed_in=True, verification=None)
    page.open("/account")
    page.wait_for("document.querySelector('[data-testid=\"account-plan\"]').textContent === 'Free account'")
    assert page.eval("return document.querySelector('[data-testid=\"account-verification\"]');") is None
    page.site.api.reset(signed_in=True, verification=True)
    page.open("/account")
    page.wait_for("document.querySelector('[data-testid=\"account-verification\"]')")
    assert page.text("[data-testid='account-verification']") == "Verified"


# --- overview, entitlements and billing -------------------------------------------------------------------------


# Words that would claim a feature, benefit or price this branch does not have.
UNIMPLEMENTED_CLAIMS = (
    "persistent memory", "memory", "spaces", "space for", "saved conversation", "save your conversation",
    "conversation history", "chat history", "model", "research", "artifact", "export", "pdf", "docx",
    "spreadsheet", "unlimited", "priority", "higher usage", "saved mode", "quota", "usage",
    "per month", "/month", "/mo", "price", "pricing", "$", "€", "£", "plus",
)
# Permanent product claims the locked roadmap retires.
RETIRED_CLAIMS = ("no plan includes", "doesn’t save your conversations", "doesn't save your conversations",
                  "chats stay temporary", "each chat is still a temporary session")


def assert_only_current_functionality(page):
    text = page.text("body").lower()
    for claim in UNIMPLEMENTED_CLAIMS + RETIRED_CLAIMS:
        assert claim not in text, claim
    # The account describes only what the backend holds today.
    assert "your account currently holds your sign-in email, verification status, plan and billing settings." in text


@pytest.mark.parametrize("tier,label", [("paid", "Paid plan"), ("free", "Free account")])
def test_plans_are_described_without_invented_features_or_prices(page, tier, label):
    billing = {"subscription_status": "active" if tier == "paid" else None, "cancel_at_period_end": False,
               "current_period_end": "2026-12-01T17:00:00+00:00" if tier == "paid" else None,
               "portal_available": tier == "paid"}
    page.site.api.reset(signed_in=True, tier=tier, verification=True, billing=billing)
    page.open("/account")
    page.wait_for(f"document.querySelector('[data-testid=\"account-plan\"]').textContent === {json.dumps(label)}"
                  " && document.querySelector('[data-testid=\"billing-section\"]')")
    assert_only_current_functionality(page)
    assert page.eval("return document.querySelector('[data-testid=\"account-plan\"]').textContent;") == label


@pytest.mark.parametrize("path", ["/account/sign-in", "/account/create", "/account/recovery"])
def test_signed_out_pages_make_no_feature_or_price_claims(page, path):
    page.open(path)
    page.wait_for("document.querySelector('main form')")
    text = page.text("body").lower()
    for claim in UNIMPLEMENTED_CLAIMS + RETIRED_CLAIMS:
        assert claim not in text, (path, claim)


def test_billing_unavailable_shows_no_billing_panel(page):
    page.site.api.reset(signed_in=True, billing=None)
    page.open("/account")
    page.wait_for("document.querySelector('[data-testid=\"account-plan\"]').textContent === 'Free account'")
    time.sleep(0.4)
    assert page.eval("return document.querySelector('[data-testid=\"billing-section\"]');") is None
    assert page.eval("return document.querySelectorAll('[data-testid=\"button-upgrade\"],[data-testid=\"button-manage-billing\"]').length;") == 0


FREE_BILLING = {"subscription_status": None, "cancel_at_period_end": False, "current_period_end": None, "portal_available": False}


def test_checkout_follows_only_stripe_checkout(page):
    page.site.api.reset(signed_in=True, billing=dict(FREE_BILLING))
    page.open("/account")
    page.wait_for("document.querySelector('[data-testid=\"button-upgrade\"]')")
    assert "Paid access is added after Stripe confirms the payment" in page.text("[data-testid='billing-section']")
    page.click_selector("[data-testid='button-upgrade']")
    # Exactly one off-origin request: the top-level navigation to Checkout.
    assert page.wait_for_offsite() == ["https://checkout.stripe.com/c/pay/cs_test_fake"]
    assert [e["params"]["type"] for e in page.dt.events if e.get("method") == "Network.requestWillBeSent"
            and e["params"]["request"]["url"].startswith("https://")] == ["Document"]
    [call] = api_calls(page, "/api/account/billing/checkout")
    assert call["body"] is None
    page.expected_offsite.append("https://checkout.stripe.com/c/pay/cs_test_fake")
    page.open("/account/sign-in")       # back on the origin for the storage checks


@pytest.mark.parametrize("url", [
    "https://evil.example/checkout", "http://checkout.stripe.com/c/pay/x", "https://checkout.stripe.com.evil.example/x",
    "https://user@checkout.stripe.com/x", "https://checkout.stripe.com:8443/x", "javascript:alert(1)", "/relative",
])
def test_untrusted_checkout_redirect_is_refused(page, url):
    page.site.api.reset(signed_in=True, billing=dict(FREE_BILLING))
    page.site.api.override("POST", "/api/account/billing/checkout", 200, {"checkout_url": url})
    page.open("/account")
    page.wait_for("document.querySelector('[data-testid=\"button-upgrade\"]')")
    page.click_selector("[data-testid='button-upgrade']")
    page.wait_for("document.querySelector('[data-testid=\"account-notice\"]')")
    assert page.text("[data-testid='account-notice']").startswith("The billing page couldn’t be opened")
    assert page.eval("return location.pathname;") == "/account" and page.offsite() == []
    assert url not in page.text("body")


@pytest.mark.parametrize("status,code,title", [
    (401, "not_authenticated", None),
    (403, "email_verification_required", "Verify your email first"),
    (409, "already_subscribed", "This account already has a subscription"),
    (409, "subscription_unresolved", "A previous subscription needs attention"),
    (409, "billing_processing", "Your payment is being confirmed"),
    (502, "billing_unavailable", "Billing is temporarily unavailable"),
])
def test_checkout_states(page, status, code, title):
    page.site.api.reset(signed_in=True, billing=dict(FREE_BILLING))
    page.site.api.override("POST", "/api/account/billing/checkout", status, {"error": code})
    page.open("/account")
    page.wait_for("document.querySelector('[data-testid=\"button-upgrade\"]')")
    if status == 401:
        page.site.api.sessions.clear()      # the server ended the session
    page.click_selector("[data-testid='button-upgrade']")
    if title is None:
        page.wait_for("location.pathname === '/account/sign-in' && document.querySelector('[data-testid=\"account-notice\"]')")
        assert page.text("[data-testid='account-notice']").startswith("Your session has ended")
        return
    page.wait_for("document.querySelector('[data-testid=\"account-notice\"]')")
    assert page.text("[data-testid='account-notice']").startswith(title)
    assert code not in page.text("body")
    assert page.offsite() == []


def test_unverified_free_account_is_asked_to_verify_before_upgrading(page):
    page.site.api.reset(signed_in=True, verification=False, billing=dict(FREE_BILLING))
    page.open("/account")
    page.wait_for("document.querySelector('[data-testid=\"billing-needs-verification\"]')")
    assert page.eval("return document.querySelector('[data-testid=\"button-upgrade\"]');") is None


def test_paid_plan_period_cancellation_and_portal(page):
    billing = {"subscription_status": "active", "cancel_at_period_end": True,
               "current_period_end": "2026-12-01T00:00:00+00:00", "portal_available": True}
    page.site.api.reset(signed_in=True, tier="paid", billing=billing)
    page.open("/account")
    page.wait_for("document.querySelector('[data-testid=\"button-manage-billing\"]')")
    assert page.eval("return document.querySelector('[data-testid=\"button-upgrade\"]');") is None
    assert page.text("[data-testid='billing-period']") == (
        "Your plan is set to end. The current billing period ends on November 30, 2026, and it won’t renew.")
    # 2026-12-01T00:00:00Z is still November 30 in New York: the local date is shown.
    page.click_selector("[data-testid='button-manage-billing']")
    assert page.wait_for_offsite() == ["https://billing.stripe.com/p/session/test_fake"]
    assert api_calls(page, "/api/account/billing/portal")[0]["body"] is None
    page.expected_offsite.append("https://billing.stripe.com/p/session/test_fake")
    page.open("/account/sign-in")       # back on the origin for the storage checks


def test_renewing_plan_and_portal_failures(page):
    billing = {"subscription_status": "active", "cancel_at_period_end": False,
               "current_period_end": "2026-12-01T00:00:00+00:00", "portal_available": True}
    page.site.api.reset(signed_in=True, tier="paid", billing=billing)
    page.site.api.override("POST", "/api/account/billing/portal", 409, {"error": "billing_profile_unavailable"})
    page.site.api.override("POST", "/api/account/billing/portal", 200, {"portal_url": "https://checkout.stripe.com/x"})
    page.open("/account")
    page.wait_for("document.querySelector('[data-testid=\"button-manage-billing\"]')")
    assert page.text("[data-testid='billing-period']") == "The current billing period ends on November 30, 2026."
    page.click_selector("[data-testid='button-manage-billing']")
    page.wait_for("document.querySelector('[data-testid=\"account-notice\"]')")
    assert page.text("[data-testid='account-notice']").startswith("There’s no billing profile for this account yet")
    page.click_selector("[data-testid='button-manage-billing']")
    page.wait_for("document.querySelector('[data-testid=\"account-notice\"]')?.textContent.startsWith('The billing page')")
    assert page.offsite() == []


def test_expired_session_returns_to_sign_in_with_a_note(page):
    # The session expires between the account check and the plan request.
    page.site.api.reset(signed_in=True)
    page.site.api.override("GET", "/api/account/me", 200, page.site.api.user())
    page.site.api.sessions.clear()
    page.open("/account")
    page.wait_for("location.pathname === '/account/sign-in' && document.querySelector('[data-testid=\"account-notice\"]')")
    assert page.text("[data-testid='account-notice']") == "Your session has ended\nSign in again to continue."


def test_signed_out_overview_redirects_to_sign_in(page):
    page.open("/account")
    page.wait_for("location.pathname === '/account/sign-in' && document.querySelector('#email')")


def test_sign_out(page):
    page.site.api.reset(signed_in=True)
    page.open("/account")
    page.wait_for("document.querySelector('[data-testid=\"button-sign-out\"]')")
    page.click_selector("[data-testid='button-sign-out']")
    page.wait_for("location.pathname === '/account/sign-in' && document.querySelector('[data-testid=\"account-notice\"]')")
    assert page.text("[data-testid='account-notice']").startswith("You’re signed out")
    assert api_calls(page, "/api/account/logout")[0]["body"] is None
    assert page.site.api.sessions == set()


# --- navigation, metadata and history ----------------------------------------------------------------------------


EXPECTED_TITLES = {
    "/account/sign-in": "Sign in — Kalillac AI", "/account/create": "Create an account — Kalillac AI",
    "/account/recovery": "Reset your password — Kalillac AI",
    "/account/recovery/confirm": "Choose a new password — Kalillac AI",
    "/account/verify": "Verify your email — Kalillac AI",
}


@pytest.mark.parametrize("path", list(EXPECTED_TITLES))
def test_direct_load_metadata_is_route_specific_and_noindex(page, path):
    fragment = "#token=" + TOKEN if path in ("/account/recovery/confirm", "/account/verify") else ""
    page.open(path + fragment)
    page.wait_for(f"document.title === {json.dumps(EXPECTED_TITLES[path])}")
    meta = page.eval("""return {
      robots: document.querySelector('meta[name=robots]').content,
      canonical: document.querySelector('link[rel=canonical]').href,
      og: document.querySelector('meta[property="og:url"]').content,
      head: document.head.outerHTML };""")
    assert meta["robots"] == "noindex, nofollow"
    assert meta["canonical"] == meta["og"] == "https://kalillac.ai" + path
    assert TOKEN not in meta["head"] and EMAIL not in meta["head"] and "#" not in meta["canonical"]


def test_in_app_navigation_back_forward_refresh_and_focus(page):
    page.open("/account/sign-in")
    page.wait_for("document.querySelector('[data-testid=\"link-create-account\"]')")
    page.eval("window.scrollTo(0, 400);")
    page.click_selector("[data-testid='link-create-account']")
    page.wait_for("location.pathname === '/account/create' && document.querySelector('#confirm-password')")
    assert page.eval("return Math.round(window.scrollY);") == 0
    assert page.active() == "acct-title"
    assert page.eval("return document.title;") == "Create an account — Kalillac AI"
    page.eval("history.back();")
    page.wait_for("location.pathname === '/account/sign-in' && document.querySelector('#password')")
    page.eval("history.forward();")
    page.wait_for("location.pathname === '/account/create' && document.querySelector('#confirm-password')")
    page.dt.call("Page.reload", {}, page.session)
    page.wait_for("location.pathname === '/account/create' && document.querySelector('#confirm-password')")
    # Leaving for a public page restores public, indexable metadata.
    page.click_selector("[data-testid='link-footer-privacy']")
    page.wait_for("location.pathname === '/privacy' && document.querySelector('h1.doc-title')")
    assert page.eval("return document.querySelector('meta[name=robots]').content;") == "index, follow"
    assert page.eval("return document.title;") == "Privacy — Kalillac AI"


def test_keyboard_order_on_sign_in(page):
    sign_in_form(page)
    page.eval("document.querySelector('#email').focus();")
    order = [page.active()]
    for _ in range(6):
        page.key("Tab", "Tab", 9)
        order.append(page.active())
    assert order == ["email", "password", "reveal:password", "Sign in", "link-forgot-password",
                     "link-create-account", "link-continue-private"]
    page.key("Tab", "Tab", 9, shift=True)
    assert page.active() == "link-create-account"


def test_continue_without_an_account_goes_to_the_private_chat(page):
    sign_in_form(page)
    assert page.eval("return document.querySelector('[data-testid=\"link-continue-private\"]').getAttribute('href');") == "/#chat"


# --- layout, reflow and motion -------------------------------------------------------------------------------------


LAYOUT_ROUTES = ["/account/sign-in", "/account/create", "/account/recovery", "/account"]


@pytest.mark.parametrize("size,mobile", [(DESKTOP, False), (PHONE, True), (NARROW, True), ((640, 450), False)])
def test_layout_reflows_without_horizontal_overflow(page, size, mobile):
    page.resize(size, mobile)
    for path in LAYOUT_ROUTES:
        if path == "/account":
            # The fullest overview: unverified, with billing available.
            page.site.api.reset(signed_in=True, verification=False, billing=dict(FREE_BILLING))
            ready = "document.querySelector('[data-testid=\"billing-needs-verification\"]')"
        else:
            page.site.api.reset()
            ready = "document.querySelector('main form')"
        page.open(path)
        page.wait_for(ready)
        geometry = page.eval("""
          const primary = document.querySelector('.acct-primary').getBoundingClientRect();
          const aside = document.querySelector('.acct-aside').getBoundingClientRect();
          const small = [...document.querySelectorAll('main button, main a, main input')]
            .filter(e => e.offsetParent !== null)
            .map(e => [e.dataset.testid || e.id || e.textContent.trim(), e.getBoundingClientRect().height])
            .filter(([, h]) => h < 44);
          return { scroll: document.documentElement.scrollWidth, width: window.innerWidth,
                   primary: [primary.left, primary.top, primary.right], aside: [aside.left, aside.top], small };""")
        assert geometry["scroll"] <= geometry["width"], (path, size)
        assert geometry["small"] == [], (path, size, geometry["small"])
        if size == DESKTOP:
            # Two columns of one surface: the note starts where the task ends
            # (no overlap) and both begin on the same line.
            assert geometry["aside"][0] >= geometry["primary"][2], path
            assert abs(geometry["aside"][1] - geometry["primary"][1]) < 2, path
        else:
            assert geometry["aside"][1] > geometry["primary"][1], path          # form first, then the aside


def test_reduced_motion_removes_transitions(page):
    page.dt.call("Emulation.setEmulatedMedia", {"features": [
        {"name": "prefers-reduced-motion", "value": "reduce"}]}, page.session)
    sign_in_form(page)
    durations = page.eval("""return ['#email', 'button[type=submit]'].map(s =>
      getComputedStyle(document.querySelector(s)).transitionDuration.split(',').map(parseFloat));""")
    assert all(value < 0.001 for group in durations for value in group)
