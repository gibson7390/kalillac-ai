"""Client-side navigation lands where the visitor expects.

In-app links (wouter <Link>) change the route without reloading the page.
Before the fix the new page kept the previous page's vertical scroll
position, so the Privacy page, for example, opened scrolled to its middle or
end. Every explicit page link must now open the new page at its top, without
smooth scrolling. The header's Product and Privacy items are page links
(/ and /privacy, desktop nav and phone menu alike) and also open at the top;
"How it works" stays a section link to /#how-it-works. A bookmarked
/#product URL still reaches the homepage section that carries that id.

Browser tests drive the BUILT site over loopback only (the same harness as
test_homepage_handoff.py) with real, trusted clicks at an exact 1440 x 900
desktop and 390 x 844 touch-phone viewport. The header's section links are
the desktop nav on desktop and the menu on phones. Nothing leaves the
machine; no chat request is made.
"""

from __future__ import annotations

import json
import shutil
import time

import pytest

from test_homepage_handoff import APP_TSX, VIEWPORTS, Site, browser_page, require_browser_and_build

SECTION_SCROLL_MARGIN = 88          # main section[id] { scroll-margin-top: 88px; }


class Page:
    def __init__(self, devtools, session):
        self.dt, self.session = devtools, session

    def eval(self, body: str):
        result = self.dt.call("Runtime.evaluate", {
            "expression": f"(async () => {{ {body} }})()",
            "awaitPromise": True, "returnByValue": True}, self.session)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")

    def try_eval(self, body: str):
        """eval that tolerates the page being mid-navigation."""
        try:
            return self.eval(body)
        except Exception:  # noqa: BLE001 - context destroyed during a document navigation
            return None

    def click(self, x: float, y: float):
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.dt.call("Input.dispatchMouseEvent", {
                "type": kind, "x": x, "y": y, "button": "left" if kind != "mouseMoved" else "none",
                "buttons": 1 if kind == "mousePressed" else 0, "clickCount": 1}, self.session)

    def wait_for(self, condition_js: str, timeout: float = 15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.try_eval(f"return !!({condition_js});"):
                return True
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for: {condition_js}")

    def settle(self, quiet: float = 0.45, timeout: float = 8):
        """Wait until window.scrollY has not changed for `quiet` seconds."""
        deadline, last, since = time.monotonic() + timeout, None, time.monotonic()
        while time.monotonic() < deadline:
            y = self.try_eval("return Math.round(window.scrollY);")
            if y != last:
                last, since = y, time.monotonic()
            elif time.monotonic() - since >= quiet:
                return y
            time.sleep(0.05)
        return last


READY = {
    "/": "document.querySelector('[data-testid=\"section-hero\"]')",
    "/privacy": "document.querySelector('h1.doc-title') && document.querySelector('h1.doc-title').textContent === 'Privacy'",
    "/terms": "document.querySelector('h1.doc-title') && document.querySelector('h1.doc-title').textContent.startsWith('Terms')",
}

# name, start path, how far to scroll first, link to click (desktop, mobile), expected path + hash, expectation
SCENARIOS = [
    ("footer-privacy-from-home", "/", "bottom", ('[data-testid="link-footer-privacy"]',) * 2, "/privacy", "top"),
    ("full-privacy-link-from-home", "/", "link", ('[data-testid="link-full-privacy"]',) * 2, "/privacy", "top"),
    ("footer-privacy-from-terms", "/terms", "bottom", ('[data-testid="link-footer-privacy"]',) * 2, "/privacy", "top"),
    ("terms-inline-privacy-link", "/terms", "link", ('#privacy a[href="/privacy"]',) * 2, "/privacy", "top"),
    ("footer-terms-from-home", "/", "bottom", ('[data-testid="link-footer-terms"]',) * 2, "/terms", "top"),
    ("footer-terms-from-privacy", "/privacy", "bottom", ('[data-testid="link-footer-terms"]',) * 2, "/terms", "top"),
    ("header-logo-from-privacy", "/privacy", 2400, ('[data-testid="link-home-brand"]',) * 2, "/", "top"),
    ("footer-logo-from-terms", "/terms", "bottom", ('[data-testid="link-footer-home"]',) * 2, "/", "top"),
    ("doc-home-link-from-privacy", "/privacy", "link", ('[data-testid="link-doc-home"]',) * 2, "/", "top"),
    ("header-logo-on-home", "/", 2400, ('[data-testid="link-home-brand"]',) * 2, "/", "top"),
    ("product-from-scrolled-privacy", "/privacy", 2400,
     ('[data-testid="link-product-nav"]', '[data-testid="link-mobile-product"]'), "/", "top"),
    ("product-from-scrolled-home", "/", 2400,
     ('[data-testid="link-product-nav"]', '[data-testid="link-mobile-product"]'), "/", "top"),
    ("header-privacy-from-scrolled-home", "/", "bottom",
     ('[data-testid="link-privacy-nav"]', '[data-testid="link-mobile-privacy"]'), "/privacy", "top"),
    ("header-privacy-from-scrolled-terms", "/terms", "bottom",
     ('[data-testid="link-privacy-nav"]', '[data-testid="link-mobile-privacy"]'), "/privacy", "top"),
    ("how-it-works-from-scrolled-privacy", "/privacy", 2400,
     ('[data-testid="link-how-nav"]', '[data-testid="link-mobile-how"]'), "/#how-it-works", "section"),
    ("how-it-works-on-home", "/", 0,
     ('[data-testid="link-how-nav"]', '[data-testid="link-mobile-how"]'), "/#how-it-works", "section"),
]


def run_scenario(page: Page, origin: str, scenario, mobile: bool) -> dict:
    name, start, scroll, links, expected, expectation = scenario
    selector = links[1] if mobile else links[0]
    page.dt.call("Page.navigate", {"url": origin + start}, page.session)
    page.wait_for(READY[start])
    page.wait_for("document.readyState === 'complete'")
    time.sleep(0.4)

    # Put the page where a visitor would be before clicking (instantly).
    if scroll == "bottom":
        js = "document.documentElement.scrollHeight"
    elif scroll == "link":
        js = (f"(() => {{ const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect(); "
              f"return window.scrollY + r.top - innerHeight / 2; }})()")
    else:
        js = str(scroll)
    page.eval(f"document.documentElement.style.scrollBehavior = 'auto'; window.scrollTo(0, {js}); "
              "document.documentElement.style.scrollBehavior = ''; return true;")
    before = page.settle()

    if mobile and selector.startswith('[data-testid="link-mobile-'):
        menu = page.eval("const b = document.querySelector('[data-testid=\"button-mobile-menu\"]').getBoundingClientRect();"
                         "return {x: b.left + b.width / 2, y: b.top + b.height / 2};")
        page.click(menu["x"], menu["y"])
        page.wait_for(f"document.querySelector({json.dumps(selector)})")
        time.sleep(0.15)

    box = page.eval(f"const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();"
                    "return {x: r.left + r.width / 2, y: r.top + r.height / 2, visible: r.bottom > 0 && r.top < innerHeight,"
                    " width: r.width};")
    assert box["visible"] and box["width"] > 0, (name, box)
    page.click(box["x"], box["y"])

    # Immediately after an in-app route change: sample scrollY to prove there is no smooth animation.
    samples = page.try_eval("const out = []; for (let i = 0; i < 12; i++) { out.push(Math.round(window.scrollY));"
                            " await new Promise(r => setTimeout(r, 25)); } return out;") or []

    path, _, fragment = expected.partition("#")
    page.wait_for(f"location.pathname === {json.dumps(path)} && location.hash === {json.dumps('#' + fragment if fragment else '')}")
    page.wait_for(READY[path])
    final = page.settle()
    facts = page.eval(f"""
      const h = document.querySelector('.site-header').getBoundingClientRect();
      const s = {json.dumps(fragment)} ? document.getElementById({json.dumps(fragment)}) : null;
      return {{ path: location.pathname, hash: location.hash, scrollY: Math.round(window.scrollY),
                headerBottom: Math.round(h.bottom),
                sectionTop: s ? Math.round(s.getBoundingClientRect().top) : null,
                title: (document.querySelector('h1') || {{}}).textContent || null,
                historyLength: history.length,
                menuOpen: !!document.getElementById('mobile-site-menu'),
                inlineScrollBehavior: document.documentElement.style.scrollBehavior }};""")
    return {"name": name, "before": before, "samples": samples, "final": final, "expectation": expectation,
            "expected": expected, "usedMenu": mobile and selector.startswith('[data-testid="link-mobile-'), **facts}


@pytest.fixture(scope="module", params=["desktop", "mobile"])
def navigation(request, tmp_path_factory):
    browser = require_browser_and_build()
    viewport = request.param
    profile = tmp_path_factory.mktemp(f"edge-route-navigation-{viewport}")
    site = Site()
    results = {}

    with site.http:
        with browser_page(browser, profile, VIEWPORTS[viewport], viewport == "mobile") as (devtools, session):
            page = Page(devtools, session)
            for scenario in SCENARIOS:
                results[scenario[0]] = run_scenario(page, site.http.origin, scenario, viewport == "mobile")

            # Back/Forward: an explicit link resets to the top, but browser history
            # traversal is left to the browser (the handler is not involved).
            page.dt.call("Page.navigate", {"url": site.http.origin + "/"}, session)
            page.wait_for(READY["/"])
            time.sleep(0.4)
            page.eval("document.documentElement.style.scrollBehavior = 'auto';"
                      "window.scrollTo(0, document.documentElement.scrollHeight);"
                      "document.documentElement.style.scrollBehavior = ''; return true;")
            home_y = page.settle()
            box = page.eval("const r = document.querySelector('[data-testid=\"link-footer-privacy\"]').getBoundingClientRect();"
                            "return {x: r.left + r.width / 2, y: r.top + r.height / 2};")
            page.click(box["x"], box["y"])
            page.wait_for(READY["/privacy"])
            privacy_y = page.settle()
            page.eval("history.back(); return true;")
            page.wait_for("location.pathname === '/'")
            page.wait_for(READY["/"])
            back_y = page.settle()
            results["back"] = {"homeY": home_y, "privacyY": privacy_y, "backY": back_y,
                               "path": page.eval("return location.pathname;")}
            # A bookmarked legacy /#product URL still reaches valid content.
            page.dt.call("Page.navigate", {"url": site.http.origin + "/#product"}, session)
            page.wait_for(READY["/"])
            page.wait_for("document.readyState === 'complete'")
            page.settle()
            results["legacyProduct"] = page.eval("""const s = document.getElementById('product');
              return { path: location.pathname, hash: location.hash, scrollY: Math.round(window.scrollY),
                       sectionTop: s ? Math.round(s.getBoundingClientRect().top) : null,
                       heading: s ? s.querySelector('h2').textContent.trim() : null };""")
            results["apiRequests"] = [p for p in site.http.paths if "/api/" in p]

    shutil.rmtree(profile, ignore_errors=True)
    results["viewport"] = viewport
    return results


@pytest.mark.parametrize("name", [s[0] for s in SCENARIOS if s[5] == "top"])
def test_explicit_page_links_open_the_new_page_at_its_top(navigation, name):
    run = navigation[name]

    assert run["path"] + run["hash"] == run["expected"], run
    assert run["before"] > 0 or name == "doc-home-link-from-privacy", run    # really started scrolled
    assert run["final"] == 0 and run["scrollY"] == 0, run
    # Immediate, not animated: no intermediate scroll positions after the click.
    assert run["samples"] and set(run["samples"]) == {0}, run["samples"]
    assert run["inlineScrollBehavior"] == ""                                  # override was restored
    assert run["menuOpen"] is False                                           # a phone menu closes on use


@pytest.mark.parametrize("name", [s[0] for s in SCENARIOS if s[5] == "section"])
def test_section_links_still_land_on_their_section(navigation, name):
    run = navigation[name]

    assert run["path"] + run["hash"] == run["expected"], run
    assert run["sectionTop"] is not None
    # The section starts just below the sticky header (its scroll margin), not at the page top.
    assert abs(run["sectionTop"] - SECTION_SCROLL_MARGIN) <= 2, run
    assert run["scrollY"] > 0


def test_privacy_entry_points_cover_header_footer_and_homepage_link(navigation):
    # The header (desktop nav or phone menu), the footer, the homepage "Read the
    # full Privacy page" link and the Terms page's inline link all open /privacy at the top.
    for name in ("header-privacy-from-scrolled-home", "header-privacy-from-scrolled-terms",
                 "footer-privacy-from-home", "full-privacy-link-from-home", "footer-privacy-from-terms",
                 "terms-inline-privacy-link"):
        assert navigation[name]["path"] == "/privacy" and navigation[name]["scrollY"] == 0, name
        assert navigation[name]["title"] == "Privacy", name


def test_product_opens_the_homepage_at_its_top_on_desktop_and_phone(navigation):
    for name in ("product-from-scrolled-privacy", "product-from-scrolled-home"):
        run = navigation[name]
        assert run["path"] == "/" and run["hash"] == "" and run["scrollY"] == 0, run
        assert run["usedMenu"] is (navigation["viewport"] == "mobile")       # phones use the menu
        assert run["menuOpen"] is False


def test_header_privacy_uses_the_menu_on_phones_and_closes_it(navigation):
    for name in ("header-privacy-from-scrolled-home", "header-privacy-from-scrolled-terms"):
        run = navigation[name]
        assert run["usedMenu"] is (navigation["viewport"] == "mobile") and run["menuOpen"] is False, run


def test_legacy_product_bookmark_reaches_the_memory_section(navigation):
    legacy = navigation["legacyProduct"]
    assert legacy["path"] == "/" and legacy["hash"] == "#product"
    assert legacy["heading"] == "Temporary memory you can test yourself."
    assert legacy["scrollY"] > 0 and abs(legacy["sectionTop"] - SECTION_SCROLL_MARGIN) <= 2, legacy


def test_back_returns_to_the_previous_page_without_forcing_the_top(navigation):
    back = navigation["back"]
    assert back["path"] == "/"
    assert back["homeY"] > 0 and back["privacyY"] == 0
    # History traversal is the browser's: the route handler does not run on
    # popstate, so nothing forces the page back to the top.
    assert back["backY"] > 0, back


def test_no_chat_request_during_navigation(navigation):
    assert navigation["apiRequests"] == []


# --- static contract (always runs) -----------------------------------------------------------------


def test_scroll_reset_is_centralized_in_the_router():
    app = APP_TSX.read_text(encoding="utf-8")

    assert "<WouterRouter aroundNav={startNavigationAtTop}>" in app
    assert app.count("aroundNav=") == 1
    handler = app[app.index("const startNavigationAtTop"):app.index("function App()")]
    assert "navigate(to, options);" in handler
    assert "window.scrollTo(0, 0)" in handler and "scrollIntoView()" in handler
    # Immediate, without the newer 'instant' value older browsers reject.
    jump = app[app.index("const jumpTo"):app.index("const startNavigationAtTop")]
    assert "root.style.scrollBehavior = 'auto';" in jump and "root.style.scrollBehavior = previous;" in jump
    # The override must be applied (style recalculated) before scrolling, or the
    # browser still animates with the stale smooth value.
    assert jump.index("void getComputedStyle(root).scrollBehavior;") < jump.index("scroll();")
    assert "'instant'" not in handler + jump and "'smooth'" not in handler + jump
    assert "'instant'" not in app.replace("newer 'instant'", "")
    # No scattered resets: the only window.scrollTo calls are the workspace CTA and this handler.
    assert app.count("window.scrollTo(") == 3, app.count("window.scrollTo(")
    assert "popstate" not in app and "scrollRestoration" not in app
    # Product and Privacy are in-app page links, so this handler governs them.
    header = app[app.index("function SiteHeader()"):app.index("function SiteFooter()")]
    assert header.count('<Link href="/" ') == 3                 # logo, Product, mobile Product
    assert header.count('<Link href="/privacy" ') == 2          # Privacy, mobile Privacy
    assert 'href="/#product"' not in header and 'href="/#privacy"' not in header
    assert header.count('href="/#how-it-works"') == 2           # the one header section link
