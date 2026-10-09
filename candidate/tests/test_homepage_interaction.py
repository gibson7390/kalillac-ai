"""Homepage chat workspace: one stable full-height workspace from the first
render, the starting state and its example prompts, CTA scrolling and focus,
the editorial eyebrow, pointer/caret, the chat composer's whole typing area,
and multi-turn conversations -- both inside the homepage iframe and on a
direct /app/ visit. Also the page structure below the hero (privacy diagram,
demonstrations, the two-block explanation, the mobile-app section) at every
required viewport and at 200% zoom.

Browser tests drive the BUILT site (site/dist/public) with the canonical
frontend/ chat at /app/, served from loopback only by the same harness as
test_homepage_handoff.py. Headless Edge (or another Chromium-family browser)
is controlled over the DevTools protocol: exact 1440 x 900 desktop and
390 x 844 touch-phone viewports, emulated prefers-reduced-motion, and real
(trusted) mouse clicks. Nothing leaves the machine.

Without a browser or a site build the browser tests skip (on Windows a
missing Edge fails, as in the other frontend tests). The static checks
always run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

import pytest

from test_homepage_handoff import (
    APP_TSX,
    REPO,
    SITE,
    SITE_CSS,
    VIEWPORTS,
    WORKSPACE_HEIGHT,
    Site,
    browser_page,
    require_browser_and_build,
)


# The commit this slice started from. /app/ and the backend must be byte-for-
# byte unchanged relative to it (update deliberately if /app/ later changes).
SLICE_BASE = "92a4984792ac0bfb4c1952555e08300d094ab3a0"
GAP_BELOW_HEADER = 16

# Installed once per page; every scenario step reads state through it.
HELPERS = r"""
window.__ix = {
  wait: ms => new Promise(r => setTimeout(r, ms)),
  frame: () => document.querySelector('[data-testid="embedded-chat"] iframe'),
  doc: () => window.__ix.frame().contentDocument,
  input: () => window.__ix.doc().getElementById('composer-input'),
  rect: el => { const b = el.getBoundingClientRect();
    return {left: b.left, top: b.top, right: b.right, bottom: b.bottom, width: b.width, height: b.height}; },
  // Rectangle of an element inside the chat, in top-level viewport coordinates.
  childRect: el => { const f = window.__ix.frame().getBoundingClientRect(), b = el.getBoundingClientRect();
    return {left: f.left + b.left, top: f.top + b.top, right: f.left + b.right, bottom: f.top + b.bottom,
            width: b.width, height: b.height}; },
  blurAll: () => { const d = window.__ix.doc(); if (d.activeElement) d.activeElement.blur();
    if (document.activeElement) document.activeElement.blur(); },
  focusState: () => ({ parentActive: document.activeElement === window.__ix.frame(),
    childActive: window.__ix.doc().activeElement === window.__ix.input() }),
  header: () => document.querySelector('.site-header'),
  card: () => document.querySelector('.embedded-chat-slot'),
  sample: async (ms, every) => { const out = [];
    for (let t = 0; t <= ms; t += every) { out.push(Math.round(window.scrollY)); await window.__ix.wait(every); }
    return out; },
};
true
"""


class Page:
    def __init__(self, devtools, session):
        self.dt, self.session = devtools, session

    def eval(self, body: str):
        result = self.dt.call("Runtime.evaluate", {
            "expression": f"(async () => {{ {body} }})()",
            "awaitPromise": True, "returnByValue": True}, self.session)
        assert "exceptionDetails" not in result, result["exceptionDetails"]
        return result["result"].get("value")

    def click(self, x: float, y: float):
        """A real (trusted) left click at top-level viewport coordinates."""
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.dt.call("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y, "button": "left" if kind != "mouseMoved" else "none",
                "buttons": 1 if kind == "mousePressed" else 0, "clickCount": 1}, self.session)


def run_scenario(page: Page, reduced: bool) -> dict:
    r: dict = {}
    page.eval(HELPERS.replace("\ntrue\n", "") + "return true;")
    page.eval("""
      for (let i = 0; i < 400; i++) {
        const f = window.__ix.frame();
        if (f && f.contentDocument && f.contentDocument.getElementById('composer-input')) break;
        await window.__ix.wait(25);
      }
      await window.__ix.wait(500); return true;""")

    # ---- landing ------------------------------------------------------------------
    r["landing"] = page.eval("""
      const ix = window.__ix, d = ix.doc(), conv = d.getElementById('conversation');
      const shown = sel => { const el = d.querySelector(sel); return !!el && getComputedStyle(el).display !== 'none'; };
      return {
        scrollY: window.scrollY, innerWidth, innerHeight,
        docScrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth,
        header: ix.rect(ix.header()), card: ix.rect(ix.card()),
        cta: ix.rect(document.querySelector('[data-testid="button-start-session"]')),
        trust: ix.rect(document.querySelector('.hero-under-note')),
        trustItems: [...document.querySelectorAll('.hero-under-note .trust-item')].map(e => ix.rect(e)),
        convScroll: [conv.scrollHeight, conv.clientHeight], convOverflowY: getComputedStyle(conv).overflowY,
        childDocScroll: [d.scrollingElement.scrollHeight, d.scrollingElement.clientHeight],
        titleShown: shown('.empty-title'), lineShown: shown('.empty-line'), hintsShown: shown('.empty-hints'),
        titleText: d.querySelector('.empty-title').textContent.trim(),
        lineText: d.querySelector('.empty-line').textContent.trim().replace(/\\s+/g, ' '),
        hints: [...d.querySelectorAll('.hint')].map(h => [h.textContent.trim(), h.getAttribute('data-prompt')]),
        composer: ix.childRect(d.getElementById('composer-shell')),
        messages: d.querySelectorAll('#thread .msg').length,
        input: ix.childRect(ix.input()), send: ix.childRect(d.getElementById('send-btn')),
        frame: ix.rect(ix.frame()),
        eyebrow: (() => { const e = document.querySelector('.hero .eyebrow'), s = getComputedStyle(e),
                                 b = getComputedStyle(e, '::before');
          return { text: e.textContent, tag: e.tagName, role: e.getAttribute('role'), rect: ix.rect(e),
                   children: e.children.length,
                   background: s.backgroundColor, backgroundImage: s.backgroundImage, radius: s.borderRadius,
                   padding: s.padding, borderTopWidth: s.borderTopWidth, letterSpacing: s.letterSpacing,
                   fontSize: s.fontSize, ruleContent: b.content, ruleWidth: b.width, ruleHeight: b.height,
                   ruleColor: b.backgroundColor }; })(),
        headline: ix.rect(document.querySelector('.hero h1')),
        eyebrowDots: document.querySelectorAll('.eyebrow-dot').length,
      };""")

    # ---- pointer, caret, decorative layers -------------------------------------------
    r["styles"] = page.eval("""
      const ix = window.__ix, d = ix.doc(), cs = el => getComputedStyle(el);
      const decorative = ['.orbital-wrap', '.orbital-glow', '.hero-signal-line', '.phone-stage'];
      return {
        inputCursor: cs(ix.input()).cursor, caretColor: cs(ix.input()).caretColor, inputColor: cs(ix.input()).color,
        shellBackground: cs(d.getElementById('composer-shell')).backgroundColor,
        // Send is disabled while the composer is empty (arrow cursor); with a
        // draft it is enabled and shows the pointer. The draft is then cleared.
        sendCursorEmpty: cs(d.getElementById('send-btn')).cursor,
        sendDisabledEmpty: d.getElementById('send-btn').disabled,
        sendCursor: (() => { const i = ix.input(); i.value = 'draft';
          i.dispatchEvent(new Event('input', {bubbles: true}));
          const c = [cs(d.getElementById('send-btn')).cursor, d.getElementById('send-btn').disabled];
          i.value = ''; i.dispatchEvent(new Event('input', {bubbles: true})); return c; })(),
        hintCursors: [...d.querySelectorAll('.hint')].map(h => cs(h).cursor),
        ctaCursor: cs(document.querySelector('[data-testid="button-start-session"]')).cursor,
        navCursors: [...document.querySelectorAll('.site-nav a, .site-nav button')]
          .filter(e => cs(e).display !== 'none').map(e => [e.textContent.trim(), cs(e).cursor]),
        noteLinkCursor: cs(document.querySelector('.note-link')).cursor,
        decorative: decorative.map(s => [s, [...document.querySelectorAll(s)].map(e => cs(e).pointerEvents)]),
        focusVisibleRule: [...document.styleSheets].some(sh => { try { return [...sh.cssRules].some(
          rule => (rule.cssText || '').includes(':focus-visible')); } catch (e) { return false; } }),
      };""")

    # ---- the visible typing area focuses the real input (trusted clicks) -----------------
    hits = []
    # The full workspace extends below the first screen; bring it into view.
    page.eval("""const ix = window.__ix, c = ix.card().getBoundingClientRect(), h = ix.header().getBoundingClientRect();
      window.scrollTo({top: window.scrollY + c.top - h.bottom - 16, behavior: 'instant'});
      await ix.wait(150); return true;""")
    box = page.eval("return window.__ix.childRect(window.__ix.input());")
    points = [(box["left"] + box["width"] * fx, box["top"] + box["height"] * fy)
              for fx in (0.04, 0.5, 0.96) for fy in (0.2, 0.5, 0.8)]
    for x, y in points:
        page.eval("window.__ix.blurAll(); return true;")
        hit = page.eval(f"""
          const ix = window.__ix, f = ix.frame().getBoundingClientRect();
          const top = document.elementFromPoint({x}, {y});
          const inner = ix.doc().elementFromPoint({x} - f.left, {y} - f.top);
          return {{ top: top === ix.frame() ? 'iframe' : (top ? top.className || top.tagName : null),
                   inner: inner === ix.input() ? 'composer-input' : (inner ? inner.id || inner.className : null) }};""")
        page.click(x, y)
        time.sleep(0.15)
        hit.update(page.eval("return window.__ix.focusState();"))
        hits.append(hit)
    r["typingAreaClicks"] = hits

    # ---- each example prompt fills the composer with exactly its text; nothing is sent -------
    clear = """const i = window.__ix.input(); i.value = '';
      i.dispatchEvent(new Event('input', {bubbles: true})); window.__ix.blurAll(); return true;"""
    r["hintClicks"] = []
    for index in range(4):
        page.eval(clear)
        box = page.eval(f"return window.__ix.childRect(window.__ix.doc().querySelectorAll('.hint')[{index}]);")
        page.click(box["left"] + box["width"] / 2, box["top"] + box["height"] / 2)
        time.sleep(0.2)
        r["hintClicks"].append(page.eval("""const ix = window.__ix, d = ix.doc();
          return { value: ix.input().value, messages: d.querySelectorAll('#thread .msg').length,
                   requests: (ix.frame().contentWindow.__HANDOFF_CHILD__ || {requests: []}).requests.length,
                   emptyPresent: !!d.getElementById('empty') && d.getElementById('empty').isConnected,
                   sendDisabled: d.getElementById('send-btn').disabled, path: location.pathname,
                   historyLength: history.length };"""))
    page.eval(clear)

    # ---- CTA 1: "Start a private session" from the landing position ------------------------
    page.eval("window.__ix.blurAll(); window.scrollTo({top: 0, behavior: 'instant'}); return true;")
    cta = r["landing"]["cta"]
    page.click(cta["left"] + cta["width"] / 2, cta["top"] + cta["height"] / 2)
    r["ctaFromTop"] = page.eval("""
      const ix = window.__ix, samples = await ix.sample(1800, 60);
      return { samples, header: ix.rect(ix.header()), card: ix.rect(ix.card()), ...ix.focusState() };""")

    # ---- the same CTA, off-screen, from the bottom of the page ------------------------------
    start = page.eval("""window.__ix.blurAll();
      window.scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'});
      await window.__ix.wait(150);
      return { scrollY: window.scrollY, header: window.__ix.rect(window.__ix.header()),
               historyLength: history.length };""")
    page.eval("document.querySelector('[data-testid=\"button-start-session\"]').click(); return true;")
    r["startFromBottom"] = page.eval("""
      const ix = window.__ix, samples = await ix.sample(1800, 30);
      return { samples, header: ix.rect(ix.header()), card: ix.rect(ix.card()), ...ix.focusState(),
               url: location.href, path: location.pathname, historyLength: history.length,
               composer: ix.childRect(ix.doc().getElementById('composer-shell')), innerHeight };""")
    r["startFromBottom"]["start"] = start

    r["reduced"] = reduced
    r["final"] = page.eval("""return { docScrollWidth: document.documentElement.scrollWidth, innerWidth,
      messages: window.__ix.doc().querySelectorAll('#thread .msg').length,
      requests: (window.__ix.frame().contentWindow.__HANDOFF_CHILD__ || {requests: []}).requests.length };""")
    return r


# The chat composer, addressed the same way whether it is framed by the
# homepage or is the top-level /app/ page. All coordinates are top-level.
COMPOSER_HELPERS = r"""
const frameOf = () => document.querySelector('[data-testid="embedded-chat"] iframe');
window.__cx = {
  wait: ms => new Promise(r => setTimeout(r, ms)),
  doc: () => { const f = frameOf(); return f ? f.contentDocument : document; },
  win: () => { const f = frameOf(); return f ? f.contentWindow : window; },
  off: () => { const f = frameOf(); if (!f) return {left: 0, top: 0};
    const b = f.getBoundingClientRect(); return {left: b.left, top: b.top}; },
  el: id => window.__cx.doc().getElementById(id),
  abs: el => { const o = window.__cx.off(), b = el.getBoundingClientRect();
    return {left: o.left + b.left, top: o.top + b.top, right: o.left + b.right, bottom: o.top + b.bottom,
            width: b.width, height: b.height}; },
  setValue: v => { const i = window.__cx.el('composer-input'); i.value = v;
    i.dispatchEvent(new (window.__cx.win().Event)('input', {bubbles: true})); },
  blurAll: () => { const d = window.__cx.doc(); if (d.activeElement) d.activeElement.blur();
    if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur(); },
  probe: (x, y) => { const o = window.__cx.off(), d = window.__cx.doc();
    const el = d.elementFromPoint(x - o.left, y - o.top);
    return { id: el ? (el.id || el.tagName.toLowerCase()) : null,
             cursor: el ? window.__cx.win().getComputedStyle(el).cursor : null }; },
  state: () => { const i = window.__cx.el('composer-input'), f = frameOf();
    const child = window.__cx.win().__HANDOFF_CHILD__ || {requests: []};
    return { focused: window.__cx.doc().activeElement === i && (!f || document.activeElement === f),
             value: i.value, selectionStart: i.selectionStart, selectionEnd: i.selectionEnd,
             requests: child.requests.map(r => r.body) }; },
};
return true;
"""


def run_composer_scenario(page: Page) -> dict:
    """Pointer, caret and focus behavior of the complete composer surface,
    then one real Send click (scripted /api/chat; nothing leaves the page)."""
    r: dict = {}
    page.eval(COMPOSER_HELPERS)
    page.eval("""for (let i = 0; i < 400 && !(window.__cx.doc() && window.__cx.el('composer-input')); i++)
        await window.__cx.wait(25);
      await window.__cx.wait(300); window.__cx.setValue(''); return true;""")

    r["geometry"] = page.eval("""const c = window.__cx, cs = el => c.win().getComputedStyle(el);
      return { shell: c.abs(c.el('composer-shell')), input: c.abs(c.el('composer-input')),
               send: c.abs(c.el('send-btn')),
               shellCursor: cs(c.el('composer-shell')).cursor, inputCursor: cs(c.el('composer-input')).cursor,
               caretColor: cs(c.el('composer-input')).caretColor, inputColor: cs(c.el('composer-input')).color,
               shellBackground: cs(c.el('composer-shell')).backgroundColor,
               innerWidth, docScrollWidth: document.documentElement.scrollWidth };""")
    shell, field, send = (r["geometry"][k] for k in ("shell", "input", "send"))
    mid_x, mid_y = field["left"] + field["width"] / 2, field["top"] + field["height"] / 2

    # A restrained hover cue: the box's border changes while the pointer is
    # over its padding (and never the cursor of Send).
    border = "return window.__cx.win().getComputedStyle(window.__cx.el('composer-shell')).borderTopColor;"
    page.eval("window.__cx.blurAll(); return true;")
    page.dt.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}, page.session)
    r["borderIdle"] = page.eval(border)
    page.dt.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": shell["left"] + 6, "y": mid_y}, page.session)
    time.sleep(0.25)
    r["borderHover"] = page.eval(border)
    page.dt.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1, "y": 1}, page.session)
    points = {
        "above": (mid_x, shell["top"] + 4),
        "below": (mid_x, shell["bottom"] - 4),
        "left": (shell["left"] + 6, mid_y),
        "between-field-and-send": ((field["right"] + send["left"]) / 2, send["top"] + send["height"] / 2),
    }

    # ---- padding presses focus the field and change nothing ---------------------------------
    r["padding"] = {}
    for name, (x, y) in points.items():
        before = page.eval(f"window.__cx.blurAll(); await window.__cx.wait(30); "
                           f"return {{ probe: window.__cx.probe({x}, {y}), state: window.__cx.state() }};")
        page.click(x, y)
        time.sleep(0.15)
        r["padding"][name] = {"before": before, "after": page.eval("return window.__cx.state();")}

    # With a draft: a padding press focuses without touching the text.
    page.eval("window.__cx.setValue('draft text'); window.__cx.blurAll(); return true;")
    page.click(*points["above"])
    time.sleep(0.15)
    r["paddingWithDraft"] = page.eval("return window.__cx.state();")

    # ---- the field itself keeps native caret placement --------------------------------------
    page.eval("window.__cx.setValue('hello world'); window.__cx.blurAll(); return true;")
    page.click(field["left"] + 13, mid_y)
    time.sleep(0.15)
    r["fieldClickStart"] = page.eval("return window.__cx.state();")
    page.click(field["right"] - 8, mid_y)
    time.sleep(0.15)
    r["fieldClickEnd"] = page.eval("return window.__cx.state();")

    # ---- Send is never intercepted --------------------------------------------------------------
    r["sendEnabled"] = page.eval("""window.__cx.setValue('Send test');
      const b = window.__cx.el('send-btn');
      return { disabled: b.disabled, cursor: window.__cx.win().getComputedStyle(b).cursor,
               state: window.__cx.state() };""")
    page.click(send["left"] + send["width"] / 2, send["top"] + send["height"] / 2)
    time.sleep(0.8)
    r["afterSend"] = page.eval("""return { ...window.__cx.state(),
      users: [...window.__cx.doc().querySelectorAll('#thread .msg-user .bubble')].map(b => b.textContent),
      innerWidth, docScrollWidth: document.documentElement.scrollWidth };""")
    return r


@pytest.fixture(scope="module", params=[
    ("desktop", False), ("desktop", True), ("mobile", False), ("mobile", True),
], ids=["desktop", "desktop-reduced-motion", "mobile", "mobile-reduced-motion"])
def interaction(request, tmp_path_factory):
    browser = require_browser_and_build()
    viewport, reduced = request.param
    profile = tmp_path_factory.mktemp(f"edge-interaction-{viewport}-{int(reduced)}")
    site = Site()

    with site.http:
        with browser_page(browser, profile, VIEWPORTS[viewport], viewport == "mobile",
                          reduced_motion=reduced) as (devtools, session):
            page = Page(devtools, session)
            devtools.call("Page.navigate", {"url": site.http.origin + "/"}, session)
            time.sleep(0.5)
            result = run_scenario(page, reduced)
            result["composer"] = run_composer_scenario(page)

    shutil.rmtree(profile, ignore_errors=True)
    result["viewport"], result["size"] = viewport, VIEWPORTS[viewport]
    return result


@pytest.fixture(scope="module", params=["desktop", "mobile"])
def direct_app(request, tmp_path_factory):
    """The composer scenario on a direct, top-level visit to /app/."""
    browser = require_browser_and_build()
    viewport = request.param
    profile = tmp_path_factory.mktemp(f"edge-direct-app-{viewport}")
    site = Site()
    site.child_passive = True

    with site.http:
        with browser_page(browser, profile, VIEWPORTS[viewport], viewport == "mobile") as (devtools, session):
            page = Page(devtools, session)
            devtools.call("Page.navigate", {"url": site.http.origin + "/app/"}, session)
            time.sleep(0.5)
            result = run_composer_scenario(page)
            result["path"] = page.eval("return location.pathname + location.search + location.hash;")

    shutil.rmtree(profile, ignore_errors=True)
    result["viewport"], result["size"] = viewport, VIEWPORTS[viewport]
    return result


def expected_card_top(run) -> float:
    return run["startFromBottom"]["header"]["bottom"] + GAP_BELOW_HEADER


EXAMPLE_PROMPTS = (
    "Help me reason through a difficult decision",
    "Rewrite this without losing my voice",
    "Explain why this code fails",
    "Research this and show me the sources",
)
MEMORY_SENTENCE = "Temporary session data can remain in server memory until capacity limits or a restart clear it."
BACKEND_SHA256 = "7095878b8680743e4f8a77bc928a85ab431b976239e17f400447c85e2c9d0dfb"
# Claims the homepage must never make.
FORBIDDEN_CLAIMS = ("end-to-end", "end to end", "deleted immediately", "immediately deleted",
                    "immediate deletion", "zero retention", "zero-retention", "completely private",
                    "never stored", "nothing is stored")



# A claim preceded directly by one of these words is negated, not made.
NEGATIONS = ("not", "no")


def makes_claim(text: str, claim: str) -> bool:
    """True when `text` makes `claim` affirmatively at least once.

    Each occurrence of the claim is checked on its own: it is negated only
    when the word immediately before it is "not" or "no" (for example "That
    is not immediate deletion"). Any other occurrence is an affirmative claim.
    """
    text, claim = text.lower(), claim.lower()
    start = text.find(claim)
    while start != -1:
        words_before = text[:start].split()
        if not words_before or words_before[-1] not in NEGATIONS:
            return True
        start = text.find(claim, start + 1)
    return False


@pytest.mark.parametrize("text, expected", [
    ("immediate deletion", True),
    ("We offer immediate deletion of your data.", True),
    ("not immediate deletion", False),
    ("no immediate deletion", False),
    ("That is not immediate deletion from the server.", False),
    ("There is No immediate deletion.", False),
    # One negated and one affirmative occurrence: still a claim.
    ("This is not immediate deletion, but we promise immediate deletion later.", True),
    ("Nothing here mentions it.", False),
])
def test_makes_claim_distinguishes_claims_from_negations(text, expected):
    assert makes_claim(text, "immediate deletion") is expected


# --- landing layout -------------------------------------------------------------------------------


def test_lands_at_the_top_with_the_header_visible_and_no_horizontal_overflow(interaction):
    landing = interaction["landing"]
    width, height = interaction["size"]

    assert (landing["innerWidth"], landing["innerHeight"]) == (width, height)
    assert landing["scrollY"] == 0
    assert landing["header"]["top"] == 0
    assert landing["docScrollWidth"] <= width and landing["bodyScrollWidth"] <= width
    assert interaction["final"]["docScrollWidth"] <= width


def test_initial_workspace_is_full_height_without_a_transcript_scrollbar(interaction):
    landing = interaction["landing"]

    assert landing["messages"] == 0
    # One stable full workspace from the first render: no compact state.
    assert abs(landing["card"]["height"] - WORKSPACE_HEIGHT[interaction["viewport"]]) <= 1, landing["card"]
    assert landing["card"]["height"] >= (560 if interaction["viewport"] == "desktop" else 520)
    # The intentional starting state sits in the workspace...
    assert landing["titleShown"] and landing["lineShown"] and landing["hintsShown"]
    assert landing["titleText"] == "What are you working on?"
    assert landing["lineText"] == "Bring a difficult question, a rough draft, broken code, or something current."
    assert landing["hints"] == [[p, p] for p in EXAMPLE_PROMPTS]
    # ...without overflowing it, so no empty-history scrollbar can appear.
    scroll_height, client_height = landing["convScroll"]
    assert scroll_height <= client_height, landing["convScroll"]
    assert landing["childDocScroll"][0] <= landing["childDocScroll"][1]


def test_eyebrow_is_editorial_text_with_a_short_blue_rule(interaction):
    eyebrow = interaction["landing"]["eyebrow"]

    assert eyebrow["text"] == "PRIVATE BY DESIGN" and eyebrow["children"] == 0
    # An eyebrow, not a heading.
    assert eyebrow["tag"] == "SPAN" and eyebrow["role"] is None
    assert interaction["landing"]["eyebrowDots"] == 0
    # No capsule: no background, border, rounding or padding.
    assert eyebrow["background"] == "rgba(0, 0, 0, 0)" and eyebrow["backgroundImage"] == "none"
    assert eyebrow["radius"] == "0px" and eyebrow["padding"] == "0px" and eyebrow["borderTopWidth"] == "0px"
    # One short blue rule beside the text.
    assert eyebrow["ruleContent"] == '""' and eyebrow["ruleHeight"] == "2px"
    assert eyebrow["ruleWidth"] in ("28px", "22px") and eyebrow["ruleColor"] == "rgb(50, 102, 216)"
    assert float(eyebrow["fontSize"].rstrip("px")) >= 10 and float(eyebrow["letterSpacing"].rstrip("px")) > 0
    # Aligned with the headline's left edge.
    assert abs(eyebrow["rect"]["left"] - interaction["landing"]["headline"]["left"]) <= 1


def test_example_prompts_fill_the_composer_and_never_send(interaction):
    clicks = interaction["hintClicks"]

    assert [c["value"] for c in clicks] == list(EXAMPLE_PROMPTS)
    for click in clicks:
        assert click["messages"] == 0 and click["requests"] == 0, click
        assert click["emptyPresent"] is True and click["sendDisabled"] is False, click
        assert click["path"] == "/" and click["historyLength"] == clicks[0]["historyLength"], click


def test_complete_composer_is_visible_on_the_first_desktop_screen(interaction):
    if interaction["viewport"] != "desktop":
        pytest.skip("phones reach the workspace through the CTA")
    landing = interaction["landing"]

    assert landing["scrollY"] == 0
    assert landing["card"]["top"] >= landing["header"]["bottom"]
    assert landing["composer"]["bottom"] <= landing["card"]["bottom"] <= landing["innerHeight"], landing["composer"]


def test_composer_controls_are_fully_visible_and_tappable(interaction):
    landing = interaction["landing"]
    frame, field, send = landing["frame"], landing["input"], landing["send"]

    for box in (field, send):
        assert frame["left"] <= box["left"] and box["right"] <= frame["right"]
        assert frame["top"] <= box["top"] and box["bottom"] <= frame["bottom"]
    assert field["height"] >= 36 and send["width"] >= 36 and send["height"] >= 36
    if interaction["viewport"] == "mobile":
        assert landing["cta"]["height"] >= 44                      # comfortable tap target
        assert landing["cta"]["width"] < interaction["size"][0] * 0.75   # not full-width


def test_trust_line_statements_stay_whole_on_every_viewport(interaction):
    landing = interaction["landing"]
    items = landing["trustItems"]
    single_line = max(i["height"] for i in items)

    assert len(items) == 3
    for item in items:
        # Each statement is one line (never a narrow multi-line column) and
        # inside the viewport.
        assert item["height"] <= single_line + 1 and item["height"] < 24
        assert item["left"] >= 0 and item["right"] <= interaction["size"][0]


# --- pointer and caret ---------------------------------------------------------------------------


def luminance(rgb: str) -> float:
    r, g, b = (int(v) / 255 for v in re.findall(r"\d+", rgb)[:3])
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def test_pointer_and_caret_styles(interaction):
    styles = interaction["styles"]

    assert styles["inputCursor"] == "text"
    caret = styles["caretColor"]
    assert caret.startswith("rgb") and "rgba(0, 0, 0, 0)" not in caret
    # The caret stands out clearly against the white composer background.
    light, dark = sorted((luminance(caret), luminance(styles["shellBackground"])))
    assert (dark + 0.05) / (light + 0.05) >= 7, (caret, styles["shellBackground"])
    assert styles["sendCursor"] == ["pointer", False]               # enabled Send
    assert styles["sendDisabledEmpty"] is True and styles["sendCursorEmpty"] == "default"
    assert styles["hintCursors"] and set(styles["hintCursors"]) == {"pointer"}
    assert styles["ctaCursor"] == "pointer" and styles["noteLinkCursor"] == "pointer"
    for label, cursor in styles["navCursors"]:
        assert cursor == "pointer", label
    for selector, values in styles["decorative"]:
        assert values and set(values) == {"none"}, selector
    assert styles["focusVisibleRule"]


def test_clicking_anywhere_in_the_visible_input_focuses_the_real_field(interaction):
    clicks = interaction["typingAreaClicks"]

    assert len(clicks) == 9
    for hit in clicks:
        # Nothing on the homepage covers the chat, and inside the chat the
        # point is the textarea itself, not an overlay.
        assert hit["top"] == "iframe" and hit["inner"] == "composer-input", hit
        assert hit["parentActive"] and hit["childActive"], hit


# --- CTAs ----------------------------------------------------------------------------------------


def test_cta_from_the_landing_position_focuses_the_composer(interaction):
    run, landing = interaction["ctaFromTop"], interaction["landing"]
    fully_visible = (landing["card"]["top"] >= landing["header"]["bottom"]
                     and landing["card"]["bottom"] <= landing["innerHeight"])

    if fully_visible:
        # Desktop: the workspace is already completely in view beside the
        # headline, so the CTA only focuses the real input; the page stays put.
        assert interaction["viewport"] == "desktop"
        assert set(run["samples"]) == {0}, run["samples"][:10]
        assert abs(run["card"]["top"] - landing["card"]["top"]) <= 1
    else:
        # Phones: the workspace sits below the hero copy, so the CTA moves it
        # just below the sticky header.
        assert interaction["viewport"] == "mobile"
        assert run["samples"][-1] > 0                               # the page moved
        assert run["samples"][-6:] == [run["samples"][-1]] * 6       # and settled
        assert abs(run["card"]["top"] - expected_card_top(interaction)) <= 1, run["card"]
    assert run["header"]["top"] == 0
    assert run["parentActive"] and run["childActive"]


def test_cta_from_the_bottom_brings_the_composer_just_below_the_header_and_focuses_it(interaction):
    run = interaction["startFromBottom"]
    samples = run["samples"]
    final = samples[-1]

    assert run["header"]["top"] == 0                               # sticky header stays visible
    assert abs(run["card"]["top"] - expected_card_top(interaction)) <= 1, run["card"]
    assert run["composer"]["bottom"] <= run["innerHeight"]          # the whole composer is in view
    assert run["parentActive"] and run["childActive"]
    # Settled, with no second browser-generated jump after focusing.
    assert samples[-12:] == [final] * 12
    # The conversation stays on the homepage: no hash, no /app/, no new history entry.
    assert "#" not in run["url"] and run["path"] == "/"
    assert run["historyLength"] == run["start"]["historyLength"]


def test_scroll_is_smooth_or_immediate_according_to_motion_preference(interaction):
    samples = interaction["startFromBottom"]["samples"]
    start, final = interaction["startFromBottom"]["start"]["scrollY"], samples[-1]
    assert start - final > 200                                      # a real distance was travelled

    if interaction["reduced"]:
        # Immediate: already at the destination at the first sample.
        assert samples[0] == final, samples[:5]
    else:
        # Smooth: intermediate positions strictly between start and end.
        intermediate = [s for s in samples if final < s < start]
        assert len(intermediate) >= 2, samples[:20]


def test_nothing_was_sent_by_the_interactions(interaction):
    assert interaction["final"]["messages"] == 0
    assert interaction["final"]["requests"] == 0


# --- page structure: hero, privacy diagram, demonstrations, explanation, mobile app ---------------

STRUCTURE = r"""
for (let i = 0; i < 400; i++) {
  const f = document.querySelector('[data-testid="embedded-chat"] iframe');
  if (f && f.contentDocument && f.contentDocument.getElementById('composer-input')) break;
  await new Promise(r => setTimeout(r, 25));
}
await new Promise(r => setTimeout(r, 400));
const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)];
const rect = el => { const b = el.getBoundingClientRect();
  return {left: b.left, top: b.top + scrollY, right: b.right, bottom: b.bottom + scrollY, width: b.width,
          height: b.height}; };
