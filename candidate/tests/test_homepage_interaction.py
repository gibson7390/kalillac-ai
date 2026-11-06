"""Homepage composer and the /app/ workspace: interaction, layout and structure.

The homepage hero holds one real message box (site/src/App.tsx): clicking,
typing, editing, clearing, the "Start a private session" CTA and the four
examples all stay on "/" and send nothing; only Send hands the draft to /app/
(test_homepage_handoff.py). These tests drive that composer with trusted mouse,
touch and keyboard input, the page structure below the hero at every required
viewport and at the browser's own 200% zoom, the /app/ header, empty state and
composer, and End & erase in /app/.

Browser tests drive the BUILT site (site/dist/public) with the canonical
frontend/ chat at /app/, served from loopback only by the harness in
test_homepage_handoff.py. Headless Edge (or another Chromium-family browser) is
controlled over the DevTools protocol: exact 1440 x 900 desktop and 390 x 844
touch-phone viewports, emulated prefers-reduced-motion, and real (trusted)
input. Nothing leaves the machine.

Without a browser or a site build the browser tests skip (on Windows a
missing Edge fails, as in the other frontend tests). The static checks
always run.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

import pytest

from test_homepage_handoff import (
    APP_TSX,
    APP_JS,
    INDEX_HTML,
    REPO,
    SITE,
    SITE_CSS,
    VIEWPORTS,
    Site,
    browser_page,
    require_browser_and_build,
)


# The commit this slice's dependency guard compares against. /app/ may differ
# only in its own files; the site's build and dependency files only by the
# reviewed edits below.
SLICE_BASE = "92a4984792ac0bfb4c1952555e08300d094ab3a0"
GAP_BELOW_HEADER = 16


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


# The /app/ chat composer, on its own top-level page. All coordinates are top-level.
COMPOSER_HELPERS = r"""
window.__cx = {
  wait: ms => new Promise(r => setTimeout(r, ms)),
  doc: () => document,
  win: () => window,
  off: () => ({left: 0, top: 0}),
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
  state: () => { const i = window.__cx.el('composer-input');
    const child = window.__cx.win().__HANDOFF_CHILD__ || {requests: []};
    return { focused: window.__cx.doc().activeElement === i,
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
               send: c.abs(c.el('send-btn')), erase: c.abs(c.el('erase-btn')),
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
    # The padding gap before Send: beside the field, or, in a phone-width
    # frame where the field has the first row to itself, between End & erase
    # and Send on the second.
    erase = r["geometry"]["erase"]
    before_send = field["right"] if send["top"] < field["bottom"] else erase["right"]
    points = {
        "above": (mid_x, shell["top"] + 4),
        "below": (mid_x, shell["bottom"] - 4),
        "left": (shell["left"] + 6, mid_y),
        "between-field-and-send": ((before_send + send["left"]) / 2, send["top"] + send["height"] / 2),
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
            chrome = page.eval(APP_CHROME)
            result = run_composer_scenario(page)
            result["chrome"] = chrome
            result["path"] = page.eval("return location.pathname + location.search + location.hash;")

    shutil.rmtree(profile, ignore_errors=True)
    result["viewport"], result["size"] = viewport, VIEWPORTS[viewport]
    return result


EXAMPLE_PROMPTS = ('Find this week’s biggest technology stories', 'Draft a professional email', 'Build a simple Python password generator', 'Explain how encryption works')
TASK_LABELS = ('Research the current web', 'Write and revise', 'Code and debug', 'Learn anything')
MEMORY_SENTENCE = "Temporary session data can remain in server memory until capacity limits or a restart clear it."
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


def luminance(rgb: str) -> float:
    r, g, b = (int(v) / 255 for v in re.findall(r"\d+", rgb)[:3])
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


STRUCTURE = r"""
for (let i = 0; i < 400 && !document.getElementById('home-composer-input'); i++) {
  await new Promise(r => setTimeout(r, 25));
}
await new Promise(r => setTimeout(r, 400));
const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)];
const rect = el => { const b = el.getBoundingClientRect();
  return {left: b.left, top: b.top + scrollY, right: b.right, bottom: b.bottom + scrollY, width: b.width,
          height: b.height}; };
const visible = el => { const s = getComputedStyle(el), b = el.getBoundingClientRect();
  return s.display !== 'none' && s.visibility !== 'hidden' && b.width > 0 && b.height > 0; };
const panel = q('[data-testid="home-composer"]').getBoundingClientRect(), shell = q('.hc-shell').getBoundingClientRect();
const svgs = qa('.flow-svg').filter(visible);
const svg = svgs[0];
const alpha = c => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return 0; const p = m[1].split(',');
  return p.length > 3 ? parseFloat(p[3]) : 1; };
const order = ['section-hero', 'section-tools', 'section-promise', 'section-flow', 'section-demos', 'section-explain', 'section-mobile-app']
  .map(id => q(`[data-testid="${id}"]`));
return {
  innerWidth, innerHeight, dpr: devicePixelRatio,
  docScrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth,
  h1s: qa('h1').map(h => h.textContent.replace(/\s+/g, ' ').trim()),
  heroEyebrow: (() => { const e = q('.hero .eyebrow'); return [e.tagName, e.textContent]; })(),
  // Filled calls to action outside the composer (its own Send is a control, not a CTA).
  filled: qa('a, button').filter(visible).filter(e => !e.closest('.home-composer'))
    .filter(e => alpha(getComputedStyle(e).backgroundColor) > 0.5
      && getComputedStyle(e).backgroundColor !== 'rgb(255, 255, 255)')
    .map(e => e.textContent.trim()),
  navLinks: qa('.site-nav a').filter(visible).map(a => [a.textContent.trim(), a.getAttribute('href')]),
  composerTopInViewport: Math.round(shell.top), composerBottomInViewport: Math.round(shell.bottom),
  panelBottomInViewport: Math.round(panel.bottom), iframes: qa('iframe').length,
  sectionsInOrder: order.every((el, i) => el && (i === 0 ||
    (order[i - 1].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING))),
  footerLast: !!q('.site-footer') && !!(order[6].compareDocumentPosition(q('.site-footer')) & Node.DOCUMENT_POSITION_FOLLOWING),
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
  demosHeading: q('#temporary-memory h2').textContent.trim(),
  flowHTML: {tag: q('[data-testid="flow-summary"]').tagName, labelledby: q('[data-testid="flow-summary"]').getAttribute('aria-labelledby'), stages: qa('.flow-stage').map(e => [e.dataset.stage, e.querySelector('h3').textContent, e.querySelector('p').textContent]), rect: rect(q('.flow-stages')), columns: getComputedStyle(q('.flow-stages')).gridTemplateColumns.split(' ').length, svgText: qa('.flow-panel svg text').length, minTextSize: Math.min(...qa('.flow-stage p').map(e => parseFloat(getComputedStyle(e).fontSize)))},
  tools: qa('.tool-card h3').map(e => e.textContent),
  promise: { heading: q('#promise-heading').textContent, text: q('.promise-panel').textContent, count: qa('.promise-item').length },
  memory: (() => { const s = q('[data-testid="section-demos"]'), phases = [...s.querySelectorAll('.memory-phase')];
    return { id: s.id, tag: s.tagName, labelledby: s.getAttribute('aria-labelledby'), headingId: s.querySelector('h2').id,
      eyebrow: s.querySelector('.eyebrow').innerText.trim(), intro: s.querySelector('.demos-intro p').textContent.trim(),
      phasesListTag: phases.length ? phases[0].parentElement.tagName : null,
      phases: phases.map(ph => { const h = ph.querySelector('h3'), list = ph.querySelector('.memory-transcript');
        return { tag: ph.tagName, label: h.textContent.replace(/\s+/g, ' ').trim(), labelTag: h.tagName, labelId: h.id, listTag: list.tagName,
                 listLabelledby: list.getAttribute('aria-labelledby'), rect: rect(ph),
                 turns: [...list.children].map(m => [m.tagName, m.className, m.querySelector('.memory-speaker').textContent.trim(),
                                                     m.querySelector('p').textContent.trim()]) }; }),
      explain: q('[data-testid="memory-explain"]').textContent.trim(),
      disclosure: q('[data-testid="memory-disclosure"]').textContent.trim(),
      overflowing: [...s.querySelectorAll('*')].filter(e => { const b = e.getBoundingClientRect();
        return b.width > 0 && (b.right > innerWidth + 1 || b.left < -1); }).length,
      text: s.innerText.replace(/\s+/g, ' ') }; })(),
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
    "1024x1366": ((1024, 1366), False, False, None),
    "320x844": ((320, 844), True, False, None),
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
            result["requests"] = len([path for path in site.http.paths
                                      if path.startswith("/app/") or path.startswith("/api/") or "POST" in path])

    shutil.rmtree(profile, ignore_errors=True)
    result.update(name=request.param, size=size, mobile=mobile, reduced=reduced, zoom=zoom)
    return result


def test_structure_has_one_h1_and_one_filled_call_to_action(structure):
    assert structure["h1s"] == ["Think clearly. Write well. Build and debug. Search the current web."]
    assert structure["heroEyebrow"] == ["SPAN", "PRIVATE BY DESIGN"]
    assert [t.replace("\u00a0", " ") for t in structure["filled"]] == ["Start a private session"], structure["filled"]
    if not structure["mobile"] and structure["innerWidth"] >= 1024:
        assert structure["navLinks"] == [["Product", "/#product"], ["Privacy", "/privacy"],
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
    assert structure["panelBottomInViewport"] <= structure["innerHeight"], structure
    assert structure["iframes"] == 0


def test_privacy_diagram_has_four_stages_and_an_accessible_name(structure):
    flow = structure["flowHTML"]
    assert flow["tag"] == "OL" and flow["labelledby"] == "flow-heading"
    assert flow["svgText"] == 0
    stages = flow["stages"]
    assert [stage[0] for stage in stages] == ["1", "2", "3", "4"]
    assert [stage[1] for stage in stages] == ["1. You ask", "2. Temporary session", "3. Answer or search", "4. You move on"]
    assert "No account or profile is required" in stages[0][2]
    assert "temporary server memory" in stages[1][2]
    assert all(p in stages[2][2] for p in ("OpenAI", "Brave", "Tavily", "eligible"))
    assert "not immediate deletion" in stages[3][2]
    assert flow["columns"] == (4 if structure["innerWidth"] > 1100 else 2 if structure["innerWidth"] > 720 else 1)
    assert flow["rect"]["left"] >= 0 and flow["rect"]["right"] <= structure["innerWidth"]
    assert flow["minTextSize"] >= 13


def test_privacy_diagram_has_a_semantic_summary_and_the_memory_sentence_beneath(structure):
    assert structure["flowHeading"] == ["Your conversation has boundaries.", "HOW IT WORKS"]
    summary = structure["summary"]
    assert len(summary) == 4 and all(p in summary[2] for p in ("OpenAI", "Brave", "Tavily", "eligible"))
    note = structure["note"]
    assert note["visible"] and note["text"].startswith(MEMORY_SENTENCE)
    assert note["rect"]["top"] >= structure["flowHTML"]["rect"]["bottom"]
    assert note["fontSize"] >= 14


MEMORY_TRANSCRIPT = {
    "1 DURING THE SAME SESSION": [
        ("you", "Remember for me that my business name is Kalillac AI."),
        ("kalillac", "I’ll remember that your business name is Kalillac AI for this conversation."),
        ("you", "What is my business name?"),
        ("kalillac", "Your business name is Kalillac AI."),
    ],
    "2 AFTER REFRESHING": [
        ("you", "Do you remember me telling you what my business name was?"),
        ("kalillac", "No. I don’t see your business name in the current session, so I can’t recover it."),
    ],
}
REMOVED_DEMONSTRATION_TEXT = (
    "See Kalillac at work.", "Illustrative examples of the kinds of help you can ask for.",
    "01 · Think", "02 · Write", "03 · Build and debug", "04 · Search the current web",
    "Turn a tangled question into a clear way to decide.", "Clearer writing that still sounds like you.",
    "See why it breaks, then fix it.", "Current answers with their sources in view.",
    "Searched the current web", "Project repository", "Release notes", "Issue tracker",
    "def add_tag(tag, tags=[])",
)


def test_memory_demonstration_replaces_the_four_handcrafted_cards(structure):
    memory = structure["memory"]

    # The memory section has its own bookmark; #product now reaches the tools.
    assert memory["id"] == "temporary-memory" and memory["tag"] == "SECTION"
    assert memory["labelledby"] == memory["headingId"] == "memory-heading"
    assert memory["eyebrow"] == "SEE IT IN ACTION"
    assert structure["demosHeading"] == "Temporary memory you can test yourself."
    assert memory["intro"] == ("Kalillac can remember information during your active conversation. Refreshing starts "
                               "a new browser session that cannot reopen the previous conversation.")
    # Two chronological phases, each a labelled, ordered transcript of user and Kalillac turns.
    assert memory["phasesListTag"] == "OL"
    assert [p["label"] for p in memory["phases"]] == list(MEMORY_TRANSCRIPT)
    for phase in memory["phases"]:
        assert phase["tag"] == "LI" and phase["labelTag"] == "H3" and phase["listTag"] == "OL"
        assert phase["listLabelledby"] == phase["labelId"]
        expected = MEMORY_TRANSCRIPT[phase["label"]]
        assert [(t[0], t[2], t[3]) for t in phase["turns"]] == [
            ("LI", "You:" if who == "you" else "Kalillac:", text) for who, text in expected]
        assert [t[1] for t in phase["turns"]] == [f"memory-msg memory-msg-{who}" for who, _ in expected]
    # The accurate server-memory caveat and the excerpt disclosure.
    assert memory["explain"] == (
        "Refreshing ends that browser’s access to the previous temporary conversation. The old session data may "
        "remain temporarily in server memory until capacity limits or a restart clear it, but the refreshed browser "
        "cannot retrieve it.")
    assert memory["disclosure"] == ("Real Kalillac session example. The final response was excerpted, and formatting "
                                    "was condensed for display.")
    # The handcrafted cards and their generic sources are gone from the whole page.
    page = structure["pageText"].lower()
    for removed in REMOVED_DEMONSTRATION_TEXT:
        assert removed.lower() not in page, removed
    # Chronology: side by side on wide screens, phase 1 above phase 2 when stacked.
    first, second = (p["rect"] for p in memory["phases"])
    if structure["innerWidth"] > 720:
        assert second["left"] >= first["right"] and abs(second["top"] - first["top"]) <= 1
    else:
        assert second["top"] >= first["bottom"]
    assert memory["overflowing"] == 0


def test_explanation_has_exactly_two_blocks_with_the_required_meaning(structure):
    blocks = structure["explainBlocks"]
    assert [b[:2] for b in blocks] == [["explain-session", "Your temporary session"],
                                       ["explain-providers", "Who processes what"]]
    session, providers = blocks[0][2], blocks[1][2]
    for phrase in ("doesn\u2019t require an account", "permanent chat history", "temporary session",
                   "Refreshing or leaving the page ends", MEMORY_SENTENCE):
        assert phrase.replace("\u2019", "'") in session.replace("\u2019", "'"), phrase
    assert "OpenAI generates model answers." in providers and "Brave is the primary search provider" in providers and "Tavily may supply one bounded fallback" in providers
    assert structure["privacyLink"] == "/privacy"


def test_mobile_app_section_follows_the_explanation(structure):
    assert "Kalillac AI app in development." in structure["mobileApp"]
    assert "No release date announced" in structure["mobileApp"]
    assert structure["sectionTops"][6] > structure["sectionTops"][5]


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


# --- a real conversation on a direct /app/ visit -----------------------------------------------------

LAYOUT = r"""
const c = window.__cx, d = document, conv = d.getElementById('conversation');
const cs = el => getComputedStyle(el);
const rect = el => { const b = el.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; };
return {
  path: location.pathname, href: location.href, historyLength: history.length,
  scrollY: Math.round(window.scrollY), innerWidth, innerHeight,
  docScrollWidth: d.documentElement.scrollWidth,
  appHeight: Math.round(d.querySelector('.app').getBoundingClientRect().height),
  users: [...d.querySelectorAll('#thread .msg-user .bubble')].map(b => b.textContent),
  assistantRows: d.querySelectorAll('#thread .msg-assistant').length,
  notes: [...d.querySelectorAll('#thread .error-note, #thread .notice-note')].map(n => n.textContent),
  actions: [...d.querySelectorAll('#thread .msg-action span')].map(s => s.textContent),
  conv: { overflowY: cs(conv).overflowY, scrollHeight: conv.scrollHeight, clientHeight: conv.clientHeight,
          scrollTop: Math.round(conv.scrollTop), rect: rect(conv) },
  docScroll: [d.scrollingElement.scrollHeight, d.scrollingElement.clientHeight],
  composer: c.abs(d.getElementById('composer-shell')), send: c.abs(d.getElementById('send-btn')),
  sendIsStop: d.getElementById('send-btn').classList.contains('is-stop'),
  emptyPresent: !!d.getElementById('empty') && d.getElementById('empty').isConnected,
  sendCursor: cs(d.getElementById('send-btn')).cursor,
  requests: (window.__HANDOFF_CHILD__ || {requests: []}).requests.map(r => r.body.message),
};
"""

LABELS = ("idle", "afterInvalid", "firstImmediate", "firstReplied", "longReplied", "afterError",
          "inFlight", "afterStop", "readingEarlier")

# Waits until the chat is (or is no longer) busy and the transcript has held
# still -- position and size -- for 300 ms: the transcript scrolls smoothly,
# so a snapshot at a fixed time can land mid-scroll. Returns the milliseconds
# it took, or -1 after 5 seconds.
SETTLE = r"""
const conv = document.getElementById('conversation'), send = document.getElementById('send-btn');
const frame = () => new Promise(resolve => requestAnimationFrame(() => resolve()));
const start = performance.now();
let last = '', since = start;
while (performance.now() - start < 5000) {
  await frame();
  const busy = send.classList.contains('is-stop'), now = performance.now();
  const state = [busy, conv.scrollTop, conv.scrollHeight, conv.clientHeight].join();
  if (state !== last) { last = state; since = now; }
  else if (busy === BUSY && now - since >= 300) return Math.round(now - start);
}
return -1;"""


def send_prompt(page: Page, text: str):
    page.eval(f"window.__cx.setValue({json.dumps(text)}); return true;")
    box = page.eval("return window.__cx.abs(window.__cx.el('send-btn'));")
    page.click(box["left"] + box["width"] / 2, box["top"] + box["height"] / 2)


def run_conversation_scenario(page: Page) -> dict:
    """Invalid submissions, then a real multi-turn conversation -- a reply, a
    long reply, a service error, a request stopped with Stop -- and reading
    earlier messages, with the workspace measured after each step."""
    r: dict = {}
    page.eval(COMPOSER_HELPERS)
    page.eval("""for (let i = 0; i < 400 && !(window.__cx.doc() && window.__cx.el('composer-input')); i++)
        await window.__cx.wait(25);
      await window.__cx.wait(400); return true;""")
    layout = lambda: page.eval(LAYOUT)
    r["settled"] = {}

    def settled(label: str, busy: bool = False) -> dict:
        r["settled"][label] = page.eval(SETTLE.replace("BUSY", "true" if busy else "false"))
        return layout()

    r["initial"] = layout()
    r["idle"] = settled("idle")

    # ---- invalid submissions send nothing ------------------------------------------------------
    send_prompt(page, "")                                     # disabled Send, empty composer
    time.sleep(0.2)
    page.eval("window.__cx.setValue('   '); window.__cx.el('composer-input').focus(); return true;")
    for kind in ("keyDown", "keyUp"):
        page.dt.call("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter",
                                                "windowsVirtualKeyCode": 13}, page.session)
    page.eval("window.__cx.el('send-btn').click(); return true;")  # whitespace-only via the button
    r["afterInvalid"] = settled("afterInvalid")

    # ---- a real multi-turn conversation in the same workspace -----------------------------------
    send_prompt(page, "First question")
    time.sleep(0.06)
    r["firstImmediate"] = layout()                            # the scripted reply takes 300 ms
    r["firstReplied"] = settled("firstReplied")

    send_prompt(page, "LONG: please write a long answer")
    r["longReplied"] = settled("longReplied")

    send_prompt(page, "ERROR: this one fails")
    r["afterError"] = settled("afterError")

    send_prompt(page, "HANG: wait for Stop")
    r["inFlight"] = settled("inFlight", busy=True)
    stop = r["inFlight"]["send"]
    page.click(stop["left"] + stop["width"] / 2, stop["top"] + stop["height"] / 2)
    r["afterStop"] = settled("afterStop")

    # Reading earlier messages is not interrupted: no new message, no yank.
    page.eval("document.getElementById('conversation').scrollTop = 0; return true;")
    r["readingEarlier"] = settled("readingEarlier")
    return r


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
            result = run_conversation_scenario(page)

    shutil.rmtree(profile, ignore_errors=True)
    result["viewport"], result["size"] = viewport, VIEWPORTS[viewport]
    return result


def assert_usable_workspace(snap):
    # The chat fills the window; the transcript is the only vertical scroller
    # and the composer sits fully visible at the bottom.
    assert abs(snap["appHeight"] - snap["innerHeight"]) <= 1
    assert snap["conv"]["overflowY"] == "auto"
    assert snap["docScroll"][0] <= snap["docScroll"][1] and snap["scrollY"] == 0
    composer = snap["composer"]
    assert 0 <= composer["top"] and composer["bottom"] <= snap["innerHeight"] + 1
    # The transcript ends above the composer.
    conv_bottom = snap["conv"]["rect"][1] + snap["conv"]["rect"][3]
    assert conv_bottom <= composer["top"] + 1, (conv_bottom, composer)
    assert snap["docScrollWidth"] <= snap["innerWidth"]


def test_direct_app_is_a_full_height_conversation(app_conversation):
    run = app_conversation
    width, height = run["size"]
    snap = run["afterStop"]

    assert run["initial"]["appHeight"] == height and run["initial"]["innerWidth"] == width
    assert snap["appHeight"] == height and snap["innerHeight"] == height
    # Every step, including reading earlier messages, keeps the composer in
    # view and the scrolling inside the transcript.
    for label in LABELS:
        assert_usable_workspace(run[label])
    # Every step settled -- the reply in, the transcript still -- well within 5 seconds.
    assert all(ms >= 0 for ms in run["settled"].values()), run["settled"]
    # A long reply overflows the transcript, which follows it to the end, as it
    # does every completed reply, error and stop...
    long = run["longReplied"]
    assert long["conv"]["scrollHeight"] > long["conv"]["clientHeight"]
    for label in ("firstReplied", "longReplied", "afterError", "afterStop"):
        conv = run[label]["conv"]
        assert conv["scrollTop"] + conv["clientHeight"] >= conv["scrollHeight"] - 2, (label, conv)
    # ...and earlier messages stay readable: scrolled back to the top, nothing moves it.
    assert run["readingEarlier"]["conv"]["scrollTop"] == 0
    assert run["readingEarlier"]["users"] == snap["users"] and run["readingEarlier"]["requests"] == snap["requests"]
    # From the first exchange on, the composer stays pinned at the same place.
    base = run["firstReplied"]["composer"]
    for label in ("firstReplied", "longReplied", "afterError", "afterStop", "readingEarlier"):
        composer = run[label]["composer"]
        assert abs(composer["bottom"] - base["bottom"]) <= 1 and abs(composer["top"] - base["top"]) <= 1, label
    assert run["inFlight"]["sendIsStop"] is True and run["inFlight"]["sendCursor"] == "pointer"
    assert run["afterInvalid"]["requests"] == []
    assert run["idle"]["emptyPresent"] and not run["firstReplied"]["emptyPresent"]
    # Nothing navigates or adds history.
    for label in LABELS:
        assert run[label]["path"] == "/app/" and run[label]["historyLength"] == run["idle"]["historyLength"], label


def test_direct_app_conversation_turns_errors_stop_and_invalid_submissions(app_conversation):
    run = app_conversation
    # Invalid and empty submissions send nothing and leave the starting state.
    invalid = run["afterInvalid"]
    assert invalid["requests"] == [] and invalid["users"] == [] and invalid["assistantRows"] == 0
    assert invalid["emptyPresent"] is True
    # The first prompt is sent once and answered in place.
    assert run["firstImmediate"]["requests"] == ["First question"]
    assert run["firstImmediate"]["users"] == ["First question"] and run["firstImmediate"]["assistantRows"] == 1
    assert run["firstReplied"]["actions"][:2] == ["Copy", "Retry"]
    # Errors and Stop keep the conversation; a new send replaces a failed exchange on screen.
    assert run["afterError"]["notes"][-1] == "Kalillac is temporarily unavailable. Please try again shortly."
    assert run["afterError"]["users"] == ["First question", "LONG: please write a long answer",
                                          "ERROR: this one fails"]
    assert run["afterStop"]["notes"][-1] == "Stopped."
    assert run["afterStop"]["users"] == ["First question", "LONG: please write a long answer", "HANG: wait for Stop"]
    assert run["afterStop"]["assistantRows"] == 3
    assert run["afterStop"]["actions"].count("Copy") == 2 and "Retry" in run["afterStop"]["actions"]
    assert run["afterStop"]["requests"] == ["First question", "LONG: please write a long answer",
                                            "ERROR: this one fails", "HANG: wait for Stop"]
    assert run["afterStop"]["sendIsStop"] is False
    # The starting state is gone from the first message on.
    for label in LABELS[2:]:
        assert run[label]["emptyPresent"] is False, label


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


def test_example_prompts_only_fill_the_composer_in_source():
    from html.parser import HTMLParser
    class Starters(HTMLParser):
        def __init__(self):
            super().__init__(); self.cards = []
        def handle_starttag(self, tag, attributes):
            a = dict(attributes)
            if tag == "button" and "hint" in a.get("class", "").split():
                self.cards.append((a["aria-label"], a["data-prompt"], a["type"]))
    parser = Starters(); parser.feed(INDEX_HTML.read_text(encoding="utf-8"))
    assert parser.cards == [(label, prompt, "button") for label, prompt in zip(TASK_LABELS, EXAMPLE_PROMPTS)]
    chat = APP_JS.read_text(encoding="utf-8")
    handler = chat[chat.index('var hints = document.querySelectorAll(".hint");'):]
    handler = handler[handler.index('addEventListener("click"'):]
    handler = handler[:handler.index("});") + 3]
    assert 'input.value = this.getAttribute("data-prompt") || "";' in handler
    assert "input.focus();" in handler
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


def test_app_frontend_and_site_dependencies_are_untouched():
    # /app/ may differ only in the composer CSS, the composer JavaScript and
    # their cache-busting references -- nothing else under frontend/.
    frontend = git_diff_names("frontend")
    if frontend is None:
        pytest.skip(f"git or the slice base commit {SLICE_BASE} is unavailable")
    # Plus exactly the two vendored highlight.js files for code highlighting
    # (pinned by hash in test_frontend_error_contract.py).
    assert set(frontend) <= {"frontend/app.css", "frontend/app.js", "frontend/index.html",
                             "frontend/vendor/highlight/highlight.min.js",
                             "frontend/vendor/highlight/LICENSE"}, frontend

    names = git_diff_names(
        "site/package.json", "site/package-lock.json", "site/vite.config.ts", "site/index.html",
        "site/deployment-preserve-live-assets.txt", "site/public",
    )
    if names is None:
        pytest.skip(f"git or the slice base commit {SLICE_BASE} is unavailable")
    # The domain migration deliberately changes only these build-boundary
    # files. Verify their complete contents against the old baseline plus
    # the exact reviewed edits, rather than relaxing the dependency guard.
    approved = {
        "site/vite.config.ts", "site/index.html",
        "site/deployment-preserve-live-assets.txt",
    }
    assert set(names) == approved, names
    for name in approved:
        baseline = subprocess.check_output(
            [shutil.which("git"), "show", f"{SLICE_BASE}:{name}"], cwd=REPO,
        ).decode("utf-8")
        if name.endswith("vite.config.ts"):
            expected = baseline.replace(
                "import { defineConfig } from 'vite';",
                "import { defineConfig } from 'vite';\nimport { publicMetadata } from './public-metadata';",
            ).replace(
                "plugins: [react(), tailwindcss()],",
                "plugins: [react(), tailwindcss(), publicMetadata()],",
            ).replace(
                "// at /app/ and embedded here as a same-origin iframe.",
                "// at /app/. The homepage never loads it; its Send opens it.",
            )
        elif name.endswith("index.html"):
            expected = baseline.replace(
                '    <meta property="og:type" content="website" />',
                '    <meta property="og:type" content="website" />\n'
                '    <link rel="canonical" href="https://kalillac.ai/" />\n'
                '    <meta property="og:url" content="https://kalillac.ai/" />\n'
                '    <meta property="og:image" content="https://kalillac.ai/assets/kalillac-ai-logo.png" />\n'
                '    <meta property="og:image:alt" content="Kalillac AI" />',
            ).replace(
                '    <meta name="twitter:card" content="summary_large_image" />',
                '    <meta name="twitter:card" content="summary_large_image" />\n'
                '    <meta name="twitter:image" content="https://kalillac.ai/assets/kalillac-ai-logo.png" />\n'
                '    <meta name="twitter:image:alt" content="Kalillac AI" />',
            )
        else:
            expected = baseline.replace("/robots.txt\n", "").replace("/sitemap.xml\n", "")
        assert (REPO / name).read_text(encoding="utf-8") == expected, name

    package = json.loads((SITE / "package.json").read_text(encoding="utf-8"))
    assert set(package["dependencies"]) == {"lucide-react", "react", "react-dom", "wouter"}


def test_tools_and_privacy_promise_match_pre_subscription_scope(structure):
    assert structure["tools"] == list(TASK_LABELS)
    promise = structure["promise"]
    assert promise["heading"] == "Your conversation stays temporary."
    assert promise["count"] == 4
    assert "Current conversation context is also held temporarily" in promise["text"]
    assert "does not immediately delete" in promise["text"]
    assert "not a promise of complete anonymity or no third-party processing" in promise["text"]
    for label in ("Pricing", "Sign in", "Create account", "Upgrade", "Checkout", "Buy Me a Coffee"):
        assert label not in structure["pageText"]


# ---- End & erase: discoverable and operable everywhere the chat appears --------------------------
# The control's label is visible text (never only a tooltip or a screen-reader
# label) in the /app/ workspace, down to a 360 x 640 phone, by keyboard and by
# touch, with and without reduced motion.
# Nothing is erased here: every path ends at Cancel or Escape, and nothing is
# sent.

ERASE_STATE = r"""const c = window.__cx, b = c.el('erase-btn'), label = b.querySelector('.erase-btn-label');
  const cs = c.win().getComputedStyle(b), ls = c.win().getComputedStyle(label), dialog = c.el('erase-dialog');
  return { button: c.abs(b), label: c.abs(label), shell: c.abs(c.el('composer-shell')),
    input: c.abs(c.el('composer-input')), send: c.abs(c.el('send-btn')),
    labelText: label.textContent, labelDisplay: ls.display, labelVisibility: ls.visibility,
    labelOpacity: ls.opacity, labelClip: ls.clip, labelClipPath: ls.clipPath,
    labelFontSize: parseFloat(ls.fontSize), labelTruncated: label.scrollWidth > label.clientWidth + 1,
    disabled: b.disabled, transition: cs.transitionDuration,
    focusIsButton: c.doc().activeElement === b, focusVisible: b.matches(':focus-visible'),
    outlineStyle: cs.outlineStyle,
    dialogOpen: dialog.open, focusIsCancel: c.doc().activeElement === c.el('erase-dialog-cancel'),
    cancel: dialog.open ? c.abs(c.el('erase-dialog-cancel')) : null,
    embedded: c.doc().body.classList.contains('is-embedded'),
    innerWidth, innerHeight, docScrollWidth: document.documentElement.scrollWidth,
    childInnerWidth: c.win().innerWidth, childScrollWidth: c.doc().documentElement.scrollWidth,
    ...c.state() };"""

ERASE_VIEWPORTS = {
    # name: (page, size, phone, reduced motion)
    "direct-desktop": ("/app/", VIEWPORTS["desktop"], False, False),
    "direct-mobile-reduced-motion": ("/app/", VIEWPORTS["mobile"], True, True),
    "direct-360x640": ("/app/", (360, 640), True, False),
    "direct-desktop-reduced-motion": ("/app/", VIEWPORTS["desktop"], False, True),
}


def key(page: Page, name: str, code: str, vk: int, modifiers: int = 0):
    """A real (trusted) key press, delivered to whatever has focus."""
    for kind in ("rawKeyDown", "keyUp"):
        page.dt.call("Input.dispatchKeyEvent", {"type": kind, "key": name, "code": code,
                                                "windowsVirtualKeyCode": vk, "modifiers": modifiers},
                     page.session)


def run_erase_scenario(page: Page) -> dict:
    r: dict = {}
    page.eval(COMPOSER_HELPERS)
    page.eval("""for (let i = 0; i < 400 && !(window.__cx.doc() && window.__cx.el('composer-input')); i++)
        await window.__cx.wait(25);
      await window.__cx.wait(300); return true;""")

    def state():
        return page.eval(ERASE_STATE)

    r["idle"] = state()
    page.eval("""window.__cx.setValue('a draft to keep');
      window.__cx.el('erase-btn').scrollIntoView({block: 'center'});
      await window.__cx.wait(900); return true;""")
    r["draft"] = state()

    # Keyboard only: from the message field, Shift+Tab reaches End & erase,
    # Enter opens the dialog with Cancel focused, Escape closes it.
    page.eval("window.__cx.el('composer-input').focus(); await window.__cx.wait(100); return true;")
    key(page, "Tab", "Tab", 9, modifiers=8)
    time.sleep(0.2)
    r["tabbed"] = state()
    press(page, "Enter", "Enter", 13, text="\r")
    time.sleep(0.3)
    r["keyboardOpen"] = state()
    key(page, "Escape", "Escape", 27)
    time.sleep(0.3)
    r["keyboardClosed"] = state()

    # Touch or mouse: a real press on the visible control, then on Cancel.
    button = r["keyboardClosed"]["button"]
    page.click(button["left"] + button["width"] / 2, button["top"] + button["height"] / 2)
    time.sleep(0.3)
    r["pressOpen"] = state()
    cancel = r["pressOpen"]["cancel"]
    if cancel:
        page.click(cancel["left"] + cancel["width"] / 2, cancel["top"] + cancel["height"] / 2)
        time.sleep(0.3)
    r["pressClosed"] = state()
    return r


@pytest.fixture(scope="module", params=list(ERASE_VIEWPORTS))
def erase_control(request, tmp_path_factory):
    browser = require_browser_and_build()
    path, size, phone, reduced = ERASE_VIEWPORTS[request.param]
    profile = tmp_path_factory.mktemp(f"edge-erase-{request.param}")
    site = Site()
    site.child_passive = path == "/app/"

    with site.http:
        with browser_page(browser, profile, size, phone, reduced_motion=reduced) as (devtools, session):
            page = Page(devtools, session)
            devtools.call("Page.navigate", {"url": site.http.origin + path}, session)
            time.sleep(0.5)
            result = run_erase_scenario(page)

    shutil.rmtree(profile, ignore_errors=True)
    result.update(name=request.param, path=path, size=size, reduced=reduced)
    return result


def inside(inner: dict, outer: dict, slack: float = 0.5) -> bool:
    return (inner["left"] >= outer["left"] - slack and inner["right"] <= outer["right"] + slack
            and inner["top"] >= outer["top"] - slack and inner["bottom"] <= outer["bottom"] + slack)


def test_erase_label_is_visible_text_that_fits_beside_the_composer(erase_control):
    s = erase_control["draft"]
    assert s["embedded"] is False and erase_control["path"] == "/app/"
    assert s["labelText"] == "End & erase"
    assert s["labelDisplay"] != "none" and s["labelVisibility"] == "visible"
    assert float(s["labelOpacity"]) == 1 and s["labelClip"] in ("auto", "") and s["labelClipPath"] == "none"
    assert s["labelFontSize"] >= 12 and s["label"]["width"] >= 40 and s["labelTruncated"] is False
    assert inside(s["label"], s["button"]) and inside(s["button"], s["shell"])
    # A comfortable control that never overlaps the message field or Send. In
    # a phone-width frame the field takes the first row and End & erase and
    # Send share the second; otherwise all three share one row.
    assert s["button"]["height"] >= 36
    assert inside(s["send"], s["shell"]) and s["button"]["right"] < s["send"]["left"]
    if s["childInnerWidth"] <= 400:
        assert s["input"]["bottom"] <= s["button"]["top"] and s["input"]["bottom"] <= s["send"]["top"]
        assert abs(s["button"]["top"] - s["send"]["top"]) <= 1
        assert s["input"]["width"] >= s["shell"]["width"] - 40, s["input"]
    else:
        assert s["button"]["right"] <= s["input"]["left"] and s["input"]["right"] <= s["send"]["left"]
        assert s["input"]["width"] >= 200, s["input"]
    assert s["button"]["top"] >= 0 and s["button"]["bottom"] <= s["innerHeight"]   # on screen
    assert s["docScrollWidth"] <= s["innerWidth"] and s["childScrollWidth"] <= s["childInnerWidth"]


def test_erase_is_disabled_only_without_anything_to_erase(erase_control):
    assert erase_control["idle"]["disabled"] is True
    assert erase_control["idle"]["labelText"] == "End & erase"      # still visible while disabled
    assert erase_control["draft"]["disabled"] is False


def test_erase_is_reachable_and_operable_by_keyboard(erase_control):
    tabbed = erase_control["tabbed"]
    assert tabbed["focusIsButton"] is True and tabbed["focusVisible"] is True
    assert tabbed["outlineStyle"] != "none"
    opened = erase_control["keyboardOpen"]
    assert opened["dialogOpen"] is True and opened["focusIsCancel"] is True   # the safe choice first
    closed = erase_control["keyboardClosed"]
    assert closed["dialogOpen"] is False and closed["focusIsButton"] is True
    assert closed["value"] == "a draft to keep"


def test_erase_opens_with_a_real_press_and_cancel_keeps_everything(erase_control):
    opened = erase_control["pressOpen"]
    assert opened["dialogOpen"] is True and opened["focusIsCancel"] is True
    closed = erase_control["pressClosed"]
    assert closed["dialogOpen"] is False and closed["value"] == "a draft to keep"
    assert closed["requests"] == []                                  # nothing was sent


def test_erase_control_follows_the_motion_preference(erase_control):
    duration = erase_control["draft"]["transition"]
    parts = duration.replace(" ", "").split(",")
    if erase_control["reduced"]:
        assert set(parts) == {"0s"}, duration
    else:
        assert any(part != "0s" for part in parts), duration


def test_header_offset_and_cta_landing_share_one_css_token():
    css = SITE_CSS.read_text(encoding="utf-8")
    app = APP_TSX.read_text(encoding="utf-8")
    assert ".site-shell { --k-header-h: calc(72px + env(safe-area-inset-top, 0px)); }" in css
    assert ".site-shell { --k-header-h: calc(64px + env(safe-area-inset-top, 0px)); }" in css
    assert "height: var(--k-header-h); padding-top: env(safe-area-inset-top, 0px);" in css
    assert "scroll-margin-top: calc(var(--k-header-h) + 16px);" in css
    header = css[css.index(".site-header { position: sticky;"):]
    header = header[: header.index("}")]
    assert "background: #fff;" in header and "backdrop-filter" not in header
    assert "parseFloat(getComputedStyle(slot).scrollMarginTop)" in app


# --- the homepage composer (native, trusted input) --------------------------------------------------

HOME_HELPERS = r"""
window.__hx = {
  wait: ms => new Promise(r => setTimeout(r, ms)),
  input: () => document.getElementById('home-composer-input'),
  shell: () => document.querySelector('.hc-shell'),
  send: () => document.querySelector('[data-testid="button-home-send"]'),
  clear: () => document.querySelector('[data-testid="button-home-clear"]'),
  cards: () => [...document.querySelectorAll('[data-testid="home-card"]')],
  header: () => document.querySelector('.site-header'),
  rect: el => { const b = el.getBoundingClientRect();
    return {left: b.left, top: b.top, right: b.right, bottom: b.bottom, width: b.width, height: b.height}; },
  setValue: v => { const i = window.__hx.input();
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(i, v);
    i.dispatchEvent(new Event('input', {bubbles: true})); },
  blurAll: () => { if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur(); },
  state: () => { const i = window.__hx.input(), t = s => document.querySelector(s).textContent;
    return { value: i.value, placeholder: i.placeholder, focused: document.activeElement === i, readOnly: i.readOnly,
             invalid: i.getAttribute('aria-invalid'), sendDisabled: window.__hx.send().disabled,
             clearDisabled: window.__hx.clear().disabled, status: t('[data-testid="home-composer-status"]'),
             alert: t('[data-testid="home-composer-alert"]'),
             count: (document.querySelector('[data-testid="home-composer-count"]') || {}).textContent || null,
             path: location.pathname, href: location.href, historyLength: history.length,
             session: Object.keys(sessionStorage), local: localStorage.length, cookie: document.cookie }; },
  sample: async (ms, every) => { const out = [];
    for (let t = 0; t <= ms; t += every) { out.push(Math.round(window.scrollY)); await window.__hx.wait(every); }
    return out; },
};
return true;
"""


def key_press(page: Page, key: str, code: str, vk: int, text: str | None = None, modifiers: int = 0):
    down = {"type": "keyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk, "modifiers": modifiers}
    if text is not None:
        down["text"] = text
    page.dt.call("Input.dispatchKeyEvent", down, page.session)
    page.dt.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk,
                                            "modifiers": modifiers}, page.session)


def center(box: dict) -> tuple[float, float]:
    return box["left"] + box["width"] / 2, box["top"] + box["height"] / 2


def run_home_scenario(page: Page, reduced: bool) -> dict:
    r: dict = {"reduced": reduced}
    page.eval(HOME_HELPERS)
    page.eval("for (let i = 0; i < 400 && !window.__hx.input(); i++) await window.__hx.wait(25);"
              "await window.__hx.wait(400); return true;")
    st = lambda: page.eval("return window.__hx.state();")  # noqa: E731

    # ---- landing ----------------------------------------------------------------------------------
    r["landing"] = page.eval("""const hx = window.__hx, i = hx.input();
      const name = el => el.getAttribute('aria-label') || (el.labels && el.labels.length ? el.labels[0].textContent
        : el.textContent.replace(/\\s+/g, ' ').trim());
      return { scrollY, innerWidth, innerHeight,
        docScrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth,
        header: hx.rect(hx.header()), panel: hx.rect(document.querySelector('[data-testid="home-composer"]')),
        shell: hx.rect(hx.shell()), input: hx.rect(i), send: hx.rect(hx.send()), clear: hx.rect(hx.clear()),
        cta: hx.rect(document.querySelector('[data-testid="button-start-session"]')),
        trustItems: [...document.querySelectorAll('.hero-under-note .trust-item')].map(e =>
          ({...hx.rect(e), fontSize: parseFloat(getComputedStyle(e).fontSize)})),
        cards: hx.cards().map(c => ({ ...hx.rect(c), tag: c.tagName, type: c.type, name: name(c),
          description: document.getElementById(c.getAttribute('aria-describedby')).textContent,
          href: c.getAttribute('href') })),
        labels: [...i.labels].map(l => l.textContent), inputTag: i.tagName, rows: i.rows,
        describedBy: document.getElementById(i.getAttribute('aria-describedby')).textContent,
        sendName: name(hx.send()), sendType: hx.send().type, clearName: name(hx.clear()),
        statusRole: document.querySelector('[data-testid="home-composer-status"]').getAttribute('role'),
        alertRole: document.querySelector('[data-testid="home-composer-alert"]').getAttribute('role'),
        links: [...document.querySelectorAll('[data-testid="home-composer"] a')].length,
        controls: [...document.querySelectorAll('[data-testid="home-composer"] button, [data-testid="home-composer"] input, ' +
                                                '[data-testid="home-composer"] textarea, [data-testid="home-composer"] select')]
          .map(e => e.tagName + ':' + (e.type || '') + ':' + name(e)),
        eyebrow: (() => { const e = document.querySelector('.hero .eyebrow'), s = getComputedStyle(e),
                                 b = getComputedStyle(e, '::before');
          return { text: e.textContent, tag: e.tagName, background: s.backgroundColor, radius: s.borderRadius,
                   ruleContent: b.content, ruleWidth: parseFloat(b.width), ruleHeight: parseFloat(b.height),
                   ruleColor: b.backgroundColor }; })(),
        ...hx.state() };""")

    # ---- pointer, caret and decoration ------------------------------------------------------------
    r["styles"] = page.eval("""const hx = window.__hx, cs = el => getComputedStyle(el);
      const out = { inputCursor: cs(hx.input()).cursor, shellCursor: cs(hx.shell()).cursor,
        caretColor: cs(hx.input()).caretColor, inputColor: cs(hx.input()).color,
        shellBackground: cs(hx.shell()).backgroundColor, sendCursorEmpty: cs(hx.send()).cursor,
        cardCursors: hx.cards().map(c => cs(c).cursor),
        ctaCursor: cs(document.querySelector('[data-testid="button-start-session"]')).cursor,
        decorative: ['.orbital-wrap', '.hero-signal-line', '.phone-stage'].map(s =>
          [s, [...document.querySelectorAll(s)].map(e => cs(e).pointerEvents)]),
        plate: getComputedStyle(document.querySelector('.hero-workspace'), '::before').pointerEvents };
      hx.setValue('draft'); await hx.wait(50);
      out.sendWithDraft = [cs(hx.send()).cursor, hx.send().disabled, cs(hx.clear()).cursor, hx.clear().disabled];
      hx.setValue(''); await hx.wait(50); return out;""")

    # ---- the visible typing area focuses the real field (trusted clicks) ------------------------
    page.eval("""const hx = window.__hx, s = hx.shell().getBoundingClientRect(), h = hx.header().getBoundingClientRect();
      if (s.top < h.bottom || s.bottom > innerHeight) window.scrollTo({top: scrollY + s.top - h.bottom - 24, behavior: 'instant'});
      await hx.wait(150); return true;""")
    box = page.eval("return window.__hx.rect(window.__hx.input());")
    r["typingAreaClicks"] = []
    for fx in (0.05, 0.5, 0.95):
        for fy in (0.25, 0.75):
            page.eval("window.__hx.blurAll(); return true;")
            page.click(box["left"] + box["width"] * fx, box["top"] + box["height"] * fy)
            time.sleep(0.12)
            r["typingAreaClicks"].append(st())

    # ---- typing, a new line, Unicode, editing and Clear draft -------------------------------------
    page.eval("window.__hx.blurAll(); window.__hx.input().focus(); return true;")
    page.dt.call("Input.insertText", {"text": "first line"}, page.session)
    key_press(page, "Enter", "Enter", 13, text="\r", modifiers=8)        # Shift+Enter
    page.dt.call("Input.insertText", {"text": "second \u2713 \U0001F642 \u65e5\u672c\u8a9e"}, page.session)
    time.sleep(0.2)
    r["typed"] = st()
    page.eval("window.__hx.input().select(); return true;")
    page.dt.call("Input.insertText", {"text": "replaced"}, page.session)
    time.sleep(0.2)
    r["edited"] = st()
    page.click(*center(page.eval("return window.__hx.rect(window.__hx.clear());")))
    time.sleep(0.25)
    r["cleared"] = st()

    # ---- nothing empty, blank or too long can be sent --------------------------------------------
    page.eval("window.__hx.setValue('   \\n\\t  '); window.__hx.input().focus(); return true;")
    time.sleep(0.1)
    r["whitespace"] = st()
    key_press(page, "Enter", "Enter", 13, text="\r")
    page.click(*center(page.eval("return window.__hx.rect(window.__hx.send());")))
    time.sleep(0.4)
    r["whitespaceAfterSend"] = st()
    page.eval("window.__hx.setValue('x'.repeat(4001)); return true;")
    time.sleep(0.1)
    r["overLimit"] = st()
    page.eval("window.__hx.setValue('\\u{1F600}'.repeat(4000)); return true;")
    time.sleep(0.1)
    r["atLimitCodePoints"] = st()
    page.eval("window.__hx.setValue(''); return true;")

    # ---- an IME still composing never sends ------------------------------------------------------
    r["ime"] = page.eval("""const hx = window.__hx, i = hx.input();
      hx.setValue('\\u6f22\\u5b57'); i.focus();
      i.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
      i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, bubbles: true, cancelable: true}));
      await hx.wait(150); const during = hx.state();
      i.dispatchEvent(new CompositionEvent('compositionend', {bubbles: true, data: '\\u6f22\\u5b57'}));
      i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true, cancelable: true}));
      i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 229, bubbles: true, cancelable: true}));
      await hx.wait(150); const after = hx.state(); hx.setValue(''); return { during, after };""")

    # ---- the examples: focus and suggest, never navigate or overwrite ---------------------------
    r["cards"] = []
    for index in range(4):
        page.eval("window.__hx.setValue(''); window.__hx.blurAll(); return true;")
        page.eval(f"window.__hx.cards()[{index}].scrollIntoView({{block: 'center', behavior: 'instant'}}); "
                  "await window.__hx.wait(80); return true;")
        page.click(*center(page.eval(f"return window.__hx.rect(window.__hx.cards()[{index}]);")))
        time.sleep(0.15)
        empty = st()
        page.eval("window.__hx.setValue('keep my draft'); window.__hx.blurAll(); return true;")
        page.click(*center(page.eval(f"return window.__hx.rect(window.__hx.cards()[{index}]);")))
        time.sleep(0.15)
        r["cards"].append({"empty": empty, "draft": st()})
    page.eval("window.__hx.setValue(''); window.__hx.blurAll(); return true;")

    # ---- "Start a private session" from the top, then from the bottom of the page ---------------
    page.eval("window.scrollTo({top: 0, behavior: 'instant'}); await window.__hx.wait(150); return true;")
    page.click(*center(r["landing"]["cta"]))
    r["ctaFromTop"] = page.eval("""const hx = window.__hx, samples = await hx.sample(1500, 50);
      return { samples, header: hx.rect(hx.header()), shell: hx.rect(hx.shell()), innerHeight, ...hx.state() };""")
    r["ctaFromBottom"] = page.eval("""const hx = window.__hx; hx.blurAll();
      window.scrollTo({top: document.documentElement.scrollHeight, behavior: 'instant'}); await hx.wait(150);
      const start = Math.round(scrollY);
      document.querySelector('[data-testid="button-start-session"]').click();
      const samples = await hx.sample(1800, 30);
      return { start, samples, header: hx.rect(hx.header()), shell: hx.rect(hx.shell()), innerHeight, ...hx.state() };""")

    # ---- keyboard: the composer, then the examples, every stop with a visible ring --------------
    # A press on the header's empty edge starts keyboard navigation from the top of the page.
    page.eval("window.__hx.blurAll(); window.scrollTo({top: 0, behavior: 'instant'}); await window.__hx.wait(100); "
              "return true;")
    page.click(2, 2)
    r["tabStops"] = []
    for _ in range(30):
        key_press(page, "Tab", "Tab", 9)
        time.sleep(0.06)
        stop = page.eval("""const e = document.activeElement, s = getComputedStyle(e), b = e.getBoundingClientRect();
          const shell = e.closest('.hc-shell');
          // The box's border settles after its 160 ms transition.
          if (shell) await new Promise(r => setTimeout(r, 300));
          const ss = shell && getComputedStyle(shell);
          return { id: e.id, testid: e.getAttribute('data-testid'), tag: e.tagName,
                   outlineStyle: s.outlineStyle, outlineWidth: parseFloat(s.outlineWidth),
                   shellBorder: ss ? ss.borderTopColor : null, shellRing: ss ? ss.boxShadow : null,
                   onScreen: b.top >= 0 && b.bottom <= innerHeight + 1 };""")
        r["tabStops"].append(stop)
        if stop["testid"] == "home-card" and sum(1 for s in r["tabStops"] if s["testid"] == "home-card") == 4:
            break

    r["final"] = page.eval("return { docScrollWidth: document.documentElement.scrollWidth, innerWidth, "
                           "...window.__hx.state() };")
    return r


@pytest.fixture(scope="module", params=[
    ("desktop", False), ("desktop", True), ("mobile", False), ("mobile", True),
], ids=["desktop", "desktop-reduced-motion", "mobile", "mobile-reduced-motion"])
def homepage(request, tmp_path_factory):
    browser = require_browser_and_build()
    viewport, reduced = request.param
    profile = tmp_path_factory.mktemp(f"edge-home-{viewport}-{int(reduced)}")
    site = Site()
    site.child_passive = True

    with site.http:
        with browser_page(browser, profile, VIEWPORTS[viewport], viewport == "mobile",
                          reduced_motion=reduced) as (devtools, session):
            page = Page(devtools, session)
            devtools.call("Page.navigate", {"url": site.http.origin + "/"}, session)
            time.sleep(0.5)
            result = run_home_scenario(page, reduced)
        result["serverPaths"] = list(site.http.paths)

    shutil.rmtree(profile, ignore_errors=True)
    result["viewport"], result["size"] = viewport, VIEWPORTS[viewport]
    return result


def test_lands_at_the_top_with_the_header_visible_and_no_horizontal_overflow(homepage):
    landing = homepage["landing"]
    assert landing["scrollY"] == 0 and landing["header"]["top"] == 0
    assert landing["docScrollWidth"] <= landing["innerWidth"] and landing["bodyScrollWidth"] <= landing["innerWidth"]
    assert landing["focused"] is False and landing["value"] == "" and landing["path"] == "/"


def test_eyebrow_is_editorial_text_with_a_short_blue_rule(homepage):
    eyebrow = homepage["landing"]["eyebrow"]
    assert eyebrow["tag"] == "SPAN" and eyebrow["text"] == "PRIVATE BY DESIGN"
    assert eyebrow["background"] == "rgba(0, 0, 0, 0)" and eyebrow["radius"] == "0px"
    assert eyebrow["ruleContent"] == '""' and eyebrow["ruleWidth"] == 28 and eyebrow["ruleHeight"] == 2
    assert eyebrow["ruleColor"] == "rgb(0, 105, 255)"


def test_complete_composer_is_visible_on_the_first_desktop_screen(homepage):
    landing = homepage["landing"]
    if homepage["viewport"] != "desktop":
        pytest.skip("the side-by-side first screen applies at 1440 x 900 and wider")
    panel = landing["panel"]
    assert panel["top"] >= landing["header"]["bottom"] and panel["bottom"] <= landing["innerHeight"], panel
    for card in landing["cards"]:
        assert card["bottom"] <= landing["innerHeight"]


def test_composer_is_a_real_accessible_message_box_with_truthful_controls(homepage):
    landing = homepage["landing"]
    assert landing["inputTag"] == "TEXTAREA" and landing["labels"] == ["Message Kalillac AI"] and landing["rows"] >= 2
    assert landing["describedBy"] == "Nothing is sent until you press Send. Sending opens the chat."
    assert landing["sendName"] == "Send message" and landing["sendType"] == "submit"
    assert landing["clearName"] == "Clear draft" and "End & erase" not in landing["clearName"]
    assert landing["statusRole"] == "status" and landing["alertRole"] == "alert"
    # Nothing to send or clear yet: both are disabled until there is a draft.
    assert landing["sendDisabled"] is True and landing["clearDisabled"] is True
    # The composer contains no link: nothing in it can navigate.
    assert landing["links"] == 0
    for card in landing["cards"]:
        assert card["tag"] == "BUTTON" and card["type"] == "button" and card["href"] is None
    assert [c["name"] for c in landing["cards"]] == list(TASK_LABELS)
    for control in (landing["send"], landing["clear"], *landing["cards"]):
        assert control["height"] >= 36 and control["width"] >= 36


def test_no_unsupported_control_is_exposed_on_the_homepage(homepage):
    controls = homepage["landing"]["controls"]
    assert controls == ["TEXTAREA:textarea:Message Kalillac AI", "BUTTON:button:Clear draft",
                        "BUTTON:submit:Send message",
                        *[f"BUTTON:button:{label}" for label in TASK_LABELS]], controls
    for control in controls:
        for unsupported in ("file", "attach", "upload", "image", "photo", "voice", "microphone", "plan", "upgrade"):
            assert unsupported not in control.lower(), control


def test_trust_line_statements_stay_whole_on_every_viewport(homepage):
    for item in homepage["landing"]["trustItems"]:
        assert item["height"] <= item["fontSize"] * 1.8, item       # one line each
        assert item["right"] <= homepage["landing"]["innerWidth"]


def test_pointer_and_caret_styles(homepage):
    styles = homepage["styles"]
    assert styles["inputCursor"] == "text" and styles["shellCursor"] == "text"
    assert styles["caretColor"] == styles["inputColor"]
    light, dark = sorted((luminance(styles["caretColor"]), luminance(styles["shellBackground"])))
    assert (dark + 0.05) / (light + 0.05) >= 7
    assert styles["sendCursorEmpty"] == "default"
    assert styles["sendWithDraft"] == ["pointer", False, "pointer", False]
    assert styles["cardCursors"] == ["pointer"] * 4 and styles["ctaCursor"] == "pointer"
    for _, values in styles["decorative"]:
        assert all(v == "none" for v in values)
    assert styles["plate"] == "none"


def test_clicking_anywhere_in_the_visible_input_focuses_it_and_stays_on_the_homepage(homepage):
    for state in homepage["typingAreaClicks"]:
        assert state["focused"] is True and state["path"] == "/" and state["session"] == [], state


def test_typing_new_lines_unicode_editing_and_clearing_stay_on_the_homepage(homepage):
    typed = homepage["typed"]
    assert typed["value"] == "first line\nsecond \u2713 \U0001F642 \u65e5\u672c\u8a9e" and typed["path"] == "/"
    assert typed["sendDisabled"] is False and typed["clearDisabled"] is False and typed["session"] == []
    assert homepage["edited"]["value"] == "replaced" and homepage["edited"]["path"] == "/"
    cleared = homepage["cleared"]
    assert cleared["value"] == "" and cleared["focused"] is True and cleared["path"] == "/"
    assert cleared["status"] == "Draft cleared." and cleared["sendDisabled"] is True and cleared["session"] == []


def test_empty_whitespace_and_over_limit_drafts_cannot_send(homepage):
    assert homepage["whitespace"]["sendDisabled"] is True
    after = homepage["whitespaceAfterSend"]
    assert after["path"] == "/" and after["session"] == [] and after["readOnly"] is False
    over = homepage["overLimit"]
    assert over["sendDisabled"] is True and over["invalid"] == "true"
    assert "too long" in over["status"] and over["count"] == "4,001 / 4,000"
    # The limit counts code points, as the chat service does: 4,000 emoji may be sent.
    at_limit = homepage["atLimitCodePoints"]
    assert at_limit["sendDisabled"] is False and at_limit["invalid"] is None and at_limit["count"] == "4,000 / 4,000"


def test_ime_composition_never_sends(homepage):
    for phase in ("during", "after"):
        state = homepage["ime"][phase]
        assert state["path"] == "/" and state["session"] == [] and state["value"] == "\u6f22\u5b57", (phase, state)


def test_examples_focus_the_composer_suggest_only_when_empty_and_never_navigate(homepage):
    for index, card in enumerate(homepage["cards"]):
        empty, draft = card["empty"], card["draft"]
        assert empty["focused"] is True and empty["value"] == "" and empty["path"] == "/"
        assert empty["placeholder"] == f"Try: {EXAMPLE_PROMPTS[index]}"
        assert draft["focused"] is True and draft["value"] == "keep my draft" and draft["path"] == "/"
        assert empty["session"] == draft["session"] == []
        assert empty["historyLength"] == draft["historyLength"] == homepage["landing"]["historyLength"]


def test_cta_focuses_the_composer_without_navigation(homepage):
    for name in ("ctaFromTop", "ctaFromBottom"):
        run = homepage[name]
        assert run["focused"] is True and run["path"] == "/" and "#" not in run["href"], name
        assert run["historyLength"] == homepage["landing"]["historyLength"] and run["session"] == [], name
        # The composer box lands fully visible below the sticky header.
        assert run["shell"]["top"] >= run["header"]["bottom"] - 1, (name, run["shell"], run["header"])
        assert run["shell"]["bottom"] <= run["innerHeight"] + 1, (name, run["shell"])


def test_scroll_is_smooth_or_immediate_according_to_motion_preference(homepage):
    samples = homepage["ctaFromBottom"]["samples"]
    assert samples[-1] < homepage["ctaFromBottom"]["start"]
    moving = [s for s in samples if s not in (samples[0], samples[-1])]
    if homepage["reduced"]:
        assert moving == [], samples                    # one immediate jump
    else:
        assert len(moving) >= 2, samples               # a visible, smooth movement


def test_keyboard_reaches_the_cta_the_composer_then_the_examples_with_visible_focus(homepage):
    stops = homepage["tabStops"]
    ids = [s["testid"] or s["id"] for s in stops]
    assert "button-start-session" in ids and "input-home-composer" in ids
    composer = ids.index("input-home-composer")
    assert ids.index("button-start-session") < composer
    # The disabled Clear draft and Send are skipped; the four examples follow at once.
    assert ids[composer + 1:composer + 5] == ["home-card"] * 4, ids
    for stop in stops:
        if stop["testid"] == "input-home-composer":
            # The message box shows the field's focus: a Kalillac-blue border and a 3px ring.
            assert stop["shellBorder"] == "rgb(0, 105, 255)" and "0px 0px 0px 3px" in stop["shellRing"], stop
        else:
            assert stop["outlineStyle"] != "none" and stop["outlineWidth"] >= 2, stop


def test_nothing_was_sent_stored_or_navigated(homepage):
    final = homepage["final"]
    assert final["path"] == "/" and final["historyLength"] == homepage["landing"]["historyLength"]
    assert final["session"] == [] and final["local"] == 0 and final["cookie"] == ""
    assert final["docScrollWidth"] <= final["innerWidth"]
    assert not any(p.startswith("/app/") or p.startswith("/api/") or "POST" in p for p in homepage["serverPaths"])


# --- real 200% browser zoom ------------------------------------------------------------------------
#
# The browser's own page zoom (the profile zoom level that Ctrl+Plus and
# Settings > Page zoom set), in a 1440 x 900 window, with no viewport or
# device-pixel emulation. Driven with real key presses: Tab to the CTA, Enter,
# type, Enter -- which hands the draft to /app/.


@pytest.fixture(scope="module")
def real_zoom(tmp_path_factory):
    browser = require_browser_and_build()
    profile = tmp_path_factory.mktemp("edge-real-zoom-200")
    site = Site()
    site.child_passive = True
    r: dict = {}

    with site.http:
        with browser_page(browser, profile, (1440, 900), False, zoom_percent=200) as (devtools, session):
            page = Page(devtools, session)
            devtools.call("Page.navigate", {"url": site.http.origin + "/"}, session)
            time.sleep(0.5)
            page.eval(HOME_HELPERS)
            page.eval("for (let i = 0; i < 400 && !window.__hx.input(); i++) await window.__hx.wait(25);"
                      "await window.__hx.wait(400); return true;")
            r["viewport"] = page.eval("""return { innerWidth, innerHeight, dpr: devicePixelRatio,
              outer: [outerWidth, outerHeight], pinch: visualViewport.scale,
              docScrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth };""")
            r["scrollX"] = page.eval("window.scrollTo(400, 0); await window.__hx.wait(100); "
                                     "const x = window.scrollX; window.scrollTo(0, 0); return x;")
            r["clipped"] = {}
            height = page.eval("return document.documentElement.scrollHeight;")
            for y in range(0, height, 300):
                page.eval(f"window.scrollTo(0, {y}); await window.__hx.wait(30); return true;")
                for item in page.eval(ZOOM_CLIPPING):
                    r["clipped"].setdefault(item, y)
            r["diagram"] = page.eval("""const flow = document.querySelector('.flow-stages');
              const panel = document.querySelector('.flow-panel').getBoundingClientRect(), b = flow.getBoundingClientRect();
              const note = document.querySelector('[data-testid="flow-memory-note"]'), n = note.getBoundingClientRect();
              return { testid: 'flow-html-stages', left: b.left, right: b.right,
                       columns: getComputedStyle(flow).gridTemplateColumns.split(' ').length,
                       panel: [panel.left, panel.right], innerWidth,
                       texts: [...flow.querySelectorAll('h3, p')].map(x => { const t = x.getBoundingClientRect();
                         return [x.textContent, t.left, t.right, t.height]; }),
                       note: note.textContent.trim(), noteRight: n.right };""")
            page.eval("window.scrollTo(0, 0); window.__hx.blurAll(); return true;")

            # Keyboard only: Tab to the primary action; every focus ring must show.
            r["tabStops"] = []
            for _ in range(25):
                press(page, "Tab", "Tab", 9)
                time.sleep(0.1)
                stop = page.eval("""const e = document.activeElement, s = getComputedStyle(e);
                  return { text: (e.textContent || '').trim().slice(0, 40), testid: e.getAttribute('data-testid'),
                           outlineStyle: s.outlineStyle, outlineWidth: parseFloat(s.outlineWidth),
                           rect: window.__hx.rect(e), innerHeight, innerWidth };""")
                r["tabStops"].append(stop)
                if stop["testid"] == "button-start-session":
                    break
            press(page, "Enter", "Enter", 13, text="\r")
            time.sleep(1.6)
            r["afterCta"] = page.eval("""const hx = window.__hx;
              return { ...hx.state(), shell: hx.rect(hx.shell()), header: hx.rect(hx.header()),
                       innerHeight, innerWidth };""")

            # The composer works at 200%: type and send with the keyboard; the chat opens with it.
            devtools.call("Input.insertText", {"text": "Zoom check"}, session)
            press(page, "Enter", "Enter", 13, text="\r")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if page.eval("return location.pathname === '/app/' && !!window.__HANDOFF_CHILD__ && "
                                 "document.querySelectorAll('#thread .msg-assistant').length >= 1;"):
                        break
                except AssertionError:
                    pass
                time.sleep(0.1)
            time.sleep(0.5)
            r["afterSend"] = page.eval("""const c = window.__HANDOFF_CHILD__;
              return { users: [...document.querySelectorAll('#thread .msg-user .bubble')].map(b => b.textContent),
                       assistants: document.querySelectorAll('#thread .msg-assistant').length,
                       requests: c.requests.map(q => q.body.message),
                       value: document.getElementById('composer-input').value,
                       shell: (b => ({top: b.top, bottom: b.bottom, right: b.right}))(
                         document.getElementById('composer-shell').getBoundingClientRect()),
                       innerHeight, innerWidth, path: location.pathname, search: location.search,
                       docScrollWidth: document.documentElement.scrollWidth };""")

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
        assert stop["outlineStyle"] != "none" and stop["outlineWidth"] >= 2, stop
        box = stop["rect"]
        assert box["top"] >= 0 and box["bottom"] <= stop["innerHeight"] and box["right"] <= stop["innerWidth"] + 1, stop


def test_real_zoom_cta_focuses_a_visible_composer_and_stays_on_the_homepage(real_zoom):
    after = real_zoom["afterCta"]
    assert after["focused"] is True
    shell = after["shell"]
    assert shell["top"] >= after["header"]["bottom"] - 1 and shell["bottom"] <= after["innerHeight"] + 1, after
    assert shell["left"] >= 0 and shell["right"] <= after["innerWidth"] + 1
    assert after["path"] == "/" and "#" not in after["href"]


def test_real_zoom_composer_hands_off_and_the_chat_stays_usable(real_zoom):
    after = real_zoom["afterSend"]
    assert after["path"] == "/app/" and after["search"] == ""
    assert after["requests"] == ["Zoom check"] and after["users"] == ["Zoom check"]
    assert after["assistants"] == 1 and after["value"] == ""
    assert after["shell"]["bottom"] <= after["innerHeight"] + 1 and after["shell"]["right"] <= after["innerWidth"] + 1


def test_real_zoom_privacy_diagram_stays_readable(real_zoom):
    d = real_zoom["diagram"]
    assert d["testid"] == "flow-html-stages" and d["columns"] == 1                       # the vertical layout
    assert d["panel"][0] - 1 <= d["left"] and d["right"] <= d["panel"][1] + 1 <= d["innerWidth"] + 2
    for text, left, right, height in d["texts"]:
        assert d["panel"][0] - 1 <= left and right <= d["panel"][1] + 1, text
        assert height >= 11, (text, height)
    assert d["note"].startswith(MEMORY_SENTENCE)
    assert d["noteRight"] <= d["innerWidth"] + 1


# --- the /app/ workspace: header, empty state and composer (direct visit) --------------------------

APP_CHROME = r"""
for (let i = 0; i < 400 && !document.getElementById('composer-input'); i++) await new Promise(r => setTimeout(r, 25));
await new Promise(r => setTimeout(r, 300));
const q = s => document.querySelector(s);
const rect = el => { const b = el.getBoundingClientRect();
  return {left: b.left, top: b.top, right: b.right, bottom: b.bottom, width: b.width, height: b.height}; };
