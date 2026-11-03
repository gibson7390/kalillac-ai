"""Homepage first load: the hero never shows a browser-generated page.

The hero's chat is the canonical /app/ client in a same-origin iframe. These
tests serve the BUILT site (site/dist/public) and the real chat (frontend/)
from loopback origins with the production framing headers (X-Frame-Options:
SAMEORIGIN and CSP frame-ancestors 'self', as kalillac.ai sends them), then
make /app/ succeed, refuse framing, hang, load the wrong document, redirect to
another origin or arrive slowly. A recorder in the homepage samples every
animation frame: the iframe may be painted only once the chat is proven
ready, and the slot always shows either the ready chat or the native panel.

Separate loopback ports are separate origins: A is the canonical site; B and
C stand in for another origin and an alias host (as the legacy alias domain
is for kalillac.ai). Headless Microsoft Edge (or another Chromium-family browser)
is driven over the DevTools protocol; nothing leaves the machine.
"""

from __future__ import annotations

import http.server
import json
import re
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from test_homepage_handoff import (
    APP_TSX, FRONTEND, INDEX_HTML, REPO, SITE_CSS, SITE_DIST, VIEWPORTS, browser_page, require_browser_and_build,
    serve_file,
)
from test_homepage_interaction import Page


PRODUCTION_FRAMING = {"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "frame-ancestors 'self'"}
DENY_FRAMING = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"}
HTML = "text/html; charset=utf-8"
READY_TIMEOUT_S = 10  # CHAT_READY_TIMEOUT_MS in App.tsx


# --- loopback origins --------------------------------------------------------------------------


class Origin:
    """One loopback origin. route(path) -> (status, headers, body, delay seconds)."""

    def __init__(self):
        self.paths: list[str] = []
        self.route = lambda path: (404, {}, b"", 0)
        self.release = threading.Event()
        origin = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                origin.paths.append(self.path)
                status, headers, body, delay = origin.route(self.path.split("?", 1)[0])
                if delay:
                    origin.release.wait(delay)
                try:
                    self.send_response(status)
                    for name, value in {"Content-Length": str(len(body)), "Cache-Control": "no-store",
                                        **headers}.items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def do_POST(self):
                origin.paths.append("POST " + self.path)
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


def site_route(app_route, framing=PRODUCTION_FRAMING):
    """The built site at /, the chat's files under /app/, and app_route for /app/ itself.
    Two fixed replacement documents: the real chat refusing framing at /denied/,
    and a framable same-origin document that is not the chat at /wrong/."""
    def route(path):
        if path == "/":
            return 200, {"Content-Type": HTML, **framing}, (SITE_DIST / "index.html").read_bytes(), 0
        if path == "/app/":
            return app_route()
        if path == "/denied/":
            return 200, {"Content-Type": HTML, **DENY_FRAMING}, INDEX_HTML.read_bytes(), 0
        if path == "/wrong/":
            return 200, {"Content-Type": HTML, **PRODUCTION_FRAMING}, IMPOSTOR, 0
        if path.startswith("/app/"):
            status, ctype, body = serve_file(FRONTEND, path[len("/app/"):])
            return status, {"Content-Type": ctype}, body, 0
        status, ctype, body = serve_file(SITE_DIST, path.lstrip("/"))
        return status, {"Content-Type": ctype}, body, 0
    return route


def chat(headers=PRODUCTION_FRAMING, delay=0):
    return lambda: (200, {"Content-Type": HTML, **headers}, INDEX_HTML.read_bytes(), delay)


# A framable page at /app/ that is not the chat, and claims to be ready by
# message: same origin, the frame's own window, every common shape.
IMPOSTOR = (b"<!doctype html><title>Kalillac AI</title><p>Not the chat</p><script>"
            b"for (const m of [{type:'ready'}, {type:'kalillac-chat-ready'}, 'ready']) parent.postMessage(m, '*');"
            b"</script>")


# --- the homepage recorder ---------------------------------------------------------------------

CARD_TITLES = ["Research the current web", "Write and revise", "Code and debug", "Learn anything"]

# Installed before any page script. Audits the slot on every animation frame
# (a filmstrip of the whole load, including its first two seconds) and at
# every change to it (a MutationObserver, so each state transition is seen at
# the moment it happens). The slot shows either the COMPLETE native twin of the
# empty chat -- heading, the four example cards, the composer -- or the proven
# real chat, painted and on top; a frame that is not proven is never painted
# and is inert, aria-hidden and untabbable; a ready frame's CURRENT document is
# the real chat; the slot never goes blank, never shows the old link-only
# panel, never changes size, and the hero around it never moves.
RECORDER = r"""
if (window.top === window) {
  const TITLES = %s;
  const r = window.__fl = { samples: 0, early: 0, states: [], transitions: [], violations: [], film: [] };
  const box = () => document.querySelector('[data-testid="embedded-chat"]');
  const shown = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility === 'visible';
  const twinComplete = t => {
    if (!t || t.inert || t.getAttribute('aria-hidden') === 'true') return false;
    const cards = [...t.querySelectorAll('[data-testid="chat-twin-card"]')].filter(shown);
    const titles = cards.map(c => c.querySelector('.ct-hint-title').textContent);
    const composer = t.querySelector('[data-testid="chat-twin-composer"]');
    return JSON.stringify(titles) === JSON.stringify(TITLES) && cards.every(c => c.getAttribute('href') === '/app/')
      && shown(t.querySelector('.ct-title')) && t.querySelector('.ct-title').textContent === 'What are you working on?'
      && shown(composer) && composer.getAttribute('href') === '/app/'
      && shown(t.querySelector('.ct-placeholder')) && t.querySelector('.ct-placeholder').textContent === 'Message Kalillac AI…'
      && shown(t.querySelector('.ct-erase')) && shown(t.querySelector('.ct-send'));
  };
  const place = el => { const b = el.getBoundingClientRect(); return [b.left + scrollX, b.top + scrollY, b.width, b.height]; };
  let hero = null, slotSize = null;
  const audit = where => {
    const b = box();
    if (!b) return;
    const state = b.dataset.chatState, frame = b.querySelector('iframe'), twin = b.querySelector('[data-testid="chat-twin"]');
    const painted = !!frame && getComputedStyle(frame).visibility === 'visible';
    const complete = twinComplete(twin);
    const bad = what => r.violations.push(what + ' while ' + state + ' (' + where + ')');
    if (r.states[r.states.length - 1] !== state) r.states.push(state);
    if (painted && state !== 'ready') bad('frame painted');
    if (frame && state !== 'ready' && !(frame.inert && frame.getAttribute('aria-hidden') === 'true' && frame.tabIndex === -1))
      bad('frame interactive');
    if (state !== 'ready' && !complete) bad('twin incomplete');
    if (document.querySelector('[data-testid="chat-panel"], [data-testid="link-chat-panel"]')) bad('link-only panel');
    if (state === 'ready') {
      let chat = false;
      try {
        const d = frame.contentDocument, l = frame.contentWindow.location;
        chat = !!d && l.origin === location.origin && l.pathname === '/app/'
          && d.body.classList.contains('is-embedded') && !!d.getElementById('composer-input');
      } catch (e) { chat = false; }
      if (!chat) bad('ready on a document that is not the chat');
      if (!painted) bad('ready but not painted');
      if (twin && !(twin.inert && twin.getAttribute('aria-hidden') === 'true')) bad('twin live beneath the chat');
    }
    // Hit testing: the visible part of the slot belongs to the chat when ready, to the twin otherwise.
    const s = b.getBoundingClientRect(), header = document.querySelector('.site-header').getBoundingClientRect();
    const top = Math.max(s.top, header.bottom) + 4, bottom = Math.min(s.bottom, innerHeight) - 4;
    if (bottom - top > 8) {
      const hit = document.elementFromPoint(s.left + s.width / 2, (top + bottom) / 2);
      if (state === 'ready' ? hit !== frame : !(twin && twin.contains(hit))) bad('slot shows ' + (hit ? hit.tagName : 'nothing'));
    }
    const slot = b.parentElement.getBoundingClientRect(), size = [Math.round(slot.width), Math.round(slot.height)];
    if (!slotSize) slotSize = size; else if (size[0] !== slotSize[0] || size[1] !== slotSize[1]) bad('slot resized');
    const now = [place(document.getElementById('hero-title')), place(document.querySelector('[data-testid="button-start-session"]'))];
    if (!hero) hero = now;
    else if (now.some((rect, i) => rect.some((v, j) => Math.abs(v - hero[i][j]) > 1))) bad('hero layout shifted');
    if (where === 'frame') {
      const t = Math.round(performance.now());
      if (t < 2000) r.early++;
      const last = r.film[r.film.length - 1];
      if (!last || t - last.t >= 100) r.film.push({ t, state, twin: complete, painted });
    }
  };
  let observed = false;
  const tick = () => {
    const b = box();
    if (b && !observed) {
      observed = true;
      r.transitions.push(b.dataset.chatState);
      new MutationObserver(() => {
        if (r.transitions[r.transitions.length - 1] !== b.dataset.chatState) r.transitions.push(b.dataset.chatState);
        audit('change');
      }).observe(b, { attributes: true, childList: true });
    }
    if (b) { r.samples++; audit('frame'); }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}
""" % json.dumps(CARD_TITLES)

SNAPSHOT = r"""
const box = document.querySelector('[data-testid="embedded-chat"]');
const frame = box && box.querySelector('iframe'), twin = box && box.querySelector('[data-testid="chat-twin"]');
const slot = document.querySelector('.embedded-chat-slot').getBoundingClientRect();
const rect = el => { const b = el.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; };
const shown = el => !!el && el.getClientRects().length > 0;
let composer = null;
try { composer = frame && frame.contentDocument && frame.contentDocument.getElementById('composer-input') ? 'present' : null; } catch (e) {}
return {
  state: box ? box.dataset.chatState : null, href: location.href, path: location.pathname, historyLength: history.length,
  frame: !!frame, frameInert: frame ? frame.inert : null, frameHidden: frame ? frame.getAttribute('aria-hidden') : null,
  framePainted: !!frame && getComputedStyle(frame).visibility === 'visible', composer,
  twin: !!twin, twinInert: twin ? twin.inert : null,
  twinCards: twin ? [...twin.querySelectorAll('[data-testid="chat-twin-card"]')].filter(shown)
                      .map(c => c.querySelector('.ct-hint-title').textContent) : [],
  twinRect: twin ? rect(twin) : null, slotRect: [Math.round(slot.left), Math.round(slot.top), Math.round(slot.width), Math.round(slot.height)],
  boxClient: box ? [box.clientWidth, box.clientHeight] : null,
  oldPanel: !!document.querySelector('[data-testid="chat-panel"], [data-testid="link-chat-panel"]'),
  recorder: window.__fl ? { samples: window.__fl.samples, early: window.__fl.early, states: window.__fl.states, film: window.__fl.film,
                            transitions: window.__fl.transitions, violations: window.__fl.violations } : null,
  docScrollWidth: document.documentElement.scrollWidth, innerWidth,
};
"""

# The important boxes and visible labels, relative to the chat slot's inner box,
# of the native twin (TWIN) and of the proven real chat inside the frame (CHAT).
TWIN_BOXES = r"""
const box = document.querySelector('[data-testid="embedded-chat"]'), origin = box.getBoundingClientRect();
const t = box.querySelector('[data-testid="chat-twin"]');
const shown = el => !!el && el.getClientRects().length > 0;
const at = el => { const b = el.getBoundingClientRect();
  return [b.left - origin.left - box.clientLeft, b.top - origin.top - box.clientTop, b.width, b.height]; };
const cards = [...t.querySelectorAll('.ct-hint')].filter(shown);
const label = [...t.querySelectorAll('.ct-starter-label')].find(shown);
return { title: [at(t.querySelector('.ct-title')), t.querySelector('.ct-title').textContent],
  line: [at(t.querySelector('.ct-line')), t.querySelector('.ct-line').textContent.trim()],
  label: label ? [at(label), label.textContent] : null,
  cards: cards.map(c => [at(c), c.querySelector('.ct-hint-title').textContent,
                         shown(c.querySelector('.ct-hint-description')) ? c.querySelector('.ct-hint-description').textContent : null,
                         at(c.querySelector('.ct-hint-icon')), at(c.querySelector('.ct-hint-arrow'))]),
  shell: at(t.querySelector('.ct-shell')), erase: [at(t.querySelector('.ct-erase')), t.querySelector('.ct-erase').textContent],
  input: [at(t.querySelector('.ct-placeholder')), t.querySelector('.ct-placeholder').textContent],
  send: at(t.querySelector('.ct-send')) };
"""

CHAT_BOXES = r"""
const d = document.querySelector('[data-testid="embedded-chat"] iframe').contentDocument;
const shown = el => !!el && el.getClientRects().length > 0;
const at = el => { const b = el.getBoundingClientRect(); return [b.left, b.top, b.width, b.height]; };
const cards = [...d.querySelectorAll('.hint')].filter(shown);
const label = [...d.querySelectorAll('.starter-label')].find(shown);
return { title: [at(d.querySelector('.empty-title')), d.querySelector('.empty-title').textContent],
  line: [at(d.querySelector('.empty-line')), d.querySelector('.empty-line').textContent.trim().replace(/\s+/g, ' ')],
  label: label ? [at(label), label.textContent] : null,
  cards: cards.map(c => [at(c), c.querySelector('.hint-title').textContent,
                         shown(c.querySelector('.hint-description')) ? c.querySelector('.hint-description').textContent : null,
                         at(c.querySelector('.hint-icon')), at(c.querySelector('.hint-arrow'))]),
  shell: at(d.getElementById('composer-shell')), erase: [at(d.getElementById('erase-btn')), d.getElementById('erase-btn').textContent.trim()],
  input: [at(d.getElementById('composer-input')), d.getElementById('composer-input').placeholder],
  send: at(d.getElementById('send-btn')) };
"""


def wait_until(page: Page, expression: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.eval(f"try {{ return !!({expression}); }} catch (e) {{ return false; }}"):
            return True
        time.sleep(0.05)
    return False


SETTLED = "['ready','unavailable'].includes(document.querySelector('[data-testid=\"embedded-chat\"]').dataset.chatState)"
STATE_IS = "document.querySelector('[data-testid=\"embedded-chat\"]').dataset.chatState === {}"
FRAME = "document.querySelector('[data-testid=\"embedded-chat\"] iframe')"
AT_TOP_LEVEL_CHAT = ("location.pathname === '/app/' && window.top === window"
                     " && !!document.getElementById('composer-input')")


def center(page: Page, selector: str) -> tuple[float, float]:
    """The centre of an element, scrolled into view: a CSS selector, or a JS expression starting with '['."""
    element = selector if selector.startswith("[...") else f"document.querySelector({json.dumps(selector)})"
    page.eval(f"({element}).scrollIntoView({{ block: 'center', behavior: 'instant' }});"
              "await new Promise(r => setTimeout(r, 150)); return true;")
    box = page.eval(f"const b = ({element}).getBoundingClientRect();"
                    "return [b.left + b.width / 2, b.top + b.height / 2];")
    return box[0], box[1]


def replace_ready_document(devtools, session, page: Page, home: str, script: str, settle: str | None) -> dict:
    """From a fresh homepage whose chat is proven ready, run `script` against
    the proven frame, then (unless the page itself leaves) wait for the chat
    to settle as `settle`."""
    devtools.call("Page.navigate", {"url": home}, session)
    time.sleep(0.3)
    assert wait_until(page, STATE_IS.format("'ready'"), READY_TIMEOUT_S + 8)
    time.sleep(0.3)
    out = {"before": page.eval(SNAPSHOT), "sync": page.eval(script)}
    if settle is None:
        return out
    out["settled"] = wait_until(page, STATE_IS.format(json.dumps(settle)), READY_TIMEOUT_S + 8)
    time.sleep(0.4)
    out["after"] = page.eval(SNAPSHOT)
    return out


def cta_to_top_level_chat(page: Page) -> dict:
    page.click(*center(page, '[data-testid="button-start-session"]'))
    arrived = wait_until(page, AT_TOP_LEVEL_CHAT, 20)
    return {"arrived": arrived, "path": page.eval("return location.pathname;"),
            "framed": page.eval("return window.top !== window;")}


def open_home(devtools, session, url: str) -> Page:
    devtools.call("Page.addScriptToEvaluateOnNewDocument", {"source": RECORDER}, session)
    devtools.call("Page.navigate", {"url": url}, session)
    page = Page(devtools, session)
    assert wait_until(page, "document.querySelector('[data-testid=\"embedded-chat\"]')", 30)
    return page


# --- scenarios ---------------------------------------------------------------------------------


_RUNS: dict[tuple[str, str], dict] = {}


def run_case(tmp_path_factory, name: str, viewport: str = "desktop") -> dict:
    """Load the homepage under one /app/ behaviour (once per module) and record what was shown."""
    if (name, viewport) not in _RUNS:
        _RUNS[name, viewport] = _run_case(tmp_path_factory, name, viewport)
    return _RUNS[name, viewport]


FORGE = (b"<script>for (const m of [{type:'ready'}, {type:'kalillac-chat-ready'}, 'ready'])"
         b" parent.postMessage(m, '*');</script>")


def _run_case(tmp_path_factory, name: str, viewport: str) -> dict:
    browser = require_browser_and_build()
    profile = tmp_path_factory.mktemp(f"edge-first-load-{name}-{viewport}")
    result: dict = {"name": name}
    with Origin() as a, Origin() as b, Origin() as c:
        b.route = site_route(chat())                          # another origin serving a real chat
        start = a.url + "/"
        a.route = site_route({
            "ready": chat(),
            "slow": chat(delay=2.5),
            "deny": chat(DENY_FRAMING),
            "hang": chat(delay=120),
            "impostor": lambda: (200, {"Content-Type": HTML, **PRODUCTION_FRAMING}, IMPOSTOR, 0),
            "recursive": lambda: (200, {"Content-Type": HTML, **PRODUCTION_FRAMING},
                                  (SITE_DIST / "index.html").read_bytes(), 0),
            "redirect": lambda: (302, {"Location": b.url + "/app/"}, b"", 0),
            "redirect-open": lambda: (302, {"Location": c.url + "/app/"}, b"", 0),
            "alias-redirect": chat(),
            "alias-direct": chat(),
        }[name])
        if name == "redirect-open":
            c.route = site_route(chat(headers={}))             # framable from anywhere
        if name == "alias-redirect":                           # alias domain -> kalillac.ai, path kept
            c.route = lambda path: (301, {"Location": a.url + path}, b"", 0)
            start = c.url + "/"
        if name == "alias-direct":                             # the alias serving the page itself,
            c.route = site_route(lambda: (301, {"Location": a.url + "/app/"}, b"", 0))  # its /app/ canonicalised
            start = c.url + "/"

        size, mobile = VIEWPORTS[viewport], viewport == "mobile"
        with browser_page(browser, profile, size, mobile) as (devtools, session):
            page = open_home(devtools, session, start)
            result["early"] = page.eval(SNAPSHOT)
            if name == "slow":
                result["twinBoxes"] = page.eval(TWIN_BOXES)
            if name == "hang":
                # Forged readiness: another origin, this window, and a synthetic
                # event naming the frame itself. None may reveal anything.
                b.route = lambda path: ((200, {"Content-Type": HTML}, FORGE, 0) if path == "/forge"
                                        else (404, {}, b"", 0))
                page.eval(f"""
                  const forge = document.createElement('iframe');
                  forge.src = {json.dumps(b.url + '/forge')}; forge.style.display = 'none';
                  document.body.appendChild(forge);
                  for (const m of [{{type:'ready'}}, {{type:'kalillac-chat-ready'}}, 'ready']) {{
                    window.postMessage(m, location.origin);
                    const f = document.querySelector('[data-testid="embedded-chat"] iframe');
                    window.dispatchEvent(new MessageEvent('message', {{ data: m, origin: location.origin,
                                                                       source: f && f.contentWindow }}));
                  }}
                  return true;""")
                time.sleep(1.5)
                result["forged"] = page.eval(SNAPSHOT)
                result["forge_paths"] = list(b.paths)
            settled = wait_until(page, SETTLED, READY_TIMEOUT_S + 8)
            result["settled_s"] = settled
            time.sleep(0.4)
            # The filmstrip always covers at least the first two seconds of the load.
            assert wait_until(page, "performance.now() > 2200", 5)
            result["final"] = page.eval(SNAPSHOT)
            result["a_paths"] = list(a.paths)

            if name == "ready":
                # A draft typed into the ready chat survives the CTA, which only focuses it.
                page.eval("""const d = document.querySelector('[data-testid="embedded-chat"] iframe').contentDocument;
                  const input = d.getElementById('composer-input');
                  input.focus(); input.value = 'my unsent draft';
                  input.dispatchEvent(new Event('input', { bubbles: true })); return true;""")
                page.click(*center(page, '[data-testid="button-start-session"]'))
                time.sleep(0.8)
                result["afterCta"] = page.eval("""const f = document.querySelector('[data-testid="embedded-chat"] iframe');
                  const d = f.contentDocument;
                  return { path: location.pathname, historyLength: history.length,
                           draft: d.getElementById('composer-input').value,
                           focused: document.activeElement === f && d.activeElement === d.getElementById('composer-input'),
                           posts: 0 };""")
                # A reload starts over: native panel first, the chat only once ready again.
                devtools.call("Page.reload", {"ignoreCache": True}, session)
                time.sleep(0.3)
                assert wait_until(page, SETTLED, READY_TIMEOUT_S + 8)
                time.sleep(0.4)
                result["afterReload"] = page.eval(SNAPSHOT)
                result["afterReloadDraft"] = page.eval(
                    """return document.querySelector('[data-testid="embedded-chat"] iframe')
                         .contentDocument.getElementById('composer-input').value;""")

                # The proven document replaced by a framing refusal, another
                # origin, a same-origin non-chat document or the homepage itself.
                home, replaced = a.url + "/", {}
                for kind, target in (("denied", a.url + "/denied/"), ("cross-origin", b.url + "/app/"),
                                     ("wrong", a.url + "/wrong/"), ("recursive", a.url + "/")):
                    replaced[kind] = replace_ready_document(
                        devtools, session, page, home,
                        f"{FRAME}.contentWindow.location.assign({json.dumps(target)}); return true;", "unavailable")
                    replaced[kind]["cta"] = cta_to_top_level_chat(page)
                # The CTA's own synchronous recheck: the proven document stops
                # being the chat and the CTA runs in the same task, before any
                # watcher could notice.
                replaced["cta-recheck"] = replace_ready_document(devtools, session, page, home, f"""
                    const f = {FRAME};
                    f.contentDocument.body.classList.remove('is-embedded');
                    document.querySelector('[data-testid="button-start-session"]').click();
                    const box = document.querySelector('[data-testid="embedded-chat"]');
                    return {{ state: box.dataset.chatState, frame: !!box.querySelector('iframe'),
                             focusedFrame: document.activeElement === f }};""", None)
                replaced["cta-recheck"]["arrived"] = wait_until(page, AT_TOP_LEVEL_CHAT, 20)
                # A valid reload of the real chat: hidden while it is proven again.
                replaced["reload"] = replace_ready_document(
                    devtools, session, page, home, f"{FRAME}.contentWindow.location.reload(); return true;", "ready")
                # The proven document announces it is leaving but stays: it is
                # retired, so no later check may prove it again.
                replaced["retired"] = replace_ready_document(devtools, session, page, home, f"""
                    {FRAME}.contentWindow.dispatchEvent(new PageTransitionEvent('pagehide', {{ persisted: false }}));
                    const box = document.querySelector('[data-testid="embedded-chat"]'), f = box.querySelector('iframe');
                    return {{ state: box.dataset.chatState, inert: f.inert, hidden: f.getAttribute('aria-hidden'),
                             tabIndex: f.tabIndex, painted: getComputedStyle(f).visibility === 'visible' }};""",
                    "unavailable")
                result["replaced"] = replaced
            if name == "slow":
                # Once the twin has left, the proven chat's own boxes and labels.
                assert wait_until(page, "!document.querySelector('[data-testid=\"chat-twin\"]')", 5)
                result["chatBoxes"] = page.eval(CHAT_BOXES)
                # Before readiness: the CTA, an example card and the composer each open /app/.
                result["early_use"] = use_before_ready(devtools, session, page, a.url + "/")
            if name == "deny":
                # The CTA, an example card and the composer all reach the working chat at /app/.
                page.click(*center(page, '[data-testid="button-start-session"]'))
                assert wait_until(page, "location.pathname === '/app/' && document.getElementById('composer-input')", 20)
                result["ctaArrival"] = page.eval("""const input = document.getElementById('composer-input');
                  return { path: location.pathname, framed: window.top !== window,
                           composer: !!input, usable: !!input && !input.disabled && !input.readOnly };""")
                for control, key in ((VISIBLE_CARD, "cardArrival"), ('[data-testid="chat-twin-composer"]', "composerArrival")):
                    devtools.call("Page.navigate", {"url": a.url + "/"}, session)
                    time.sleep(0.3)
                    assert wait_until(page, SETTLED, READY_TIMEOUT_S + 8)
                    page.click(*center(page, control))
                    arrived = wait_until(page, AT_TOP_LEVEL_CHAT, 20)
                    result[key] = {"arrived": arrived, "path": page.eval("return location.pathname;")}
                if mobile:
                    # The phone also loads a two-second-late chat and one that never becomes ready.
                    a.route = site_route(chat(delay=2.5))
                    devtools.call("Page.navigate", {"url": a.url + "/"}, session)
                    time.sleep(0.3)
                    slow = {"early": page.eval(SNAPSHOT), "twinBoxes": page.eval(TWIN_BOXES)}
                    slow["settled"] = wait_until(page, STATE_IS.format("'ready'"), READY_TIMEOUT_S + 8)
                    assert wait_until(page, "!document.querySelector('[data-testid=\"chat-twin\"]')", 5)
                    slow["final"], slow["chatBoxes"] = page.eval(SNAPSHOT), page.eval(CHAT_BOXES)
                    slow["early_use"] = use_before_ready(devtools, session, page, a.url + "/")
                    a.route = site_route(chat(delay=120))
                    devtools.call("Page.navigate", {"url": a.url + "/"}, session)
                    time.sleep(0.3)
                    hang = {"settled": wait_until(page, STATE_IS.format("'unavailable'"), READY_TIMEOUT_S + 8)}
                    time.sleep(0.4)
                    hang["final"] = page.eval(SNAPSHOT)
                    result["phone"] = {"slow": slow, "hang": hang}
            result["all_paths"] = list(a.paths)
    return result


VISIBLE_CARD = "[...document.querySelectorAll('[data-testid=\"chat-twin-card\"]')].find(c => c.getClientRects().length)"


def use_before_ready(devtools, session, page: Page, home: str) -> dict:
    """With a chat that is still loading, use the CTA, an example card and the twin's
    composer: each must open the full chat at top level."""
    out = {}
    for control in ("cta", "card", "composer"):
        devtools.call("Page.navigate", {"url": home}, session)
        assert wait_until(page, "document.querySelector('[data-testid=\"chat-twin-composer\"]')", 10)
        selector = {"cta": "document.querySelector('[data-testid=\"button-start-session\"]')", "card": VISIBLE_CARD,
                    "composer": "document.querySelector('[data-testid=\"chat-twin-composer\"]')"}[control]
        # Located and clicked in one task, while the frame is still unproven.
        before = page.eval(f"""const el = {selector};
          el.scrollIntoView({{ block: 'center', behavior: 'instant' }});
          const state = document.querySelector('[data-testid="embedded-chat"]').dataset.chatState;
          el.click(); return state;""")
        arrived = wait_until(page, AT_TOP_LEVEL_CHAT, 20)
        out[control] = {"stateAtUse": before, "arrived": arrived, "path": page.eval("return location.pathname;"),
                        "framed": page.eval("return window.top !== window;")}
    return out


CASES = ["ready", "slow", "deny", "hang", "impostor", "recursive", "redirect", "redirect-open",
         "alias-redirect", "alias-direct"]
UNAVAILABLE = {"deny", "hang", "impostor", "recursive", "redirect", "redirect-open", "alias-direct"}


@pytest.fixture(scope="module", params=CASES)
def case(request, tmp_path_factory):
    return run_case(tmp_path_factory, request.param)


@pytest.fixture(scope="module", params=["ready", "deny"])
def mobile_case(request, tmp_path_factory):
    return run_case(tmp_path_factory, request.param, "mobile")


def assert_complete_twin(snapshot: dict, label=""):
    assert snapshot["twin"] and snapshot["twinInert"] is False and snapshot["twinCards"] == CARD_TITLES, (label, snapshot)
    assert snapshot["oldPanel"] is False and snapshot["twinRect"][2:] == snapshot["boxClient"], label


def assert_twin_matches_chat(twin: dict, chat: dict, label: str):
    """Every important box of the native twin sits where the proven chat's does (within 1.5 px), with the same labels."""
    def close(a, b):
        return len(a) == len(b) and all(abs(x - y) <= 1.5 for x, y in zip(a, b))
    for part in ("title", "line", "erase", "input"):
        assert twin[part][1] == chat[part][1], (label, part, twin[part][1], chat[part][1])
        assert close(twin[part][0], chat[part][0]), (label, part, twin[part][0], chat[part][0])
    assert (twin["label"] is None) == (chat["label"] is None), label
    if twin["label"]:
        assert twin["label"][1] == chat["label"][1] and close(twin["label"][0], chat["label"][0]), (label, twin["label"], chat["label"])
    assert [c[1] for c in twin["cards"]] == [c[1] for c in chat["cards"]] == CARD_TITLES, label
    for mine, real in zip(twin["cards"], chat["cards"]):
        assert mine[2] == real[2], (label, mine[1], "description")
        for i, part in ((0, "card"), (3, "icon"), (4, "arrow")):
            assert close(mine[i], real[i]), (label, mine[1], part, mine[i], real[i])
    for part in ("shell", "send"):
        assert close(twin[part], chat[part]), (label, part, twin[part], chat[part])


def assert_used_before_ready(early_use: dict, label: str):
    for control in ("cta", "card", "composer"):
        assert early_use[control] == {"stateAtUse": "loading", "arrived": True, "path": "/app/", "framed": False}, (
            label, control, early_use[control])


def test_no_browser_page_is_ever_painted_and_the_slot_is_never_blank(case):
    for label in ("early", "forged", "final", "afterReload"):
        snapshot = case.get(label)
        if snapshot is None:
            continue
        recorder = snapshot["recorder"]
        assert recorder["samples"] >= 1, (case["name"], label, recorder)
        assert recorder["violations"] == [], (case["name"], label, recorder)
    # The recorder really watched the whole load frame by frame, including a
    # dense filmstrip of its first two seconds, so a half-second flash cannot escape.
    final = case["final"]["recorder"]
    assert final["samples"] > 20 and final["early"] >= 30, (case["name"], final["samples"], final["early"])
    assert final["film"] and final["film"][0]["state"] == "loading" and final["film"][0]["twin"], case["name"]


def test_first_paint_is_the_native_twin_until_the_chat_is_proven_ready(case):
    early = case["early"]
    assert early["recorder"]["states"][0] == "loading", case["name"]
    if early["state"] != "ready":
        assert_complete_twin(early, case["name"])
        assert early["framePainted"] is False
        if early["frame"]:
            assert early["frameInert"] is True and early["frameHidden"] == "true"
    if case["name"] == "slow":
        # Two and a half seconds of the twin, then the proven chat in exactly its place.
        assert_twin_matches_chat(case["twinBoxes"], case["chatBoxes"], "desktop")
        assert_used_before_ready(case["early_use"], "desktop")


def test_each_load_settles_to_the_ready_chat_or_the_native_twin(case):
    final = case["final"]
    assert case["settled_s"], case["name"]
    if case["name"] in UNAVAILABLE:
        assert final["state"] == "unavailable" and final["frame"] is False, case["name"]
        assert_complete_twin(final, case["name"])
        assert final["recorder"]["transitions"] == ["loading", "unavailable"], (case["name"], final["recorder"])
    else:
        assert final["state"] == "ready" and final["framePainted"] and final["composer"] == "present", case["name"]
        assert final["twin"] is False and final["frameInert"] is False and final["frameHidden"] is None
        assert final["recorder"]["states"] == ["loading", "ready"]
        assert final["recorder"]["transitions"] == ["loading", "ready"]


def test_the_top_level_page_never_moves_or_frames_itself(case):
    final = case["final"]
    assert final["path"] == "/" and final["docScrollWidth"] <= final["innerWidth"], case["name"]
    if case["name"] == "alias-redirect":
        # The alias host canonicalises before anything renders; the chat is same-origin there.
        assert final["state"] == "ready" and case["a_paths"][0] == "/"


def test_landing_sends_nothing_and_loads_the_chat_once(case):
    paths = case["a_paths"]
    assert not any(p.startswith("POST ") or p.startswith("/api/") for p in paths), (case["name"], paths)
    assert paths.count("/app/") <= 1, (case["name"], paths)


@pytest.fixture(scope="module")
def ready_case(tmp_path_factory):
    return run_case(tmp_path_factory, "ready")


@pytest.fixture(scope="module")
def deny_case(tmp_path_factory):
    return run_case(tmp_path_factory, "deny")


@pytest.fixture(scope="module")
def hang_case(tmp_path_factory):
    return run_case(tmp_path_factory, "hang")


def test_forged_ready_messages_reveal_nothing(hang_case):
    forged = hang_case["forged"]
    # The other-origin page really loaded and posted its messages.
    assert "/forge" in hang_case["forge_paths"]
    assert forged["state"] == "loading" and forged["framePainted"] is False
    assert_complete_twin(forged, "forged")
    assert forged["recorder"]["violations"] == []
    assert hang_case["final"]["state"] == "unavailable" and hang_case["final"]["frame"] is False


INVALID_REPLACEMENTS = ("denied", "cross-origin", "wrong", "recursive")


def assert_every_replacement_is_revalidated(result: dict):
    replaced = result["replaced"]
    for kind, run in replaced.items():
        assert run["before"]["state"] == "ready" and run["before"]["recorder"]["violations"] == [], kind
        if "after" in run:
            assert run["after"]["recorder"]["violations"] == [], (kind, run["after"]["recorder"])
    for kind in INVALID_REPLACEMENTS:
        run = replaced[kind]
        # Withdrawn as the proven document left, never ready again, then removed.
        assert run["settled"], kind
        assert run["after"]["recorder"]["transitions"] == ["loading", "ready", "loading", "unavailable"], (
            kind, run["after"]["recorder"])
        assert run["after"]["frame"] is False
        assert_complete_twin(run["after"], kind)
        # The CTA then opens the full chat at top level; it never focuses the replaced frame.
        assert run["cta"] == {"arrived": True, "path": "/app/", "framed": False}, kind
    recheck = replaced["cta-recheck"]
    assert recheck["sync"] == {"state": "unavailable", "frame": False, "focusedFrame": False}
    assert recheck["arrived"]
    reload = replaced["reload"]
    assert reload["settled"] and reload["after"]["state"] == "ready" and reload["after"]["framePainted"]
    assert reload["after"]["recorder"]["transitions"] == ["loading", "ready", "loading", "ready"]
    retired = replaced["retired"]
    assert retired["sync"] == {"state": "loading", "inert": True, "hidden": "true", "tabIndex": -1, "painted": False}
    assert retired["settled"] and retired["after"]["frame"] is False
    assert retired["after"]["recorder"]["transitions"] == ["loading", "ready", "loading", "unavailable"]


def test_a_valid_chat_keeps_its_draft_and_every_replacement_document_is_revalidated(ready_case):
    result = ready_case
    after = result["afterCta"]
    assert after == {"path": "/", "historyLength": result["final"]["historyLength"],
                     "draft": "my unsent draft", "focused": True, "posts": 0}
    reloaded = result["afterReload"]
    assert reloaded["state"] == "ready" and reloaded["recorder"]["states"] == ["loading", "ready"]
    assert reloaded["recorder"]["violations"] == [] and result["afterReloadDraft"] == ""
    assert_every_replacement_is_revalidated(result)
    assert not any(p.startswith("POST ") or p.startswith("/api/") for p in result["all_paths"])


def test_when_unavailable_the_cta_cards_and_composer_open_the_working_chat(deny_case):
    result = deny_case
    assert result["ctaArrival"] == {"path": "/app/", "framed": False, "composer": True, "usable": True}
    assert result["cardArrival"] == result["composerArrival"] == {"arrived": True, "path": "/app/"}
    assert not any(p.startswith("POST ") or p.startswith("/api/") for p in result["all_paths"])


def test_mobile_viewport_shows_the_twin_or_the_ready_chat_without_overflow(mobile_case):
    final = mobile_case["final"]
    assert final["recorder"]["violations"] == [] and final["innerWidth"] == VIEWPORTS["mobile"][0]
    assert final["docScrollWidth"] <= final["innerWidth"]
    if mobile_case["name"] == "deny":
        assert final["state"] == "unavailable"
        assert_complete_twin(final, "mobile deny")
        assert mobile_case["ctaArrival"]["path"] == "/app/"
        assert mobile_case["cardArrival"] == mobile_case["composerArrival"] == {"arrived": True, "path": "/app/"}
        slow, hang = mobile_case["phone"]["slow"], mobile_case["phone"]["hang"]
        assert_complete_twin(slow["early"], "mobile slow")
        assert slow["settled"] and slow["final"]["state"] == "ready" and slow["final"]["twin"] is False
        assert slow["final"]["recorder"]["violations"] == [] and slow["final"]["recorder"]["early"] >= 30
        assert slow["final"]["recorder"]["transitions"] == ["loading", "ready"]
        assert_twin_matches_chat(slow["twinBoxes"], slow["chatBoxes"], "mobile")
        assert_used_before_ready(slow["early_use"], "mobile")
        assert hang["settled"] and hang["final"]["state"] == "unavailable" and hang["final"]["frame"] is False
        assert_complete_twin(hang["final"], "mobile never ready")
        assert hang["final"]["recorder"]["violations"] == [] and hang["final"]["recorder"]["transitions"] == ["loading", "unavailable"]
        assert not any(p.startswith("POST ") or p.startswith("/api/") for p in mobile_case["all_paths"])
    else:
        assert final["state"] == "ready" and final["framePainted"]
        assert mobile_case["afterCta"]["draft"] == "my unsent draft" and mobile_case["afterCta"]["path"] == "/"
        assert_every_replacement_is_revalidated(mobile_case)


# --- static contract (always runs) ---------------------------------------------------------------


def test_readiness_reads_the_same_origin_chat_and_never_a_message():
    app = APP_TSX.read_text(encoding="utf-8")
    gate = app[app.index("function chatFrameStatus"):app.index("const CHAT_READY_POLL_MS")]
    for required in ("frame.contentDocument", "view.location.origin !== window.location.origin",
                     "view.location.pathname !== CHAT_PATH", "classList.contains('is-embedded')",
                     "getElementById('composer-input')", "return 'invalid';",
                     "if (retired.has(doc)) return 'pending';"):
        assert required in gate, required
    # Readiness belongs to one document and is withdrawn, synchronously, as it leaves;
    # every check belongs to a generation; the CTA re-proves the current document.
    for required in ("if (provenDoc.current) retired.current.add(provenDoc.current);",
                     "const nextGeneration = ++generationRef.current;",
                     "if (stateRef.current === 'ready') flushSync(update);",
                     "view?.addEventListener('beforeunload', leave);", "view?.addEventListener('pagehide', leave);",
                     "frame?.contentDocument === provenDoc.current",
                     "if (generationRef.current !== mine) return;",
                     "if (stateRef.current === 'ready' && provenNow()) { focusChat(); return; }",
                     "invalidate('unavailable');\n    window.location.assign(CHAT_PATH);"):
        assert required in app, required
    for forbidden in ("postMessage", "addEventListener('message'", 'addEventListener("message"', "'*'",
                      "MessageEvent", "onmessage"):
        assert forbidden not in app, forbidden
    assert "const CHAT_READY_TIMEOUT_MS = 10000;" in app and READY_TIMEOUT_S == 10
    # The child's marker is still the last setup step of the canonical chat.
    js = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert 'if (isEmbedded) document.body.classList.add("is-embedded");' in js


WORKFLOW = REPO / ".github" / "workflows" / "code-structure-frontend-verification.yml"
PERMITTED_SKIPS = ("phones reach the workspace through the CTA",
                   "the side-by-side first screen applies at 1440 x 900 and wider", "reduced-motion viewport only")


def focused_result_checker() -> str:
    """The Python the workflow runs after the focused group, taken from the workflow itself."""
    text = WORKFLOW.read_text(encoding="utf-8")
    step = text[text.index("- name: Require every focused test to have run"):]
    body = step[step.index("<<'PY'\n") + len("<<'PY'\n"):]
    return textwrap.dedent(body[:re.search(r"^\s*PY\s*$", body, re.M).start()])


def junit(passed: int, skips: list[str], failures: int = 0) -> str:
    cases = [f'<testcase classname="t" name="p{i}"/>' for i in range(passed)]
    cases += [f'<testcase classname="t" name="s{i}"><skipped message="{reason}"/></testcase>'
              for i, reason in enumerate(skips)]
    cases += [f'<testcase classname="t" name="f{i}"><failure message="x"/></testcase>' for i in range(failures)]
    total = passed + len(skips) + failures
    return (f'<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="{total}" skipped="{len(skips)}" '
            f'failures="{failures}" errors="0">{"".join(cases)}</testsuite></testsuites>')


def test_framing_protection_is_untouched_and_ci_requires_the_exact_focused_result(tmp_path):
    app, css = APP_TSX.read_text(encoding="utf-8"), SITE_CSS.read_text(encoding="utf-8")
    for source in (app, css, (SITE_DIST.parent.parent / "index.html").read_text(encoding="utf-8")):
        for header in ("X-Frame-Options", "frame-ancestors", "http-equiv", "Content-Security-Policy"):
            assert header not in source, header
    # The hidden, inert frame and the panel are the only new presentation.
    assert '.embedded-chat:not([data-chat-state="ready"]) iframe { visibility: hidden; }' in css
    assert re.search(r"inert=\{!ready\} aria-hidden=\{ready \? undefined : true\} tabIndex=\{ready \? undefined : -1\}", app)

    # The workflow accepts exactly 1504 tests = 1488 passed + 16 permitted skips, and nothing else.
    checker = focused_result_checker()
    permitted16 = [PERMITTED_SKIPS[i % 3] for i in range(16)]

    def accepted(xml: str) -> bool:
        path = tmp_path / f"result-{len(list(tmp_path.iterdir()))}.xml"
        path.write_text(xml, encoding="utf-8")
        run = subprocess.run([sys.executable, "-I", "-", str(path)], input=checker, text=True,
                             capture_output=True, timeout=60)
        return run.returncode == 0
    assert accepted(junit(1488, permitted16))
    assert not accepted(junit(1487, permitted16 + [PERMITTED_SKIPS[0]]))         # a 17th permitted skip
    assert not accepted(junit(1488, permitted16[:15] + ["some other reason"]))  # an unapproved reason
    assert not accepted(junit(1487, permitted16, failures=1))                     # a failure