const visible = el => { const s = getComputedStyle(el), b = el.getBoundingClientRect();
  return s.display !== 'none' && s.visibility !== 'hidden' && b.width > 0 && b.height > 0; };
const f = q('[data-testid="embedded-chat"] iframe'), d = f.contentDocument;
const slot = q('.embedded-chat-slot').getBoundingClientRect(), shell = d.getElementById('composer-shell').getBoundingClientRect();
const svgs = qa('.flow-svg').filter(visible);
const svg = svgs[0];
const alpha = c => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return 0; const p = m[1].split(',');
  return p.length > 3 ? parseFloat(p[3]) : 1; };
const order = ['section-hero', 'section-flow', 'section-demos', 'section-explain', 'section-mobile-app']
  .map(id => q(`[data-testid="${id}"]`));
return {
  innerWidth, innerHeight, dpr: devicePixelRatio,
  docScrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth,
  h1s: qa('h1').map(h => h.textContent.replace(/\s+/g, ' ').trim()),
  heroEyebrow: (() => { const e = q('.hero .eyebrow'); return [e.tagName, e.textContent]; })(),
  filled: qa('a, button').filter(visible).filter(e => alpha(getComputedStyle(e).backgroundColor) > 0.5
      && getComputedStyle(e).backgroundColor !== 'rgb(255, 255, 255)')
    .map(e => e.textContent.trim()),
  navLinks: qa('.site-nav a').filter(visible).map(a => [a.textContent.trim(), a.getAttribute('href')]),
  composerTopInViewport: Math.round(slot.top + shell.top), composerBottomInViewport: Math.round(slot.top + shell.bottom),
  slotHeight: Math.round(slot.height),
  sectionsInOrder: order.every((el, i) => el && (i === 0 ||
    (order[i - 1].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING))),
  footerLast: !!q('.site-footer') && !!(order[4].compareDocumentPosition(q('.site-footer')) & Node.DOCUMENT_POSITION_FOLLOWING),
  sectionTops: order.map(el => Math.round(rect(el).top)),
  visibleSvgs: svgs.map(s => s.getAttribute('data-testid')),
  svg: svg ? {
    role: svg.getAttribute('role'),
    title: (svg.querySelector('title') || {}).textContent, titleId: (svg.querySelector('title') || {}).id,
    labelledby: svg.getAttribute('aria-labelledby'), describedby: svg.getAttribute('aria-describedby'),
    desc: (svg.querySelector('desc') || {}).textContent, descId: (svg.querySelector('desc') || {}).id,
    stages: [...svg.querySelectorAll('.flow-stage')].map(g => [g.getAttribute('data-stage'),
      [...g.querySelectorAll('text')].map(t => t.textContent).join(' ')]),
    text: [...svg.querySelectorAll('text')].map(t => t.textContent).join(' '),
    searchPaths: [...svg.querySelectorAll('.flow-path-search')].map(p => [getComputedStyle(p).strokeDasharray,
      getComputedStyle(p).stroke]),
    answerPaths: [...svg.querySelectorAll('.flow-path')].map(p => getComputedStyle(p).strokeDasharray),
    rect: rect(svg), minTextHeight: Math.min(...[...svg.querySelectorAll('text')].map(t => t.getBoundingClientRect().height)),
    overflowsPanel: rect(svg).right > rect(q('.flow-panel')).right + 1 || rect(svg).left < rect(q('.flow-panel')).left - 1,
  } : null,
  summary: qa('[data-testid="flow-summary"] li').map(li => li.textContent.replace(/\s+/g, ' ').trim()),
  note: (() => { const n = q('[data-testid="flow-memory-note"]'); return { text: n.textContent.replace(/\s+/g, ' ').trim(),
    rect: rect(n), visible: visible(n), fontSize: parseFloat(getComputedStyle(n).fontSize) }; })(),
  flowHeading: [q('#how-it-works h2').textContent.trim(), q('#how-it-works .eyebrow').textContent.trim()],
  demos: qa('[data-testid="section-demos"] [data-testid^="demo-"]').map(e => e.getAttribute('data-testid')),
  demoText: Object.fromEntries(qa('[data-testid^="demo-"]').map(e => [e.getAttribute('data-testid'),
    e.textContent.replace(/\s+/g, ' ')])),
  demosHeading: q('#product h2').textContent.trim(),
  explainBlocks: qa('[data-testid="section-explain"] .explain-block').map(b => [b.getAttribute('data-testid'),
    b.querySelector('h3').textContent.trim(), b.textContent.replace(/\s+/g, ' ').trim()]),
  privacyLink: (q('[data-testid="link-full-privacy"]') || {}).getAttribute
    ? q('[data-testid="link-full-privacy"]').getAttribute('href') : null,
  mobileApp: q('[data-testid="section-mobile-app"]').innerText.replace(/\s+/g, ' '),
  pageText: document.body.innerText.replace(/\s+/g, ' '),
  animations: document.getAnimations().filter(a => a.playState === 'running').length,
  path: location.pathname, href: location.href,
};
"""

# name: (size, phone, reduced motion, real browser zoom percent or None).
STRUCTURE_VIEWPORTS = {
    "1920x1080": ((1920, 1080), False, False, None),
    "1440x900": ((1440, 900), False, False, None),
    "1440x900-reduced-motion": ((1440, 900), False, True, None),
    "1024x768": ((1024, 768), False, False, None),
    "390x844": ((390, 844), True, False, None),
    "360x800": ((360, 800), True, False, None),
    # A 1440 x 900 browser window at the browser's own 200% page zoom (no emulation).
    "1440x900-window-zoom200": ((1440, 900), False, False, 200),
}


@pytest.fixture(scope="module", params=list(STRUCTURE_VIEWPORTS))
def structure(request, tmp_path_factory):
    browser = require_browser_and_build()
    size, mobile, reduced, zoom = STRUCTURE_VIEWPORTS[request.param]
    profile = tmp_path_factory.mktemp(f"edge-structure-{request.param}")
    site = Site()

    with site.http:
        with browser_page(browser, profile, size, mobile, reduced_motion=reduced,
                          zoom_percent=zoom) as (devtools, session):
            page = Page(devtools, session)
            devtools.call("Page.navigate", {"url": site.http.origin + "/"}, session)
            time.sleep(0.5)
            result = page.eval(STRUCTURE)
            result["requests"] = page.eval("""const f = document.querySelector('[data-testid="embedded-chat"] iframe');
              return (f.contentWindow.__HANDOFF_CHILD__ || {requests: []}).requests.length;""")

    shutil.rmtree(profile, ignore_errors=True)
    result.update(name=request.param, size=size, mobile=mobile, reduced=reduced, zoom=zoom)
    return result


def test_structure_has_one_h1_and_one_filled_call_to_action(structure):
    assert structure["h1s"] == ["Think clearly. Write well. Build and debug. Search the current web."]
    assert structure["heroEyebrow"] == ["SPAN", "PRIVATE BY DESIGN"]
    assert [t.replace("\u00a0", " ") for t in structure["filled"]] == ["Start a private session"], structure["filled"]
    if not structure["mobile"] and structure["innerWidth"] >= 1024:
        assert structure["navLinks"] == [["Product", "/#product"], ["Privacy", "/#privacy"],
                                         ["How it works", "/#how-it-works"]]


def test_structure_sections_are_in_the_required_order(structure):
    assert structure["sectionsInOrder"] and structure["footerLast"]
    tops = structure["sectionTops"]
    assert tops == sorted(tops), tops


def test_structure_has_no_horizontal_overflow(structure):
    width = structure["innerWidth"]
    if structure["zoom"]:
        # Real 200% zoom: half the window's CSS width, two device pixels per CSS pixel.
        assert structure["dpr"] == 2 and width <= structure["size"][0] / 2, (width, structure["dpr"])
    else:
        assert (width, structure["dpr"]) == (structure["size"][0], 1)
    assert structure["docScrollWidth"] <= width and structure["bodyScrollWidth"] <= width


def test_structure_desktop_first_screen_shows_the_complete_composer(structure):
    if structure["innerWidth"] < 1440:
        pytest.skip("the side-by-side first screen applies at 1440 x 900 and wider")
    assert structure["composerBottomInViewport"] <= structure["innerHeight"], structure
    assert structure["slotHeight"] >= 560                           # not shrunk to fit


def test_privacy_diagram_has_four_stages_and_an_accessible_name(structure):
    svg = structure["svg"]
    wide = structure["innerWidth"] >= 1180

    assert structure["visibleSvgs"] == ["flow-svg-wide" if wide else "flow-svg-narrow"]
    assert svg["role"] == "img" and svg["labelledby"] == svg["titleId"] and svg["describedby"] == svg["descId"]
    assert svg["title"] == "How a Kalillac conversation moves" and "four stages" in svg["desc"]
    stages = svg["stages"]
    assert [s[0] for s in stages] == ["1", "2", "3", "4"]
    expected = [("YOU ASK", "No account or profile required."),
                ("TEMPORARY SESSION", "Kalillac keeps the current conversation context in server memory."),
                ("ANSWER OR SEARCH", "OpenAI produces the answer."),
                ("YOU MOVE ON", "Refreshing or leaving ends this browser\u2019s access to the conversation.")]
    for (_, text), (title, body) in zip(stages, expected):
        assert text.startswith(title + " ") and body in text, text
    assert "Tavily searches when current web information is needed." in " ".join(stages[2][1].split())
    # The conditional Tavily branch is dashed and teal; the normal path is solid.
    assert svg["searchPaths"], svg
    for dash, stroke in svg["searchPaths"]:
        assert dash not in ("none", "") and stroke == "rgb(53, 201, 167)", (dash, stroke)
    assert svg["answerPaths"] and set(svg["answerPaths"]) == {"none"}, svg["answerPaths"]
    assert "Answers return to you" in svg["text"]                   # the return path to the visitor
    assert not svg["overflowsPanel"]
    # Readable at every width, including 200% zoom (text height in CSS pixels).
    assert svg["minTextHeight"] >= 11, svg["minTextHeight"]


def test_privacy_diagram_has_a_semantic_summary_and_the_memory_sentence_beneath(structure):
    assert structure["flowHeading"] == ["Your conversation has boundaries.", "PRIVATE BY STRUCTURE"]
    summary = structure["summary"]
    assert len(summary) == 4
    assert "OpenAI" in summary[2] and "Tavily" in summary[2] and "when" in summary[2]
    note = structure["note"]
    assert note["visible"] and note["text"].startswith(MEMORY_SENTENCE)
    assert note["rect"]["top"] >= structure["svg"]["rect"]["bottom"]     # directly underneath
    assert note["fontSize"] >= 14


def test_demonstrations_are_four_distinct_compositions(structure):
    assert structure["demosHeading"] == "See Kalillac at work."
    assert structure["demos"] == ["demo-think", "demo-write", "demo-code", "demo-search"]
    text = structure["demoText"]
    assert "What you know" in text["demo-think"] and "assuming" in text["demo-think"]
    assert "def add_tag(tag, tags=[])" in text["demo-code"] and "tags=None" in text["demo-code"]
    assert "Searched the current web" in text["demo-search"]
    assert len(set(text.values())) == 4


def test_explanation_has_exactly_two_blocks_with_the_required_meaning(structure):
    blocks = structure["explainBlocks"]
    assert [b[:2] for b in blocks] == [["explain-session", "Your temporary session"],
                                       ["explain-providers", "Who processes what"]]
    session, providers = blocks[0][2], blocks[1][2]
    for phrase in ("doesn\u2019t require an account", "permanent chat history", "temporary session",
                   "Refreshing or leaving the page ends", MEMORY_SENTENCE):
        assert phrase.replace("\u2019", "'") in session.replace("\u2019", "'"), phrase
    assert "OpenAI produces the answer." in providers and "Tavily searches when" in providers
    assert structure["privacyLink"] == "/privacy"


def test_mobile_app_section_follows_the_explanation(structure):
    assert "Kalillac AI app in development." in structure["mobileApp"]
    assert "No release date announced" in structure["mobileApp"]
    assert structure["sectionTops"][4] > structure["sectionTops"][3]


def test_page_makes_no_absolute_privacy_claims(structure):
    text = structure["pageText"].lower()
    for claim in FORBIDDEN_CLAIMS:
        assert not makes_claim(text, claim), claim


def test_structure_loads_without_sending_or_navigating(structure):
    assert structure["requests"] == 0
    assert structure["path"] == "/" and "#" not in structure["href"]


def test_reduced_motion_leaves_nothing_animating(structure):
    if not structure["reduced"]:
        pytest.skip("reduced-motion viewport only")
    assert structure["animations"] == 0


# --- real 200% browser zoom ------------------------------------------------------------------------
#
# The browser's own page zoom (the profile zoom level that Ctrl+Plus and
# Settings > Page zoom set), in a 1440 x 900 window, with no viewport or
# device-pixel emulation. Driven with real key presses.

ZOOM_CLIPPING = r"""
// Visible elements outside the viewport horizontally, ignoring content inside
// a deliberate horizontal scroller (the code demos), visually hidden text and
// inert decoration.
const scrollers = [...document.querySelectorAll('*')].filter(e => e !== document.documentElement
  && e !== document.body && ['auto', 'scroll'].includes(getComputedStyle(e).overflowX));