const header = q('.app-header'), logo = q('#brand-logo'), status = q('.session-status');
const visibleText = el => [...el.querySelectorAll('span')].filter(s => !s.children.length && !s.closest('.sr-only')
  && !s.matches('[aria-hidden="true"]') && s.getClientRects().length).map(s => s.textContent.trim()).join(' | ');
const named = el => ((el.getAttribute('aria-label') || '') + ' ' + el.textContent).toLowerCase();
return {
  innerWidth, innerHeight, docScrollWidth: document.documentElement.scrollWidth,
  headerBackground: getComputedStyle(header).backgroundColor, header: rect(header),
  logo: rect(logo), logoShown: !logo.hidden && getComputedStyle(logo).display !== 'none' && logo.naturalWidth > 0,
  logoAlt: logo.alt, wordmarkShown: getComputedStyle(q('#brand-name')).display !== 'none',
  status: visibleText(status), statusTitle: status.title, pill: !!q('.session-pill'),
  headerControls: [...header.querySelectorAll('a, button, input, select')].length,
  unsupported: document.querySelectorAll('input[type="file"], [accept], [capture]').length
    + [...document.querySelectorAll('button, a')].filter(e => /attach|upload|image|photo|voice|microphone|upgrade|\bplan\b|free plan/
        .test(named(e))).length,
  title: rect(q('.empty-title')), shell: rect(q('#composer-shell')), footer: rect(q('.app-footer')),
  cards: [...document.querySelectorAll('.hint')].map(rect),
  order: (() => { const t = q('.empty-title'), s = q('#composer-shell'), h = [...document.querySelectorAll('.hint')];
    const after = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    return after(t, s) && h.every(e => after(s, e)); })(),
  focused: document.activeElement === q('#composer-input'),
};
"""


def test_app_header_is_white_with_the_full_logo_and_a_truthful_status(app_composer):
    chrome = app_composer["chrome"]
    assert chrome["headerBackground"] == "rgb(255, 255, 255)"
    assert chrome["header"]["height"] >= 64 and chrome["header"]["top"] == 0
    assert chrome["logoShown"] is True and chrome["wordmarkShown"] is False and chrome["logoAlt"] == "Kalillac AI"
    assert chrome["logo"]["width"] == (172 if app_composer["viewport"] == "desktop" else 128)
    # No generic pill, no plan badge: one plain status that is true for every visitor now.
    assert chrome["pill"] is False and chrome["headerControls"] == 0
    # The separating dot is decorative (aria-hidden); the words are what is announced.
    assert chrome["status"] == "Temporary session | Not saved", chrome["status"]
    assert "not saved to an account or a chat history" in chrome["statusTitle"]


def test_app_empty_state_groups_heading_composer_and_examples(app_composer):
    chrome = app_composer["chrome"]
    title, shell, footer = chrome["title"], chrome["shell"], chrome["footer"]
    assert chrome["order"] is True and chrome["focused"] is True
    # Heading, composer and examples read as one group, not a heading in the
    # middle and a composer stranded at the bottom of the screen.
    assert 0 < shell["top"] - title["bottom"] <= 120, (title, shell)
    assert all(card["top"] >= shell["bottom"] for card in chrome["cards"])
    assert footer["top"] >= max(card["bottom"] for card in chrome["cards"])
    assert title["top"] - chrome["header"]["bottom"] >= 24 and chrome["innerHeight"] - footer["bottom"] >= 24
    assert chrome["docScrollWidth"] <= chrome["innerWidth"]


def test_app_exposes_no_unsupported_control(app_composer):
    assert app_composer["chrome"]["unsupported"] == 0


def test_composer_padding_shows_text_cursor_and_focuses_the_field_on_direct_app(app_composer):
    check_composer_padding_shows_text_cursor_and_focuses_the_field(app_composer)


def test_padding_press_keeps_a_draft_unchanged_on_direct_app(app_composer):
    check_padding_press_keeps_a_draft_unchanged(app_composer)


def test_clicking_the_field_keeps_native_caret_placement_on_direct_app(app_composer):
    check_clicking_the_field_keeps_native_caret_placement(app_composer)


def test_send_click_is_not_intercepted_on_direct_app(app_composer):
    check_send_click_is_not_intercepted(app_composer)


def test_composer_cursor_and_caret_are_explicit_and_visible_on_direct_app(app_composer):
    check_composer_cursor_and_caret_are_explicit_and_visible(app_composer)


def test_composer_pages_have_no_horizontal_overflow_on_direct_app(app_composer):
    check_composer_pages_have_no_horizontal_overflow(app_composer)


def test_composer_box_has_a_hover_cue_on_direct_app(app_composer):
    check_composer_box_has_a_hover_cue(app_composer)


@pytest.fixture
def app_composer(direct_app):
    assert direct_app["path"] == "/app/"
    return direct_app


def test_cta_code_uses_reduced_motion_and_prevent_scroll():
    app = APP_TSX.read_text(encoding="utf-8")
    focus = app[app.index("function focusComposer("):app.index("/* Meaningful flow content")]
    assert "behavior: prefersReducedMotion() ? 'auto' : 'smooth'," in focus
    assert "input.focus({ preventScroll: true });" in focus
    assert "location" not in focus and "navigate" not in focus
    home = app[app.index("function HomePage()"):app.index("type DocSectionProps")]
    assert "onClick={startSession}" in home
    # The composer itself holds no link: nothing in it can navigate.
    composer = app[app.index("function HomeComposer("):app.index("function focusComposer(")]
    assert "href" not in composer and "<Link" not in composer and "<a " not in composer
