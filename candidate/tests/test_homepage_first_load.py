"""Homepage first load: the native composer from the first painted frame.

The homepage no longer frames the chat. Its hero holds one real message box,
rendered with the page: there is no iframe, no hidden /app/ document, no
readiness polling and no fallback layer, so nothing can be swapped in later
and no browser refusal page can ever appear. Loading the homepage requests
neither /app/ nor /api/ -- only Send opens the chat (test_homepage_handoff.py).

These tests serve the BUILT site (site/dist/public) and the real chat
(frontend/) from loopback with the harness in test_homepage_handoff.py. A
recorder installed before any page script samples every animation frame.
Headless Microsoft Edge (or another Chromium-family browser) is driven over the
DevTools protocol; nothing leaves the machine.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

from test_homepage_handoff import (
    APP_TSX, REPO, SITE_CSS, SITE_DIST, Site, browser_page, require_browser_and_build,
)


WORKFLOW = REPO / ".github" / "workflows" / "code-structure-frontend-verification.yml"

# Installed before any page script: one sample per animation frame.
FRAME_RECORDER = r"""
(() => {
  const frames = [];
  window.__firstFrames = frames;
  const shown = el => { if (!el) return false; const b = el.getBoundingClientRect(), s = getComputedStyle(el);
    return b.width > 0 && b.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity) > 0; };
  const sample = () => {
    const root = document.getElementById('root');
    frames.push({
      t: performance.now(),
      rendered: !!(root && root.children.length),
      input: shown(document.getElementById('home-composer-input')),
      send: shown(document.querySelector('[data-testid="button-home-send"]')),
      clear: shown(document.querySelector('[data-testid="button-home-clear"]')),
      cards: [...document.querySelectorAll('[data-testid="home-card"]')].filter(shown).length,
      iframes: document.querySelectorAll('iframe, frame, object, embed').length,
    });
    if (frames.length < 180) requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
})();
"""

FIRST_SCREEN = r"""
const q = s => document.querySelector(s);
const rect = el => { const b = el.getBoundingClientRect();
  return {left: b.left, top: b.top, right: b.right, bottom: b.bottom, width: b.width, height: b.height}; };