const inScroller = el => scrollers.some(s => s !== el && s.contains(el));
const name = el => el.tagName + '.' + (typeof el.className === 'string' ? el.className : el.className.baseVal);
const out = [];
for (const el of document.body.querySelectorAll('*')) {
  const s = getComputedStyle(el), b = el.getBoundingClientRect();
  if (s.display === 'none' || s.visibility === 'hidden' || b.width === 0 || b.height === 0) continue;
  if (el.closest('.sr-only, [aria-hidden="true"], .orbital-wrap, .hero-signal-line, .phone-stage')) continue;
  if (s.pointerEvents === 'none' && s.position === 'absolute') continue;
  if (inScroller(el)) continue;
  if (b.left < -1 || b.right > innerWidth + 1) out.push(name(el) + ' [' + Math.round(b.left) + ', ' + Math.round(b.right) + ']');
}
return out.slice(0, 20);
"""


def press(page: Page, key: str, code: str, vk: int, text: str | None = None):
    down = {"type": "keyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk}
    if text is not None:
        down["text"] = text
    page.dt.call("Input.dispatchKeyEvent", down, page.session)
    page.dt.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code,
                                            "windowsVirtualKeyCode": vk}, page.session)


@pytest.fixture(scope="module")
def real_zoom(tmp_path_factory):
    browser = require_browser_and_build()
    profile = tmp_path_factory.mktemp("edge-real-zoom-200")
    site = Site()
    r: dict = {}

    with site.http:
        with browser_page(browser, profile, (1440, 900), False, zoom_percent=200) as (devtools, session):
            page = Page(devtools, session)
            devtools.call("Page.navigate", {"url": site.http.origin + "/"}, session)
            time.sleep(0.5)
            page.eval(HELPERS.replace("\ntrue\n", "") + "return true;")
            page.eval("""for (let i = 0; i < 400; i++) { const f = window.__ix.frame();
                if (f && f.contentDocument && f.contentDocument.getElementById('composer-input')) break;
                await window.__ix.wait(25); }
              await window.__ix.wait(500); return true;""")
            r["viewport"] = page.eval("""return { innerWidth, innerHeight, dpr: devicePixelRatio,
              outer: [outerWidth, outerHeight], pinch: visualViewport.scale,
              docScrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth };""")
            # Sideways scrolling is impossible: a horizontal scroll request has no effect.
            r["scrollX"] = page.eval("window.scrollTo(400, 0); await window.__ix.wait(100); "
                                     "const x = window.scrollX; window.scrollTo(0, 0); return x;")
            r["clipped"] = {}
            height = page.eval("return document.documentElement.scrollHeight;")
            for y in range(0, height, 300):
                page.eval(f"window.scrollTo(0, {y}); await window.__ix.wait(30); return true;")
                for item in page.eval(ZOOM_CLIPPING):
                    r["clipped"].setdefault(item, y)
            page.eval("window.scrollTo(0, 0); window.__ix.blurAll(); return true;")

            # Keyboard only: Tab to the primary action; every focus ring must show.
            r["tabStops"] = []
            for _ in range(25):
                press(page, "Tab", "Tab", 9)
                time.sleep(0.1)
                stop = page.eval("""const e = document.activeElement, s = getComputedStyle(e);
                  return { text: (e.textContent || '').trim().slice(0, 40), testid: e.getAttribute('data-testid'),
                           outlineStyle: s.outlineStyle, outlineWidth: parseFloat(s.outlineWidth),
                           rect: window.__ix.rect(e), innerHeight, innerWidth };""")
                r["tabStops"].append(stop)
                if stop["testid"] == "button-start-session":
                    break
            press(page, "Enter", "Enter", 13, text="\r")
            time.sleep(1.6)
            r["afterCta"] = page.eval("""const ix = window.__ix;
              return { ...ix.focusState(), composer: ix.childRect(ix.doc().getElementById('composer-shell')),
                       header: ix.rect(ix.header()), innerHeight, innerWidth, path: location.pathname,
                       url: location.href };""")

            # The composer is usable: type and send with the keyboard (local stub reply).
            devtools.call("Input.insertText", {"text": "Zoom check"}, session)
            press(page, "Enter", "Enter", 13, text="\r")
            time.sleep(1.2)
            r["afterSend"] = page.eval("""const ix = window.__ix, d = ix.doc();
              return { users: [...d.querySelectorAll('#thread .msg-user .bubble')].map(b => b.textContent),
                       assistants: d.querySelectorAll('#thread .msg-assistant').length,
                       requests: (ix.frame().contentWindow.__HANDOFF_CHILD__ || {requests: []}).requests
                         .map(q => q.body.message),
                       value: ix.input().value, childActive: d.activeElement === ix.input(),
                       composer: ix.childRect(d.getElementById('composer-shell')), frame: ix.rect(ix.frame()),
                       innerHeight, innerWidth, path: location.pathname,
                       docScrollWidth: document.documentElement.scrollWidth };""")

            # The diagram at 200%.
            r["diagram"] = page.eval("""const svg = [...document.querySelectorAll('.flow-svg')]
                .filter(s => getComputedStyle(s).display !== 'none')[0];
              const panel = document.querySelector('.flow-panel').getBoundingClientRect(), b = svg.getBoundingClientRect();
              const note = document.querySelector('[data-testid="flow-memory-note"]'), n = note.getBoundingClientRect();
              return { testid: svg.getAttribute('data-testid'), left: b.left, right: b.right,
                       panel: [panel.left, panel.right], innerWidth,
                       texts: [...svg.querySelectorAll('text')].map(x => { const t = x.getBoundingClientRect();
                         return [x.textContent, t.left, t.right, t.height]; }),
                       note: note.textContent.trim(), noteRight: n.right };""")

    shutil.rmtree(profile, ignore_errors=True)
    return r


def test_real_zoom_is_the_browsers_own_200_percent_page_zoom(real_zoom):
    v = real_zoom["viewport"]
    # The window stays 1440 x 900; the browser's zoom halves the CSS viewport
    # and doubles device pixels per CSS pixel. It is not pinch zoom.
    assert v["outer"] == [1440, 900] and v["dpr"] == 2 and v["pinch"] == 1, v
    assert 600 <= v["innerWidth"] <= 720, v


def test_real_zoom_has_no_horizontal_clipping_or_sideways_scrolling(real_zoom):
    v = real_zoom["viewport"]
    assert v["docScrollWidth"] <= v["innerWidth"] and v["bodyScrollWidth"] <= v["innerWidth"], v
    assert real_zoom["scrollX"] == 0
    assert real_zoom["clipped"] == {}, real_zoom["clipped"]
    assert real_zoom["afterSend"]["docScrollWidth"] <= real_zoom["afterSend"]["innerWidth"]


def test_real_zoom_primary_action_is_keyboard_reachable_with_visible_focus(real_zoom):
    stops = real_zoom["tabStops"]
    assert stops[-1]["testid"] == "button-start-session", [s["text"] for s in stops]
    for stop in stops:
        # Every focused control shows a visible ring and is on screen.
        assert stop["outlineStyle"] != "none" and stop["outlineWidth"] >= 2, stop
        box = stop["rect"]
        assert box["top"] >= 0 and box["bottom"] <= stop["innerHeight"] and box["right"] <= stop["innerWidth"] + 1, stop


def test_real_zoom_cta_focuses_a_visible_composer_and_stays_on_the_homepage(real_zoom):
    after = real_zoom["afterCta"]
    assert after["parentActive"] and after["childActive"]
    composer = after["composer"]
    assert composer["top"] >= after["header"]["bottom"] - 1 and composer["bottom"] <= after["innerHeight"] + 1, after
    assert composer["left"] >= 0 and composer["right"] <= after["innerWidth"] + 1
    assert after["path"] == "/" and "#" not in after["url"]


def test_real_zoom_composer_sends_and_stays_usable(real_zoom):
    after = real_zoom["afterSend"]
    assert after["requests"] == ["Zoom check"] and after["users"] == ["Zoom check"]
    assert after["assistants"] == 1 and after["value"] == "" and after["childActive"]
    composer, frame = after["composer"], after["frame"]
    assert frame["top"] <= composer["top"] and composer["bottom"] <= frame["bottom"] + 1
    assert composer["right"] <= after["innerWidth"] + 1 and after["path"] == "/"


def test_real_zoom_privacy_diagram_stays_readable(real_zoom):
    d = real_zoom["diagram"]
    assert d["testid"] == "flow-svg-narrow"                       # the vertical layout
    assert d["panel"][0] - 1 <= d["left"] and d["right"] <= d["panel"][1] + 1 <= d["innerWidth"] + 2
    for text, left, right, height in d["texts"]:
        assert d["panel"][0] - 1 <= left and right <= d["panel"][1] + 1, text
        assert height >= 11, (text, height)                      # CSS pixels; shown at 2x
    assert d["note"].startswith(MEMORY_SENTENCE)
    assert d["noteRight"] <= d["innerWidth"] + 1


# --- the whole composer typing area (homepage iframe and direct /app/) -----------------------------


@pytest.fixture
def homepage_composer(interaction):
    return {**interaction["composer"], "size": interaction["size"]}


@pytest.fixture
def app_composer(direct_app):
    assert direct_app["path"] == "/app/"
    return direct_app


def check_composer_padding_shows_text_cursor_and_focuses_the_field(composer):
    for name, result in composer["padding"].items():
        before, after = result["before"], result["after"]
        # The point really is the composer's own padding, and it shows the
        # text cursor.
        assert before["probe"] == {"id": "composer-shell", "cursor": "text"}, (name, before)
        assert before["state"]["focused"] is False, name
        # The press focused the real field and changed nothing else.
        assert after["focused"] is True, (name, after)
        assert after["value"] == "" and after["requests"] == [], (name, after)


def check_padding_press_keeps_a_draft_unchanged(composer):
    state = composer["paddingWithDraft"]

    assert state["focused"] is True
    assert state["value"] == "draft text"
    assert state["requests"] == []


def check_clicking_the_field_keeps_native_caret_placement(composer):
    start, end = composer["fieldClickStart"], composer["fieldClickEnd"]

    assert start["focused"] and end["focused"]
    assert start["value"] == end["value"] == "hello world"
    assert (start["selectionStart"], start["selectionEnd"]) == (0, 0)
    assert (end["selectionStart"], end["selectionEnd"]) == (11, 11)
    assert end["requests"] == []


def check_send_click_is_not_intercepted(composer):
    enabled, after = composer["sendEnabled"], composer["afterSend"]

    assert enabled["disabled"] is False and enabled["cursor"] == "pointer"
    assert enabled["state"]["requests"] == []
    # One real send of the draft, exactly once; the field is cleared by send.
    assert after["requests"] == [{"message": "Send test", "history": [], "session_id": None}]
    assert after["users"] == ["Send test"]
    assert after["value"] == ""


def check_composer_cursor_and_caret_are_explicit_and_visible(composer):
    geometry = composer["geometry"]

    assert geometry["shellCursor"] == "text" and geometry["inputCursor"] == "text"
    assert geometry["caretColor"] == geometry["inputColor"] and geometry["caretColor"].startswith("rgb(")
    light, dark = sorted((luminance(geometry["caretColor"]), luminance(geometry["shellBackground"])))
    assert (dark + 0.05) / (light + 0.05) >= 7


def check_composer_box_has_a_hover_cue(composer):
    assert composer["borderIdle"] != composer["borderHover"], (composer["borderIdle"], composer["borderHover"])


def check_composer_pages_have_no_horizontal_overflow(composer):
    for snapshot in (composer["geometry"], composer["afterSend"]):
        assert snapshot["docScrollWidth"] <= snapshot["innerWidth"] == composer["size"][0]


def test_composer_padding_shows_text_cursor_and_focuses_the_field_in_homepage_iframe(homepage_composer):
    check_composer_padding_shows_text_cursor_and_focuses_the_field(homepage_composer)


def test_composer_padding_shows_text_cursor_and_focuses_the_field_on_direct_app(app_composer):
    check_composer_padding_shows_text_cursor_and_focuses_the_field(app_composer)


def test_padding_press_keeps_a_draft_unchanged_in_homepage_iframe(homepage_composer):
    check_padding_press_keeps_a_draft_unchanged(homepage_composer)


def test_padding_press_keeps_a_draft_unchanged_on_direct_app(app_composer):
    check_padding_press_keeps_a_draft_unchanged(app_composer)


def test_clicking_the_field_keeps_native_caret_placement_in_homepage_iframe(homepage_composer):
    check_clicking_the_field_keeps_native_caret_placement(homepage_composer)


def test_clicking_the_field_keeps_native_caret_placement_on_direct_app(app_composer):
    check_clicking_the_field_keeps_native_caret_placement(app_composer)


def test_send_click_is_not_intercepted_in_homepage_iframe(homepage_composer):
    check_send_click_is_not_intercepted(homepage_composer)


def test_send_click_is_not_intercepted_on_direct_app(app_composer):
    check_send_click_is_not_intercepted(app_composer)


def test_composer_cursor_and_caret_are_explicit_and_visible_in_homepage_iframe(homepage_composer):
    check_composer_cursor_and_caret_are_explicit_and_visible(homepage_composer)


def test_composer_cursor_and_caret_are_explicit_and_visible_on_direct_app(app_composer):
    check_composer_cursor_and_caret_are_explicit_and_visible(app_composer)


def test_composer_pages_have_no_horizontal_overflow_in_homepage_iframe(homepage_composer):
    check_composer_pages_have_no_horizontal_overflow(homepage_composer)


def test_composer_pages_have_no_horizontal_overflow_on_direct_app(app_composer):
    check_composer_pages_have_no_horizontal_overflow(app_composer)


def test_composer_box_has_a_hover_cue_in_homepage_iframe(homepage_composer):
    check_composer_box_has_a_hover_cue(homepage_composer)


def test_composer_box_has_a_hover_cue_on_direct_app(app_composer):
    check_composer_box_has_a_hover_cue(app_composer)


def test_padding_handler_is_scoped_and_never_intercepts_controls():
    source = (REPO / "frontend" / "app.js").read_text(encoding="utf-8")
    css = (REPO / "frontend" / "app.css").read_text(encoding="utf-8")
    handler = source[source.index('composerShell.addEventListener("mousedown"'):]
    handler = handler[: handler.index("});") + 3]

    assert 'var composerShell = document.getElementById("composer-shell");' in source
    assert "document.addEventListener(\"mousedown\"" not in source and "document.addEventListener('click'" not in source
    assert "if (target && target.closest && target.closest(COMPOSER_CONTROLS)) return;" in handler
    assert "input.focus({ preventScroll: true });" in handler
    for forbidden in (".click()", "dispatchEvent", "send(", "input.value"):
        assert forbidden not in handler, forbidden
    for control in ("button", "a[href]", "input", "textarea", "select", "label", "[contenteditable]"):
        assert control in source[source.index("var COMPOSER_CONTROLS"):source.index("composerShell.addEventListener")]
    shell_rule = css[css.index(".composer-shell {"):]
    assert "cursor: text;" in shell_rule[: shell_rule.index("}")]
    field_rule = css[css.index(".composer textarea {"):]
    field_rule = field_rule[: field_rule.index("}")]
    assert "caret-color: var(--ink);" in field_rule and "cursor: text;" in field_rule


# --- the conversation workspace (stable size, no handoff) ------------------------------------------

# Records, in the homepage window, every message the chat iframe posts (there
# must be none: the workspace never resizes or hands off).
PARENT_LOG = r"""
window.__parentLog = [];
window.addEventListener('message', e => window.__parentLog.push({
  type: e.data && typeof e.data === 'object' ? e.data.type : String(e.data), origin: e.origin }));
