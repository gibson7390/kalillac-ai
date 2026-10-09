"""Homepage quick-start composer: CTA scrolling, focus, pointer/caret,
responsive layout, and the chat composer's whole typing area -- both inside
the homepage iframe and on a direct /app/ visit.

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
        markShown: shown('.empty-mark'), titleShown: shown('.empty-title'), lineShown: shown('.empty-line'),
        hintsShown: shown('.empty-hints'), messages: d.querySelectorAll('#thread .msg').length,
        input: ix.childRect(ix.input()), send: ix.childRect(d.getElementById('send-btn')),
        frame: ix.rect(ix.frame()),
      };""")

    # ---- pointer, caret, decorative layers -------------------------------------------
    r["styles"] = page.eval("""
      const ix = window.__ix, d = ix.doc(), cs = el => getComputedStyle(el);
      const decorative = ['.orbital-wrap', '.orbital-glow', '.hero-signal-line', '.capability-art', '.web-path',
                          '.privacy-orbit', '.phone-stage'];
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
    box = r["landing"]["input"]
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

    # ---- CTA 1: "Start a private session" from the landing position ------------------------
    page.eval("window.__ix.blurAll(); window.scrollTo({top: 0, behavior: 'instant'}); return true;")
    cta = r["landing"]["cta"]
    page.click(cta["left"] + cta["width"] / 2, cta["top"] + cta["height"] / 2)
    r["ctaFromTop"] = page.eval("""
      const ix = window.__ix, samples = await ix.sample(900, 60);
      return { samples, header: ix.rect(ix.header()), card: ix.rect(ix.card()), ...ix.focusState() };""")

    # ---- CTA 2: header "Try Kalillac" from the bottom of the page ----------------------------
    page.eval("""window.__ix.blurAll();
      window.scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'});
      await window.__ix.wait(150); return true;""")
    start = page.eval("""const ix = window.__ix, b = document.querySelector('.site-nav .nav-cta');
      return { scrollY: window.scrollY, button: ix.rect(b), header: ix.rect(ix.header()) };""")
    page.click(start["button"]["left"] + start["button"]["width"] / 2,
               start["button"]["top"] + start["button"]["height"] / 2)
    r["tryFromBottom"] = page.eval("""
      const ix = window.__ix, samples = await ix.sample(1800, 30);
      return { samples, header: ix.rect(ix.header()), card: ix.rect(ix.card()), ...ix.focusState(),
               url: location.href };""")
    r["tryFromBottom"]["start"] = start

    # ---- CTA 1 again, off-screen, from the bottom: same target, same position -----------------
    page.eval("""window.__ix.blurAll();
      window.scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'});
      await window.__ix.wait(150);
      document.querySelector('[data-testid="button-start-session"]').click(); return true;""")
    r["startFromBottom"] = page.eval("""
      const ix = window.__ix, samples = await ix.sample(1800, 30);
      return { samples, header: ix.rect(ix.header()), card: ix.rect(ix.card()), ...ix.focusState() };""")

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
    return run["tryFromBottom"]["header"]["bottom"] + GAP_BELOW_HEADER


# --- landing layout -------------------------------------------------------------------------------


def test_lands_at_the_top_with_the_header_visible_and_no_horizontal_overflow(interaction):
    landing = interaction["landing"]
    width, height = interaction["size"]

    assert (landing["innerWidth"], landing["innerHeight"]) == (width, height)
    assert landing["scrollY"] == 0
    assert landing["header"]["top"] == 0
    assert landing["docScrollWidth"] <= width and landing["bodyScrollWidth"] <= width
    assert interaction["final"]["docScrollWidth"] <= width


def test_empty_composer_is_compact_without_a_transcript_scrollbar(interaction):
    landing = interaction["landing"]

    assert landing["messages"] == 0
    # No redundant intro inside the card: only the starters and the composer.
    assert not landing["markShown"] and not landing["titleShown"] and not landing["lineShown"]
    assert landing["hintsShown"]
    # Nothing in the empty card overflows, so no scrollbar can appear.
    scroll_height, client_height = landing["convScroll"]
    assert scroll_height <= client_height, landing["convScroll"]
    assert landing["childDocScroll"][0] <= landing["childDocScroll"][1]
    # No large empty transcript area is reserved: the transcript region is at
    # most the starters' height plus their padding.
    assert client_height <= 130, client_height
    assert landing["card"]["height"] == (164 if interaction["viewport"] == "desktop" else 200)


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


def test_cta_from_the_landing_position_focuses_without_moving_the_page(interaction):
    run = interaction["ctaFromTop"]

    # The card is already fully visible below the header on both viewports,
    # so the page does not move at all; the real input is focused.
    assert set(run["samples"]) == {0}
    assert run["header"]["top"] == 0
    assert run["parentActive"] and run["childActive"]


@pytest.mark.parametrize("flow", ["tryFromBottom", "startFromBottom"])
def test_both_ctas_bring_the_composer_just_below_the_header_and_focus_it(interaction, flow):
    run = interaction[flow]
    samples = run["samples"]
    final = samples[-1]

    assert run["header"]["top"] == 0                               # sticky header stays visible
    assert abs(run["card"]["top"] - expected_card_top(interaction)) <= 1, run["card"]
    assert run["parentActive"] and run["childActive"]
    # Settled, with no second browser-generated jump after focusing.
    assert samples[-12:] == [final] * 12
    assert "#" not in run.get("url", "")


def test_both_ctas_target_the_same_composer_position(interaction):
    assert abs(interaction["tryFromBottom"]["card"]["top"] - interaction["startFromBottom"]["card"]["top"]) <= 1
    assert abs(interaction["tryFromBottom"]["samples"][-1] - interaction["startFromBottom"]["samples"][-1]) <= 1


def test_scroll_is_smooth_or_immediate_according_to_motion_preference(interaction):
    samples = interaction["tryFromBottom"]["samples"]
    start, final = interaction["tryFromBottom"]["start"]["scrollY"], samples[-1]
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
    # Both CTAs go through the same handler.
    assert 'onClick={focusChat} data-testid="button-start-session"' in app
    assert "onTryKalillac={tryKalillac}" in app and "focusChat();" in app


def test_css_keeps_the_header_sticky_and_decorations_inert():
    css = SITE_CSS.read_text(encoding="utf-8")

    assert ".site-shell { overflow-x: hidden; overflow-x: clip; }" in css
    assert re.search(r"\.orbital-wrap, \.orbital-glow, \.hero-signal-line, \.capability-art, \.web-path,\s*"
                     r"\.privacy-orbit, \.phone-stage \{ pointer-events: none; \}", css)
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