const panel = q('[data-testid="home-composer"]'), shell = q('.hc-shell'), input = q('#home-composer-input');
return {
  innerWidth, innerHeight, scrollY,
  docScrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth,
  header: rect(q('.site-header')), panel: rect(panel), shell: rect(shell), input: rect(input),
  send: rect(q('[data-testid="button-home-send"]')), clear: rect(q('[data-testid="button-home-clear"]')),
  cards: [...document.querySelectorAll('[data-testid="home-card"]')].map(rect),
  cta: rect(q('[data-testid="button-start-session"]')),
  value: input.value, placeholder: input.placeholder, active: document.activeElement === input,
  path: location.pathname, href: location.href, historyLength: history.length,
  sessionKeys: Object.keys(sessionStorage), localKeys: Object.keys(localStorage), cookie: document.cookie,
  text: document.body.innerText,
};
"""

VIEWPORTS = {
    # name: (size, phone)
    "1440x900": ((1440, 900), False),
    "1366x768": ((1366, 768), False),
    "390x844": ((390, 844), True),
    "360x640": ((360, 640), True),
    "320x568": ((320, 568), True),
}


def evaluate(devtools, session, body: str):
    result = devtools.call("Runtime.evaluate", {"expression": f"(async () => {{ {body} }})()",
                                                "awaitPromise": True, "returnByValue": True}, session)
    assert "exceptionDetails" not in result, result["exceptionDetails"]
    return result["result"].get("value")


@pytest.fixture(scope="module", params=list(VIEWPORTS))
def first_load(request, tmp_path_factory):
    browser = require_browser_and_build()
    size, phone = VIEWPORTS[request.param]
    profile = tmp_path_factory.mktemp(f"edge-first-load-{request.param}")
    site = Site()
    site.child_passive = True

    with site.http:
        with browser_page(browser, profile, size, phone) as (devtools, session):
            devtools.call("Page.addScriptToEvaluateOnNewDocument", {"source": FRAME_RECORDER}, session)
            devtools.call("Page.navigate", {"url": site.http.origin + "/"}, session)
            deadline = time.monotonic() + 30
            while not evaluate(devtools, session, "return document.readyState === 'complete' && "
                                                  "(window.__firstFrames || []).length >= 60;"):
                assert time.monotonic() < deadline, "the homepage never finished loading"
                time.sleep(0.1)
            # Time for anything late (a deferred frame, a poll) to have happened.
            time.sleep(2.0)
            result = evaluate(devtools, session, FIRST_SCREEN)
            result["frames"] = evaluate(devtools, session, "return window.__firstFrames;")
            evaluate(devtools, session, "return 1;")
            result["requests"] = [e["params"]["request"]["url"] for e in devtools.events
                                  if e.get("method") == "Network.requestWillBeSent"]
            result["serverPaths"] = list(site.http.paths)
            result["origin"] = site.http.origin

    shutil.rmtree(profile, ignore_errors=True)
    result.update(name=request.param, size=size, phone=phone)
    return result


def test_first_painted_frame_already_holds_the_complete_native_composer(first_load):
    frames = first_load["frames"]
    rendered = [f for f in frames if f["rendered"]]
    assert rendered, "the page never rendered"
    # From the first frame with any content onward, the whole composer is there:
    # the message box, Clear draft, Send and the four examples. Nothing is
    # swapped in later and nothing is ever blank in its place.
    for frame in rendered:
        assert frame["input"] and frame["send"] and frame["clear"] and frame["cards"] == 4, frame
    # Before the first render the page is simply empty -- never a frame, a
    # refusal page or a partial workspace.
    assert all(frame["iframes"] == 0 for frame in frames)


def test_homepage_has_no_iframe_or_refusal_surface(first_load):
    assert all(frame["iframes"] == 0 for frame in first_load["frames"])
    text = first_load["text"].lower()
    for refusal in ("refused to connect", "can't be reached", "cannot be displayed", "err_", "blocked"):
        assert refusal not in text, refusal


def test_loading_the_homepage_requests_neither_the_chat_nor_the_api(first_load):
    origin = first_load["origin"]
    paths = [url[len(origin):] for url in first_load["requests"] if url.startswith(origin)]
    assert first_load["requests"][0] == origin + "/"
    assert not any(p.startswith("/app/") or p.startswith("/api/") for p in paths), paths
    assert not any(p.startswith("/app/") or p.startswith("/api/") for p in first_load["serverPaths"]), \
        first_load["serverPaths"]
    # Only this origin (and inline data) is ever contacted.
    for url in first_load["requests"]:
        assert url.startswith(origin + "/") or url.startswith("data:"), url


def test_load_stores_nothing_focuses_nothing_and_stays_on_the_homepage(first_load):
    assert first_load["sessionKeys"] == [] and first_load["localKeys"] == [] and first_load["cookie"] == ""
    # The composer waits for the visitor: empty, unfocused, at the top of the page.
    assert first_load["value"] == "" and first_load["placeholder"] == "Message Kalillac AI…"
    assert first_load["active"] is False and first_load["scrollY"] == 0
    # One history entry for the homepage after the browser's initial blank page; nothing was pushed.
    assert first_load["path"] == "/" and first_load["href"].endswith("/") and first_load["historyLength"] == 2


def test_first_screen_layout_has_no_overflow_and_a_complete_desktop_composer(first_load):
    width, height = first_load["innerWidth"], first_load["innerHeight"]
    assert (width, height) == first_load["size"]
    assert first_load["docScrollWidth"] <= width and first_load["bodyScrollWidth"] <= width
    panel, shell = first_load["panel"], first_load["shell"]
    for box in (panel, shell, first_load["send"], first_load["clear"], *first_load["cards"]):
        assert box["left"] >= 0 and box["right"] <= width + 0.5, box
    # Send and Clear draft sit inside the message box, comfortably tappable.
    for control in (first_load["send"], first_load["clear"]):
        assert shell["left"] <= control["left"] and control["right"] <= shell["right"] + 0.5
        assert control["height"] >= 36
    if first_load["phone"]:
        pytest.skip("phones reach the workspace through the CTA")
    # Side by side on the first desktop screen: the whole composer panel is in view.
    assert panel["top"] >= first_load["header"]["bottom"] and panel["bottom"] <= height, panel
    assert first_load["cta"]["right"] <= panel["left"]


APP_FRAME_RECORDER = r"""
(() => {
  const frames = [];
  window.__appFrames = frames;
  const shown = el => { if (!el) return false; const b = el.getBoundingClientRect(), s = getComputedStyle(el);
    return b.width > 0 && b.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'; };
  const sample = () => {
    frames.push({ composer: shown(document.getElementById('composer-input')),
                  logo: shown(document.getElementById('brand-logo')),
                  wordmark: shown(document.getElementById('brand-name')),
                  status: shown(document.querySelector('.session-status')),
                  iframes: document.querySelectorAll('iframe').length });
    if (frames.length < 120) requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
})();
"""


@pytest.fixture(scope="module", params=["1440x900", "390x844"])
def app_first_load(request, tmp_path_factory):
    browser = require_browser_and_build()
    size, phone = VIEWPORTS[request.param]
    profile = tmp_path_factory.mktemp(f"edge-app-first-load-{request.param}")
    site = Site()
    site.child_passive = True

    with site.http:
        with browser_page(browser, profile, size, phone) as (devtools, session):
            devtools.call("Page.addScriptToEvaluateOnNewDocument", {"source": APP_FRAME_RECORDER}, session)
            devtools.call("Page.navigate", {"url": site.http.origin + "/app/"}, session)
            deadline = time.monotonic() + 30
            while not evaluate(devtools, session, "return document.readyState === 'complete' && "
                                                  "(window.__appFrames || []).length >= 60;"):
                assert time.monotonic() < deadline, "/app/ never finished loading"
                time.sleep(0.1)
            frames = evaluate(devtools, session, "return window.__appFrames;")

    shutil.rmtree(profile, ignore_errors=True)
    return frames


def test_app_shows_its_logo_and_status_from_the_first_frame_without_a_swap(app_first_load):
    with_workspace = [f for f in app_first_load if f["composer"]]
    assert with_workspace, "the workspace never rendered"
    # The full logo's box is there from the first frame the workspace is;
    # the text wordmark (only a failed-load fallback) never shows.
    for frame in with_workspace:
        assert frame["logo"] and not frame["wordmark"] and frame["status"], frame
    assert all(frame["iframes"] == 0 for frame in app_first_load)


def test_framing_protection_is_untouched_and_ci_requires_the_exact_focused_result(tmp_path):
    app, css = APP_TSX.read_text(encoding="utf-8"), SITE_CSS.read_text(encoding="utf-8")
    for source in (app, css, (SITE_DIST.parent.parent / "index.html").read_text(encoding="utf-8")):
        for header in ("X-Frame-Options", "frame-ancestors", "http-equiv", "Content-Security-Policy"):
            assert header not in source, header
    # The site still refuses to render inside another page's frame.
    assert "function FramedNotice()" in app and "if (IS_FRAMED) return <FramedNotice />;" in app
    for gone in ("<iframe", "contentDocument", "contentWindow", "postMessage", "is-embedded", "chat-twin"):
        assert gone not in app and gone not in css, gone

    # The workflow accepts exactly the focused total = passed + permitted skips, and nothing else.
    checker = focused_result_checker()
    expected = re.search(r'expected = \{"tests": (\d+), "passed": (\d+), "skipped": (\d+), "failures": 0, '
                         r'"errors": 0\}', checker)
    total, passed, skipped = (int(n) for n in expected.groups())
    assert total == passed + skipped
    permitted = [PERMITTED_SKIPS[i % len(PERMITTED_SKIPS)] for i in range(skipped)]

    def accepted(xml: str) -> bool:
        path = tmp_path / f"result-{len(list(tmp_path.iterdir()))}.xml"
        path.write_text(xml, encoding="utf-8")
        run = subprocess.run([sys.executable, "-I", "-", str(path)], input=checker, text=True,
                             capture_output=True, timeout=60)
        return run.returncode == 0
    assert accepted(junit(passed, permitted))
    assert not accepted(junit(passed - 1, permitted + [PERMITTED_SKIPS[0]]))      # one more permitted skip
    assert not accepted(junit(passed, permitted[:-1] + ["some other reason"]))   # an unapproved reason
    assert not accepted(junit(passed - 1, permitted, failures=1))               # a failure
    assert not accepted(junit(passed - 1, permitted))                           # a missing test


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