return true;
"""

LAYOUT = r"""
const c = window.__cx, d = c.doc(), w = c.win(), f = document.querySelector('[data-testid="embedded-chat"] iframe');
const conv = d.getElementById('conversation'), slot = document.querySelector('.embedded-chat-slot');
const cs = el => w.getComputedStyle(el);
const rect = el => { const b = el.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; };
const header = document.querySelector('.site-header'), trust = document.querySelector('.hero-under-note');
return {
  path: location.pathname, href: location.href, historyLength: history.length,
  scrollY: Math.round(window.scrollY), innerWidth, innerHeight,
  docScrollWidth: document.documentElement.scrollWidth,
  slot: slot ? rect(slot) : null, frame: f ? rect(f) : null,
  slotClass: slot ? slot.className : null,
  frameHeight: f ? Math.round(f.getBoundingClientRect().height) : innerHeight,
  header: header ? rect(header) : null, trust: trust ? rect(trust) : null,
  appHeight: Math.round(d.querySelector('.app').getBoundingClientRect().height), childInnerHeight: w.innerHeight,
  users: [...d.querySelectorAll('#thread .msg-user .bubble')].map(b => b.textContent),
  assistantRows: d.querySelectorAll('#thread .msg-assistant').length,
  notes: [...d.querySelectorAll('#thread .error-note, #thread .notice-note')].map(n => n.textContent),
  actions: [...d.querySelectorAll('#thread .msg-action span')].map(s => s.textContent),
  conv: { overflowY: cs(conv).overflowY, scrollHeight: conv.scrollHeight, clientHeight: conv.clientHeight,
          scrollTop: Math.round(conv.scrollTop), rect: rect(conv) },
  childDocScroll: [d.scrollingElement.scrollHeight, d.scrollingElement.clientHeight],
  composer: c.abs(d.getElementById('composer-shell')), send: c.abs(d.getElementById('send-btn')),
  sendIsStop: d.getElementById('send-btn').classList.contains('is-stop'),
  emptyPresent: !!d.getElementById('empty') && d.getElementById('empty').isConnected,
  sendCursor: cs(d.getElementById('send-btn')).cursor,
  requests: (w.__HANDOFF_CHILD__ || {requests: []}).requests.map(r => r.body.message),
  parentLog: (window.__parentLog || []).slice(),
};
"""

LABELS = ("idle", "afterInvalid", "firstImmediate", "firstReplied", "longReplied", "afterError",
          "inFlight", "afterStop", "readingEarlier")


def send_prompt(page: "Page", text: str):
    page.eval(f"window.__cx.setValue({json.dumps(text)}); return true;")
    box = page.eval("return window.__cx.abs(window.__cx.el('send-btn'));")
    page.click(box["left"] + box["width"] / 2, box["top"] + box["height"] / 2)


def run_conversation_scenario(page: "Page", homepage: bool) -> dict:
    r: dict = {}
    page.eval(COMPOSER_HELPERS)
    if homepage:
        page.eval(PARENT_LOG)
    page.eval("""for (let i = 0; i < 400 && !(window.__cx.doc() && window.__cx.el('composer-input')); i++)
        await window.__cx.wait(25);
      await window.__cx.wait(400); return true;""")
    layout = lambda: page.eval(LAYOUT)
    r["initial"] = layout()                                   # first render, page at the top

    if homepage:
        # Put the whole workspace in view (as the CTAs do) so the composer can
        # be clicked like a user would.
        page.eval("""const s = document.querySelector('.embedded-chat-slot').getBoundingClientRect();
          const h = document.querySelector('.site-header').getBoundingClientRect();
          window.scrollTo({top: window.scrollY + s.top - h.bottom - 16, behavior: 'instant'});
          await window.__cx.wait(200); return true;""")
    r["idle"] = layout()

    # ---- invalid submissions send nothing ------------------------------------------------------
    send_prompt(page, "")                                     # disabled Send, empty composer
    time.sleep(0.2)
    page.eval("window.__cx.setValue('   '); window.__cx.el('composer-input').focus(); return true;")
    for kind in ("keyDown", "keyUp"):
        page.dt.call("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter",
                                                "windowsVirtualKeyCode": 13}, page.session)
    page.eval("window.__cx.el('send-btn').click(); return true;")  # whitespace-only via the button
    time.sleep(0.3)
    r["afterInvalid"] = layout()

    # ---- a real multi-turn conversation in the same workspace -----------------------------------
    send_prompt(page, "First question")
    time.sleep(0.06)
    r["firstImmediate"] = layout()                            # the scripted reply takes 300 ms
    time.sleep(0.7)
    r["firstReplied"] = layout()

    send_prompt(page, "LONG: please write a long answer")
    time.sleep(0.9)
    r["longReplied"] = layout()

    send_prompt(page, "ERROR: this one fails")
    time.sleep(0.9)
    r["afterError"] = layout()

    send_prompt(page, "HANG: wait for Stop")
    time.sleep(0.3)
    r["inFlight"] = layout()
    stop = r["inFlight"]["send"]
    page.click(stop["left"] + stop["width"] / 2, stop["top"] + stop["height"] / 2)
    time.sleep(0.4)
    r["afterStop"] = layout()

    # Reading earlier messages is not interrupted: no new message, no yank.
    page.eval("window.__cx.doc().getElementById('conversation').scrollTop = 0; return true;")
    time.sleep(0.5)
    r["readingEarlier"] = layout()
    return r


@pytest.fixture(scope="module", params=[
    ("desktop", False), ("desktop", True), ("mobile", False), ("mobile", True),
], ids=["desktop", "desktop-reduced-motion", "mobile", "mobile-reduced-motion"])
def homepage_conversation(request, tmp_path_factory):
    browser = require_browser_and_build()
    viewport, reduced = request.param
    profile = tmp_path_factory.mktemp(f"edge-conversation-{viewport}-{int(reduced)}")
    site = Site()

    with site.http:
        with browser_page(browser, profile, VIEWPORTS[viewport], viewport == "mobile",
                          reduced_motion=reduced) as (devtools, session):
            page = Page(devtools, session)
            devtools.call("Page.navigate", {"url": site.http.origin + "/"}, session)
            time.sleep(0.5)
            result = run_conversation_scenario(page, homepage=True)

    shutil.rmtree(profile, ignore_errors=True)
    result["viewport"], result["size"], result["reduced"] = viewport, VIEWPORTS[viewport], reduced
    return result


@pytest.fixture(scope="module", params=["desktop", "mobile"])
def app_conversation(request, tmp_path_factory):
    browser = require_browser_and_build()
    viewport = request.param
    profile = tmp_path_factory.mktemp(f"edge-app-conversation-{viewport}")
    site = Site()
    site.child_passive = True

    with site.http:
        with browser_page(browser, profile, VIEWPORTS[viewport], viewport == "mobile") as (devtools, session):
            page = Page(devtools, session)
            devtools.call("Page.navigate", {"url": site.http.origin + "/app/"}, session)
            time.sleep(0.5)
            result = run_conversation_scenario(page, homepage=False)

    shutil.rmtree(profile, ignore_errors=True)
    result["viewport"], result["size"] = viewport, VIEWPORTS[viewport]
    return result


def min_height(viewport: str) -> int:
    return 560 if viewport == "desktop" else 520


def assert_usable_workspace(snap):
    # The chat fills its frame; the transcript is the only vertical scroller
    # and the composer sits fully visible at the bottom.
    assert abs(snap["appHeight"] - snap["childInnerHeight"]) <= 1
    assert snap["conv"]["overflowY"] == "auto"
    assert snap["childDocScroll"][0] <= snap["childDocScroll"][1]
    frame_top, frame_height = (snap["frame"][1], snap["frame"][3]) if snap["frame"] else (0, snap["innerHeight"])
    composer = snap["composer"]                                     # top-level coordinates
    assert frame_top <= composer["top"] and composer["bottom"] <= frame_top + frame_height + 1
    assert composer["bottom"] <= snap["innerHeight"] + 1
    # The transcript ends above the composer (both in the chat's coordinates).
    conv_bottom = snap["conv"]["rect"][1] + snap["conv"]["rect"][3]
    assert conv_bottom <= composer["top"] - frame_top + 1, (conv_bottom, composer, frame_top)
    assert snap["docScrollWidth"] <= snap["innerWidth"]


def test_workspace_is_full_height_from_the_first_render(homepage_conversation):
    run = homepage_conversation
    first = run["initial"]
    target = WORKSPACE_HEIGHT[run["viewport"]]

    assert first["scrollY"] == 0 and first["requests"] == [] and first["users"] == []
    assert abs(first["slot"][3] - target) <= 1 and first["slot"][3] >= min_height(run["viewport"])
    assert abs(first["frameHeight"] - (first["slot"][3] - 2)) <= 1          # the frame fills the card
    assert first["slotClass"] == "embedded-chat-slot"
    # No empty-history scrollbar before the first message.
    assert first["conv"]["scrollHeight"] <= first["conv"]["clientHeight"]


def test_workspace_height_is_stable_before_and_after_submission(homepage_conversation):
    run = homepage_conversation
    height, frame_height = run["initial"]["slot"][3], run["initial"]["frameHeight"]

    for label in LABELS:
        snap = run[label]
        assert snap["slot"][3] == height and snap["frameHeight"] == frame_height, (label, snap["slot"])
        assert snap["slotClass"] == "embedded-chat-slot", label
        assert snap["slot"][2] == run["initial"]["slot"][2], label               # width too


def test_sending_never_moves_navigates_or_signals(homepage_conversation):
    run = homepage_conversation
    base = run["idle"]

    for label in LABELS[1:]:
        snap = run[label]
        assert snap["path"] == "/" and snap["href"] == base["href"], label       # no URL change
        assert snap["historyLength"] == base["historyLength"], label             # no history entry
        assert snap["scrollY"] == base["scrollY"], label                         # no page jump or hijack
        assert snap["slot"][1] == base["slot"][1], label                         # no layout shift
        assert snap["parentLog"] == [], label                                    # no message at all


def test_invalid_or_empty_submissions_send_nothing(homepage_conversation):
    snap = homepage_conversation["afterInvalid"]
    assert snap["requests"] == [] and snap["users"] == [] and snap["assistantRows"] == 0


def test_first_prompt_is_sent_once_and_answered_in_place(homepage_conversation):
    run = homepage_conversation
    assert run["firstImmediate"]["requests"] == ["First question"]
    assert run["firstImmediate"]["users"] == ["First question"] and run["firstImmediate"]["assistantRows"] == 1
    assert run["firstReplied"]["requests"] == ["First question"]
    assert run["firstReplied"]["actions"][:2] == ["Copy", "Retry"]


def test_multi_turn_conversation_with_copy_retry_stop_and_errors(homepage_conversation):
    run = homepage_conversation
    assert run["afterError"]["notes"][-1] == "Kalillac is temporarily unavailable. Please try again shortly."
    assert run["afterError"]["users"] == ["First question", "LONG: please write a long answer",
                                          "ERROR: this one fails"]
    assert run["afterError"]["assistantRows"] == 3
    # A new send replaces a failed exchange on screen (existing chat behavior);
    # the stopped exchange then stays with its notice and Retry.
    assert run["afterStop"]["notes"][-1] == "Stopped."
    assert run["afterStop"]["users"] == ["First question", "LONG: please write a long answer",
                                         "HANG: wait for Stop"]
    assert run["afterStop"]["assistantRows"] == 3
    assert run["afterStop"]["actions"].count("Copy") == 2 and "Retry" in run["afterStop"]["actions"]
    assert run["afterStop"]["requests"] == ["First question", "LONG: please write a long answer",
                                            "ERROR: this one fails", "HANG: wait for Stop"]


def test_only_message_history_scrolls_and_the_composer_stays_visible(homepage_conversation):
    run = homepage_conversation
    for label in ("idle", "firstReplied", "longReplied", "afterError", "afterStop", "readingEarlier"):
        assert_usable_workspace(run[label])
    long = run["longReplied"]
    assert long["conv"]["scrollHeight"] > long["conv"]["clientHeight"]           # it overflows...
    assert long["conv"]["scrollTop"] + long["conv"]["clientHeight"] >= long["conv"]["scrollHeight"] - 2
    # ...and the trust line never overlaps the workspace (it sits beside it on
    # desktop and above it on phones).
    snap = run["afterStop"]
    (tl, tt, tw, th), (sl, st, sw, sh) = snap["trust"], snap["slot"]
    assert tl + tw <= sl or sl + sw <= tl or tt + th <= st or st + sh <= tt, (snap["trust"], snap["slot"])


def test_starting_state_is_removed_after_the_first_message(homepage_conversation):
    run = homepage_conversation
    assert run["idle"]["emptyPresent"] and run["afterInvalid"]["emptyPresent"]
    for label in LABELS[2:]:
        assert run[label]["emptyPresent"] is False, label


def test_composer_stays_pinned_at_the_bottom_of_the_workspace(homepage_conversation):
    run = homepage_conversation
    # Same composer position from the empty state to a long, overflowing transcript.
    base = run["idle"]["composer"]
    for label in ("firstReplied", "longReplied", "afterError", "afterStop", "readingEarlier"):
        composer = run[label]["composer"]
        assert abs(composer["bottom"] - base["bottom"]) <= 1 and abs(composer["top"] - base["top"]) <= 1, label


def test_reading_earlier_messages_is_not_interrupted(homepage_conversation):
    assert homepage_conversation["readingEarlier"]["conv"]["scrollTop"] == 0


def test_stop_keeps_the_pointer_cursor(homepage_conversation):
    in_flight = homepage_conversation["inFlight"]
    assert in_flight["sendIsStop"] is True and in_flight["sendCursor"] == "pointer"


def test_direct_app_is_a_full_height_conversation(app_conversation):
    run = app_conversation
    width, height = run["size"]
    snap = run["afterStop"]

    assert run["initial"]["frame"] is None and run["initial"]["appHeight"] == height
    assert snap["appHeight"] == height and snap["childInnerHeight"] == height
    for label in ("idle", "longReplied", "afterStop"):
        assert_usable_workspace(run[label])
    assert snap["assistantRows"] == 3 and snap["requests"][0] == "First question"
    assert run["longReplied"]["conv"]["scrollHeight"] > run["longReplied"]["conv"]["clientHeight"]
    assert run["readingEarlier"]["conv"]["scrollTop"] == 0
    assert run["inFlight"]["sendIsStop"] is True and run["inFlight"]["sendCursor"] == "pointer"
    assert run["afterInvalid"]["requests"] == []
    assert run["idle"]["emptyPresent"] and not run["firstReplied"]["emptyPresent"]


def test_workspace_css_is_one_stable_size():
    css = SITE_CSS.read_text(encoding="utf-8")

    assert "height: 640px; height: clamp(560px, 70vh, 720px); scroll-margin-top: 96px; }" in css
    # Beside the headline (1180px and wider) it fills the first screen below the header.
    assert ("  .embedded-chat-slot { height: 760px; height: clamp(560px, calc(100vh - 128px), 760px); "
            "height: clamp(560px, calc(100svh - 128px), 760px); }") in css
    assert ("  .embedded-chat-slot { height: 600px; height: clamp(520px, 72vh, 680px); "
            "height: clamp(520px, 72svh, 680px); }") in css
    # Short viewports (200% zoom, landscape phones): it fits below the sticky header.
    assert ("@media (max-height: 560px) {\n  .embedded-chat-slot { height: 320px; "
            "height: max(280px, calc(100vh - 96px)); height: max(280px, calc(100svh - 96px)); }") in css
    # No other height for the workspace, and no state that could resize it.
    assert len(re.findall(r"\.embedded-chat-slot[^{]*\{[^}]*\bheight:", css)) == 4
    for gone in ("is-expanded", "is-active", "chat-expanded", "chat-expand"):
        assert gone not in css, gone


# --- static contract (always runs) -----------------------------------------------------------------


def test_cta_code_uses_reduced_motion_and_prevent_scroll():
    app = APP_TSX.read_text(encoding="utf-8")

    assert "window.matchMedia('(prefers-reduced-motion: reduce)')" in app
    assert "behavior: prefersReducedMotion() ? 'auto' : 'smooth'" in app
    assert "'instant'" not in app.replace("newer 'instant'", "")
    # 'auto' is immediate under reduced motion only because the CSS forces it.
    css = SITE_CSS.read_text(encoding="utf-8")
    assert re.search(r"@media \(prefers-reduced-motion: reduce\) \{\s*\*, \*::before, \*::after \{ "
                     r"scroll-behavior: auto !important;", css)
    assert "input.focus({ preventScroll: true })" in app
    assert "frame?.focus({ preventScroll: true })" in app
    assert "scrollIntoView({ block: 'center' })" not in app
    # One CTA, one handler; the header has no second call to action.
    assert 'onClick={focusChat} data-testid="button-start-session"' in app
    assert app.count("onClick={focusChat}") == 1
    assert "tryKalillac" not in app and "nav-cta" not in app and "Try Kalillac" not in app


def test_conversation_stays_on_the_homepage_in_source():
    app = APP_TSX.read_text(encoding="utf-8")
    chat = (REPO / "frontend" / "app.js").read_text(encoding="utf-8")

    for source in (app, chat):
        for forbidden in ("pushState", "replaceState", "location.assign", "location.href =",
                          "location.replace", "window.open("):
            assert forbidden not in source, forbidden
    # The homepage embeds the one chat implementation; it never links into /app/.
    assert app.count("'/app/'") == 1 and "const CHAT_PATH = '/app/';" in app
    assert 'href="/app' not in app and "navigate('/app" not in app


def test_example_prompts_only_fill_the_composer_in_source():
    chat = (REPO / "frontend" / "app.js").read_text(encoding="utf-8")
    html = (REPO / "frontend" / "index.html").read_text(encoding="utf-8")

    for prompt in EXAMPLE_PROMPTS:
        assert f'data-prompt="{prompt}">{prompt}</button>' in html
    handler = chat[chat.index('var hints = document.querySelectorAll(".hint");'):]
    handler = handler[handler.index('addEventListener("click"'):]
    handler = handler[: handler.index("});") + 3]
    assert 'input.value = this.getAttribute("data-prompt") || "";' in handler
    assert "send(" not in handler and ".submit" not in handler and "requestSubmit" not in handler


def test_privacy_diagram_source_makes_no_absolute_claims_and_respects_reduced_motion():
    app = APP_TSX.read_text(encoding="utf-8")
    css = SITE_CSS.read_text(encoding="utf-8")
    # The homepage's own sections (the Privacy and Terms pages are separate routes).
    home = app[app.index("function FlowMarkers"):app.index("function DocSection(")].lower()

    for claim in FORBIDDEN_CLAIMS:
        assert not makes_claim(home, claim), claim
    for icon in ("padlock", "lock-icon", "<lock", "lockkeyhole"):
        assert icon not in home, icon
    assert home.count(MEMORY_SENTENCE.lower()) >= 2     # beneath the diagram and in the explanation
    assert css.count("@media (prefers-reduced-motion: reduce)") >= 1
    assert re.search(r"@media \(prefers-reduced-motion: no-preference\) \{[^}]*animation", css)


def test_backend_source_hash_is_unchanged():
    # SHA-256 of the committed (LF) bytes; a Windows checkout with
    # core.autocrlf=true holds CRLF on disk, so compare git's normalized form.
    backend = REPO / "candidate" / "app_fastapi_candidate.py"
    assert hashlib.sha256(backend.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == BACKEND_SHA256


def test_css_keeps_the_header_sticky_and_decorations_inert():
    css = SITE_CSS.read_text(encoding="utf-8")

    assert re.search(r"\.site-shell \{[^}]*overflow-x: hidden; overflow-x: clip;", css)
    assert ".orbital-wrap, .orbital-glow, .hero-signal-line, .phone-stage { pointer-events: none; }" in css
    assert ":focus-visible { outline:" in css


def git_diff_names(*paths: str) -> list[str] | None:
    git = shutil.which("git")
    if not git:
        return None
    known = subprocess.run([git, "cat-file", "-e", SLICE_BASE + "^{commit}"], cwd=REPO, capture_output=True)
    if known.returncode != 0:
        return None
    changed = subprocess.run([git, "diff", "--name-only", SLICE_BASE, "--", *paths], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout.split()
    untracked = subprocess.run([git, "ls-files", "--others", "--exclude-standard", "--", *paths], cwd=REPO,
                               capture_output=True, text=True, check=True).stdout.split()
    return changed + untracked


def test_app_frontend_backend_and_dependencies_are_untouched():
    # /app/ may differ only in the composer CSS, the composer JavaScript and
    # their cache-busting references -- nothing else under frontend/.
    frontend = git_diff_names("frontend")
    if frontend is None:
        pytest.skip(f"git or the slice base commit {SLICE_BASE} is unavailable")
    assert set(frontend) <= {"frontend/app.css", "frontend/app.js", "frontend/index.html"}, frontend

    names = git_diff_names(
        "candidate/app_fastapi_candidate.py", "candidate/kalillac_routing",
        "candidate/kalillac_accounts", "candidate/kalillac_billing", "candidate/kalillac_db",
        "candidate/migrations", "requirements-production-lock.txt", "requirements-dev.txt",
        "requirements-database.txt", "requirements-billing.txt", "site/package.json", "site/package-lock.json",
        "site/vite.config.ts", "site/index.html", "site/deployment-preserve-live-assets.txt", "site/public",
    )
    if names is None:
        pytest.skip(f"git or the slice base commit {SLICE_BASE} is unavailable")
    assert names == [], names

    package = json.loads((SITE / "package.json").read_text(encoding="utf-8"))
    assert set(package["dependencies"]) == {"lucide-react", "react", "react-dom", "wouter"}
