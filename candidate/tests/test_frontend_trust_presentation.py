"""Chat trust presentation: the accuracy notice and the source list.

Browser tests run the real frontend/index.html and frontend/app.js in headless
Microsoft Edge (or another installed Chromium-family browser), served from a
loopback-only HTTP server, with frontend_harness/trust_presentation.js
injected before app.js to script fetch and drive the real DOM. Every reply is
built here with the backend's own source_trust functions (the same
enforce_answer_links + render_source_footer the chat routes return), so the
page renders exactly what the server sends; a few hand-written replies stand
for shapes the server never produces. No request leaves the machine: fetch is
replaced in the page, every non-loopback connection goes to a dead proxy and
no other host name resolves. No AI or search provider is called.
"""

from __future__ import annotations

import html
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from urllib.parse import quote, urlsplit

import pytest

from kalillac_routing import source_trust as st


REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
APP_JS = FRONTEND / "app.js"
APP_CSS = FRONTEND / "app.css"
INDEX_HTML = FRONTEND / "index.html"
SITE_APP = REPO / "site" / "src" / "App.tsx"
HARNESS_JS = Path(__file__).resolve().parent / "frontend_harness" / "trust_presentation.js"
EDGE_WINDOWS = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
# The release this presentation slice started from.
BASE_COMMIT = "2235d8dc3ec1e8bd15cbb16344adc0784299cedf"

NOTICE = "Kalillac can make mistakes. Verify important information."
PAGE_FAILURE_STDERR = ("Page load failed", "Uncaught", "net::ERR_")


# --- fixtures: replies exactly as the server builds them -------------------------------------------

FED = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261105a.htm"
REUTERS = "https://www.reuters.com/markets/us/fed-holds-rates-steady-2026-11-05/"
BLOG = "https://example.org/blog/what-the-rate-decision-means"
LONG_HOST = ("https://very-long-subdomain-segment-for-layout-testing.department."
             "example-university.ac.uk/research/papers/2026/an-extremely-long-path-segment-name")
PAREN = "https://en.wikipedia.org/wiki/Mercury_(planet)"
QUERY = "https://search.example.com/results?q=interest+rates&page=2&sort=recent#section-4"
IDN = "https://b\u00fccher.example/katalog"
EVIL = "https://evil.example/steal"


def result(title, url, content="Result text."):
    return {"title": title, "url": url, "content": content}


def server_reply(body, results, ids):
    """What a search route returns: the guarded answer plus the footer.
    ids: validated ids (a tuple; () means "none") or None (missing/invalid)."""
    selection = st.SourceSelection(None if ids is None else tuple(ids))
    return st.enforce_answer_links(body, results, selection) + st.render_source_footer(results, selection)


SERVER = {
    "sources_selected": (
        "The Federal Reserve held its policy rate steady "
        f"([Federal Reserve]({FED})). Reuters also reported the decision at {REUTERS}.\n\n"
        "Markets had expected the hold.",
        [result("Federal Reserve issues FOMC statement", FED),
         result("Fed holds rates steady as inflation cools", REUTERS),
         result("What the rate decision means for savers", BLOG)],
        (1, 3),
    ),
    "search_results": (
        f"I could not confirm the decision from these results. One report is [here]({REUTERS}).",
        [result("Federal Reserve issues FOMC statement", FED),
         result("Fed holds rates steady as inflation cools", REUTERS)],
        (),
    ),
    "search_results_invalid_selection": (
        "Here is what the search found.",
        [result("Federal Reserve issues FOMC statement", FED),
         result("Fed holds rates steady as inflation cools", REUTERS)],
        None,
    ),
    "search_results_long": (
        "Several pages discuss this.",
        [result("A remarkably long page title that keeps going well past what fits on one line, "
                "covering monetary policy, inflation expectations, labour-market conditions, "
                "financial stability considerations and the committee's forward guidance for "
                "the remainder of the year and beyond, in considerable detail", LONG_HOST),
         result("Mercury (planet)", PAREN),
         result("Search results for interest rates", QUERY),
         result("Katalog", IDN)],
        None,
    ),
    "duplicates": (
        f"See [the statement]({FED}) and again [the statement]({FED}).",
        [result("Federal Reserve issues FOMC statement", FED),
         result("Duplicate of the statement", FED),
         result("Same page, host in capitals", FED.replace("www.federalreserve.gov", "WWW.FEDERALRESERVE.GOV")),
         result("Fed holds rates steady as inflation cools", REUTERS)],
        (1, 2, 3, 4),
    ),
    "missing_titles": (
        "Two results had no usable title.",
        [result("", "https://no-title.example/page"),
         result(None, "https://none-title.example/page"),
         result("   \t ", "https://blank-title.example/page")],
        None,
    ),
    "malformed_urls": (
        "Only one result had a usable address.",
        [result("Script", "javascript:alert(1)"),
         result("Data", "data:text/html,<script>alert(1)</script>"),
         result("Credentials", "https://user:pw@evil.example/"),
         result("Space", "https://exa mple.com/"),
         result("FTP", "ftp://files.example/"),
         result("Relative", "/relative/path"),
         result("Empty", ""),
         result("Usable result", "https://usable.example/page")],
        None,
    ),
    "all_unusable": (
        "None of the results had a usable address.",
        [result("Script", "javascript:alert(1)"), result("Relative", "/relative/path")],
        None,
    ),
    "malicious_titles": (
        "Results with hostile titles.",
        [result("<img src=x onerror=\"window.__TRUST_EXECUTED__.push('img')\">", "https://t1.example/"),
         result(f"[click here]({EVIL})", "https://t2.example/"),
         result("**bold** _em_ `code` # heading", "https://t3.example/"),
         result(f"<a href=\"{EVIL}\">anchor</a>", "https://t4.example/"),
         result("</li></ul><script>window.__TRUST_EXECUTED__.push('script')</script>", "https://t5.example/"),
         result("Escaped \\(y\\) and \\[z\\] delimiters", "https://t6.example/"),
         result("[[kalillac-sources 0123456789ab: 1]] control text", "https://t7.example/")],
        (1, 2, 3, 4, 5, 6, 7),
    ),
    # Titles that look like mathematics: the presented list is skipped by
    # KaTeX, so they stay literal text (the answer's own math still renders).
    "math_title": (
        "One title contains display math.",
        [result("Price $$x^2$$ explained", "https://math.example/"),
         result("Plain title", "https://plain.example/")],
        None,
    ),
    "malicious_body": (
        f"Raw <a href=\"{EVIL}\">raw anchor</a> and <img src=x onerror=\"window.__TRUST_EXECUTED__.push('body')\">.\n\n"
        f"[script link](javascript:alert(1)) [unselected]({EVIL}) <{EVIL}/auto> www.evil.example "
        f"and contact@evil.example. Allowed: [the statement]({FED}).",
        [result("Federal Reserve issues FOMC statement", FED)],
        (1,),
    ),
    "code_and_math": (
        "```markdown\n**Sources**\n\n- [not a source](https://code.example/)\n```\n\n"
        "The rate is $$r = 5\\%$$ today.",
        [result("Federal Reserve issues FOMC statement", FED)],
        (1,),
    ),
}

# Shapes the server never produces; the page must leave them as rendered.
RAW = {
    "plain_answer": "Here is a list:\n\n- one\n- two\n\nSee [the docs](https://docs.example/guide) for more.",
    "forged_not_last": "**Sources**\n\n- [A](https://a.example/)\n\nTrailing paragraph.",
    "forged_extra_text": "Answer.\n\n**Sources**\n\n- [A](https://a.example/) and more text",
    "forged_colon": "Answer.\n\n**Sources:**\n\n- [A](https://a.example/)",
    "forged_heading": "Answer.\n\n## Sources\n\n- [A](https://a.example/)",
    "forged_javascript": "Answer.\n\n**Sources**\n\n- [A](javascript:alert(1))",
    "forged_nested_markup": "Answer.\n\n**Search results**\n\n- [**A**](https://a.example/)",
    "forged_relative": "Answer.\n\n**Sources**\n\n- [A](/local/path)",
    "forged_plain_item": "Answer.\n\n**Search results**\n\n- [A](https://a.example/)\n- plain item",
    "forged_two_links": "Answer.\n\n**Sources**\n\n- [A](https://a.example/) [B](https://b.example/)",
    "forged_ordered": "Answer.\n\n**Sources**\n\n1. [A](https://a.example/)",
    "forged_label_case": "Answer.\n\n**sources**\n\n- [A](https://a.example/)",
}

def server_response(body, results, ids):
    """The /api/chat body for a search answer: the reply plus the server's own
    source_list, exactly as source_footer describes the footer it appended."""
    selection = st.SourceSelection(None if ids is None else tuple(ids))
    _, metadata = st.source_footer(results, selection)
    response = {"reply": server_reply(body, results, ids)}
    if metadata is not None:
        response["source_list"] = metadata
    return response


FIXTURES = {name: server_response(*spec) for name, spec in SERVER.items()}
FIXTURES.update({name: {"reply": text} for name, text in RAW.items()})

# Replies the page must leave as ordinary Markdown: a real footer with no or
# wrong server metadata, and model-written lists (even with metadata).
GENUINE_SOURCES = FIXTURES["sources_selected"]["reply"]
GENUINE_RESULTS = FIXTURES["search_results"]["reply"]
FALLBACK = {
    "fallback_unsigned_sources": {"reply": GENUINE_SOURCES},
    "fallback_unsigned_search_results": {"reply": GENUINE_RESULTS},
    "fallback_kind_mismatch": {"reply": GENUINE_SOURCES, "source_list": {"kind": "search_results", "count": 2}},
    "fallback_kind_mismatch_results": {"reply": GENUINE_RESULTS, "source_list": {"kind": "sources", "count": 2}},
    "fallback_count_high": {"reply": GENUINE_SOURCES, "source_list": {"kind": "sources", "count": 3}},
    "fallback_count_low": {"reply": GENUINE_SOURCES, "source_list": {"kind": "sources", "count": 1}},
    "fallback_count_zero": {"reply": GENUINE_SOURCES, "source_list": {"kind": "sources", "count": 0}},
    "fallback_count_string": {"reply": GENUINE_SOURCES, "source_list": {"kind": "sources", "count": "2"}},
    "fallback_count_fraction": {"reply": GENUINE_SOURCES, "source_list": {"kind": "sources", "count": 2.5}},
    "fallback_count_negative": {"reply": GENUINE_SOURCES, "source_list": {"kind": "sources", "count": -2}},
    "fallback_count_huge": {"reply": GENUINE_SOURCES, "source_list": {"kind": "sources", "count": 101}},
    "fallback_count_missing": {"reply": GENUINE_SOURCES, "source_list": {"kind": "sources"}},
    "fallback_kind_label": {"reply": GENUINE_SOURCES, "source_list": {"kind": "Sources", "count": 2}},
    "fallback_kind_null": {"reply": GENUINE_SOURCES, "source_list": {"kind": None, "count": 2}},
    "fallback_list_array": {"reply": GENUINE_SOURCES, "source_list": ["sources", 2]},
    "fallback_list_string": {"reply": GENUINE_SOURCES, "source_list": "sources"},
    "fallback_list_null": {"reply": GENUINE_SOURCES, "source_list": None},
    "fallback_model_sources": {"reply": "Answer.\n\n**Sources**\n\n- [A](https://a.example/)\n- [B](https://b.example/)"},
    "fallback_model_search_results": {"reply": "Answer.\n\n**Search results**\n\n- [A](https://a.example/)"},
    "fallback_forged_shape_with_metadata": {"reply": RAW["forged_extra_text"],
                                            "source_list": {"kind": "sources", "count": 1}},
}
FIXTURES.update(FALLBACK)


def rendered_href(url):
    """The href marked writes for a link destination: non-ASCII characters
    percent-encoded as UTF-8 (pre-existing pipeline behaviour, unchanged here)."""
    return "".join(c if ord(c) < 128 else quote(c) for c in url)


def expected_host(url):
    host = urlsplit(url).hostname or ""
    host = host.encode("idna").decode("ascii")
    return host[4:] if host.startswith("www.") else host


def expected_entries(name):
    body, results, ids = SERVER[name]
    selection = st.SourceSelection(None if ids is None else tuple(ids))
    chosen, label = st.displayed_sources(results, selection)
    return label, [
        {"href": rendered_href(item["url"]), "title": html.unescape(st._footer_title(item)),
         "host": expected_host(item["url"])}
        for item in chosen
    ]


VIEWPORTS = ("1280x800", "1024x768", "768x1024", "640x400", "390x844", "360x640", "320x568",
             "640x360", "360x300")
DAILY_LIMIT_VIEWPORTS = ("1280x800", "360x640", "320x568")
SCENARIOS = (
    [f"layout_{v}" for v in VIEWPORTS]
    + [f"daily_limit_{v}" for v in DAILY_LIMIT_VIEWPORTS]
    + ["preservation", "retry_with_metadata"]
    + [f"fixture_{name}" for name in FIXTURES]
    + [f"fixture_{name}_360x640" for name in FIXTURES]
)


# --- loopback server and browser ----------------------------------------------------------------


def find_browser() -> Path | None:
    if os.name == "nt":
        return EDGE_WINDOWS if EDGE_WINDOWS.is_file() else None
    for name in ("microsoft-edge", "microsoft-edge-stable", "chromium", "chromium-browser",
                 "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


class TrustServer:
    """Loopback-only server for the real frontend plus the harness routes.
    Every requested path is recorded; anything unexpected is a failure."""

    ALLOWED = re.compile(
        r"^(/app/(\?scenario=[a-z0-9_]+(&fixture=[a-z0-9_]+)?)?|/app/[A-Za-z0-9_./-]+(\?v=[A-Za-z0-9._-]+)?"
        r"|/__trust__/trust_presentation\.js|/__trust__/fixtures\.json"
        r"|/__trust__/runner\?scenarios=[a-z0-9_,]+"
        r"|/favicon\.ico|/assets/kalillac-ai-logo\.png\?v=[A-Za-z0-9-]+)$"
    )

    def __init__(self, fixtures):
        self.paths: list[str] = []
        self.unexpected: list[str] = []
        self.fixtures = json.dumps(fixtures).encode("utf-8")
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.paths.append(self.path)
                if not TrustServer.ALLOWED.match(self.path):
                    server.unexpected.append(self.path)
                status, ctype, body = server.route(self.path)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                server.paths.append("POST " + self.path)
                server.unexpected.append("POST " + self.path)
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def route(self, raw_path: str):
        path = raw_path.split("?", 1)[0]
        if path == "/app/":
            page = INDEX_HTML.read_text(encoding="utf-8")
            marker = "<!-- Local vendored libraries only"
            assert page.count(marker) == 1
            page = page.replace(
                marker, '<script src="/__trust__/trust_presentation.js"></script>\n  ' + marker, 1)
            return 200, "text/html; charset=utf-8", page.encode("utf-8")
        if path == "/__trust__/trust_presentation.js":
            return 200, "text/javascript", HARNESS_JS.read_bytes()
        if path == "/__trust__/fixtures.json":
            return 200, "application/json", self.fixtures
        if path == "/__trust__/runner":
            page = ('<!DOCTYPE html><html><body style="margin:0"><pre id="results">PENDING</pre>'
                    '<script src="/__trust__/trust_presentation.js"></script></body></html>')
            return 200, "text/html; charset=utf-8", page.encode("utf-8")
        if path.startswith("/app/"):
            target = (FRONTEND / path[len("/app/"):]).resolve()
            if FRONTEND.resolve() in target.parents and target.is_file():
                ctype = {".js": "text/javascript", ".css": "text/css", ".html": "text/html"}.get(
                    target.suffix, "application/octet-stream")
                return 200, ctype, target.read_bytes()
        return 404, "text/plain", b""

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


def browser_args(browser: Path, profile: Path, *extra: str) -> list[str]:
    args = [
        str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--disable-extensions", "--disable-sync",
        "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
        f"--user-data-dir={profile}",
        # Every non-loopback connection goes to a dead proxy; no other host resolves.
        "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=127.0.0.1",
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
        *extra,
    ]
    if os.name != "nt":
        args.insert(1, "--no-sandbox")
    return args


@pytest.fixture(scope="module")
def browser_results(tmp_path_factory):
    browser = find_browser()
    if browser is None:
        if os.name == "nt":
            pytest.fail(f"Microsoft Edge is required on Windows and was not found at {EDGE_WINDOWS}")
        pytest.skip("no supported Chromium-family browser is installed")

    profile = tmp_path_factory.mktemp("edge-profile")
    with TrustServer(FIXTURES) as server:
        url = f"{server.origin}/__trust__/runner?scenarios={','.join(SCENARIOS)}"
        result = subprocess.run(
            browser_args(browser, profile, "--window-size=1400,1000", "--virtual-time-budget=600000",
                         "--dump-dom", url),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        paths, unexpected = list(server.paths), list(server.unexpected)
    shutil.rmtree(profile, ignore_errors=True)

    diagnostics = (f"browser={browser}\nexit={result.returncode}\nstdout={result.stdout[-3000:]}\n"
                   f"stderr={result.stderr[-3000:]}\npaths={paths[:40]}")
    match = re.search(r'<pre id="results">(.*?)</pre>', result.stdout, re.S)
    assert result.returncode == 0 and match, diagnostics
    return {
        "results": json.loads(html.unescape(match.group(1))),
        "paths": paths,
        "unexpected": unexpected,
        "stderr": result.stderr,
        "diagnostics": diagnostics,
    }


def scenario(browser_results, name):
    result = browser_results["results"][name]
    assert result.get("error") is None, (name, result.get("error"), browser_results["diagnostics"])
    return result


def step(result, label):
    return next(s for s in result["steps"] if s["label"] == label)


def rendered(browser_results, name):
    """The single assistant message of a fixture scenario."""
    snap = step(scenario(browser_results, name), "rendered")
    assert len(snap["messages"]) == 1
    return snap, snap["messages"][0]


def rgb(value: str) -> tuple[float, float, float]:
    numbers = [float(n) for n in re.findall(r"[\d.]+", value)[:3]]
    return numbers[0], numbers[1], numbers[2]


def contrast(foreground: str, background: str) -> float:
    def luminance(color):
        channels = []
        for c in color:
            c = c / 255
            channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
    a, b = luminance(rgb(foreground)), luminance(rgb(background))
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def clickable(message):
    """Anchors that are links: an <a> whose href was removed is plain text."""
    return [a for a in message["anchors"] if a["href"] is not None]


def overlaps(a, b, tolerance=0.5):
    return (a["left"] < b["right"] - tolerance and b["left"] < a["right"] - tolerance
            and a["top"] < b["bottom"] - tolerance and b["top"] < a["bottom"] - tolerance)


ALL_FIXTURE_SCENARIOS = [f"fixture_{n}" for n in FIXTURES] + [f"fixture_{n}_360x640" for n in FIXTURES]


# --- the run itself ------------------------------------------------------------------------------


def test_every_scenario_completed(browser_results):
    for name in SCENARIOS:
        scenario(browser_results, name)


def test_browser_served_only_approved_loopback_paths(browser_results):
    assert browser_results["unexpected"] == []
    assert any(p.startswith("/app/app.js?v=") for p in browser_results["paths"])


def test_browser_reports_no_page_failure(browser_results):
    for marker in PAGE_FAILURE_STDERR:
        assert marker not in browser_results["stderr"], browser_results["diagnostics"]


def test_only_the_chat_endpoint_was_requested(browser_results):
    for name in SCENARIOS:
        for request in scenario(browser_results, name)["requests"]:
            assert request["url"] == "/api/chat" and request["method"] == "POST", (name, request)


# --- 1. the accuracy notice ----------------------------------------------------------------------


def test_notice_markup_is_fixed_text_directly_beneath_the_composer():
    page = INDEX_HTML.read_text(encoding="utf-8")
    assert page.count(NOTICE) == 1
    notice = f'<p class="accuracy-notice" id="accuracy-notice">{NOTICE}</p>'
    assert notice in page
    assert page.index("</form>") < page.index(notice) < page.index('id="starter-dock"')
    textarea = page[page.index("<textarea"):page.index("</textarea>")]
    assert 'aria-describedby="accuracy-notice"' in textarea
    # Fixed copy: nothing in app.js writes or hides it.
    assert "accuracy-notice" not in APP_JS.read_text(encoding="utf-8")


def assert_notice_visible(notice, where):
    assert notice["exists"] and notice["text"] == NOTICE, where
    assert notice["tag"] == "P" and not notice["focusable"] and not notice["ariaHidden"], where
    assert notice["describedBy"] == "accuracy-notice" and notice["afterComposer"], where
    style = notice["style"]
    assert style["display"] != "none" and style["visibility"] == "visible" and float(style["opacity"]) == 1, where
    assert float(style["font-size"].rstrip("px")) >= 11, where
    assert contrast(style["color"], notice["background"]) >= 4.5, where
    box, viewport = notice["rect"], notice["viewport"]
    assert box["height"] > 0 and box["width"] > 0, where
    # Fully inside the viewport, beneath the composer, clear of every control.
    assert box["top"] >= 0 and box["bottom"] <= viewport["height"] + 0.5, (where, box, viewport)
    assert box["left"] >= -0.5 and box["right"] <= viewport["width"] + 0.5, (where, box, viewport)
    assert box["top"] >= notice["composer"]["bottom"] - 0.5, (where, box, notice["composer"])
    for control in ("composer", "input", "send", "erase"):
        assert not overlaps(box, notice[control]), (where, control, box, notice[control])
    if notice["limitNotice"]:
        assert not overlaps(box, notice["limitNotice"]), where
    # One line wherever it fits; never wider than the page.
    assert notice["scrollWidth"] <= notice["clientWidth"], where


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_notice_is_visible_and_clear_of_the_composer(browser_results, viewport):
    result = scenario(browser_results, f"layout_{viewport}")
    for label in ("empty", "conversation"):
        assert_notice_visible(step(result, label)["notice"], (viewport, label))


@pytest.mark.parametrize("viewport", ("1280x800", "1024x768", "768x1024", "640x400"))
def test_notice_stays_on_one_line_on_wider_screens(browser_results, viewport):
    notice = step(scenario(browser_results, f"layout_{viewport}"), "conversation")["notice"]
    line = float(notice["style"]["line-height"].rstrip("px"))
    assert notice["rect"]["height"] <= line * 1.5


@pytest.mark.parametrize("viewport", DAILY_LIMIT_VIEWPORTS)
def test_daily_limit_note_and_notice_never_obstruct_each_other(browser_results, viewport):
    notice = step(scenario(browser_results, f"daily_limit_{viewport}"), "paused")["notice"]
    assert notice["limitNotice"], "the daily-limit note is shown"
    assert "You\u2019ve used today\u2019s 10 free responses." in notice["limitText"]
    # The limit note stays directly above the composer; the notice beneath it.
    assert notice["limitNotice"]["bottom"] <= notice["composer"]["top"] + 0.5
    assert_notice_visible(notice, viewport)


def test_notice_styles_use_the_existing_palette():
    css = APP_CSS.read_text(encoding="utf-8")
    block = css[css.index(".accuracy-notice {"):]
    block = block[:block.index("}")]
    assert "color: var(--muted);" in block and "text-align: center;" in block


def test_homepage_footer_carries_the_same_wording_once():
    source = SITE_APP.read_text(encoding="utf-8")
    assert source.count("can make mistakes") == 1
    assert f"<span>{NOTICE}</span>" in source
    footer = source[source.index("function SiteFooter"):source.index("</footer>")]
    assert NOTICE in footer
    # The homepage composer itself sends nothing and shows no second copy.
    composer = source[source.index("function HomeComposer"):]
    assert "mistakes" not in composer[:composer.index("\n}\n")]


# --- 2. source presentation ----------------------------------------------------------------------


def assert_presented(message, name):
    label, entries = expected_entries(name)
    listing = message["list"]
    assert listing is not None, name
    assert listing["label"] == label
    assert listing["kind"] == {"Sources": "sources", "Search results": "search-results"}[label]
    assert listing["isLastBlock"] and listing["labelledBy"] == listing["labelId"]
    shown = [{"href": i["href"], "title": i["title"], "host": i["host"]} for i in listing["items"]]
    assert shown == entries, name
    for item in listing["items"]:
        assert item["target"] == "_blank" and item["rel"] == "noopener noreferrer"
        assert item["tabIndex"] == 0
        assert item["children"] == ["source-title", "source-host", "sr-only"]
        assert item["linkText"] == item["title"] + item["host"] + " (opens in a new tab)"
    # The raw footer heading paragraph is replaced, not duplicated.
    assert f"<strong>{label}</strong>" not in message["html"]
    return listing


@pytest.mark.parametrize("suffix", ("", "_360x640"))
def test_selected_sources_are_presented_as_sources(browser_results, suffix):
    _, message = rendered(browser_results, "fixture_sources_selected" + suffix)
    listing = assert_presented(message, "sources_selected")
    assert [i["host"] for i in listing["items"]] == ["federalreserve.gov", "example.org"]


@pytest.mark.parametrize("name", ("search_results", "search_results_invalid_selection"))
def test_unselected_results_are_presented_as_search_results(browser_results, name):
    _, message = rendered(browser_results, f"fixture_{name}")
    listing = assert_presented(message, name)
    assert listing["label"] == "Search results"


def test_sources_and_search_results_look_different(browser_results):
    _, sources = rendered(browser_results, "fixture_sources_selected")
    _, results = rendered(browser_results, "fixture_search_results")
    a, b = sources["list"]["items"][0], results["list"]["items"][0]
    assert a["linkStyle"]["border-left-width"] != b["linkStyle"]["border-left-width"]
    assert a["linkStyle"]["background-color"] != b["linkStyle"]["background-color"]
    assert a["titleStyle"]["color"] != b["titleStyle"]["color"]
    assert sources["list"]["labelStyle"]["color"] != results["list"]["labelStyle"]["color"]


@pytest.mark.parametrize("name", ALL_FIXTURE_SCENARIOS)
def test_source_text_meets_contrast(browser_results, name):
    _, message = rendered(browser_results, name)
    for item in (message["list"] or {}).get("items", []):
        assert contrast(item["titleStyle"]["color"], item["linkBackground"]) >= 4.5
        assert contrast(item["hostStyle"]["color"], item["linkBackground"]) >= 4.5


def test_source_links_have_a_visible_keyboard_focus_indicator(browser_results):
    focus = step(scenario(browser_results, "fixture_sources_selected"), "rendered")["focus"]
    assert focus["active"] and focus["focusVisible"]
    assert focus["outlineStyle"] == "solid" and float(focus["outlineWidth"].rstrip("px")) >= 2


@pytest.mark.parametrize("suffix", ("", "_360x640"))
def test_long_titles_and_addresses_stay_inside_the_answer(browser_results, suffix):
    snap, message = rendered(browser_results, "fixture_search_results_long" + suffix)
    listing = assert_presented(message, "search_results_long")
    bubble = message["bubble"]
    for item in listing["items"]:
        for box in (item["linkRect"], item["titleRect"], item["hostRect"]):
            assert box["left"] >= bubble["left"] - 0.5 and box["right"] <= bubble["right"] + 0.5, (item, bubble)
        # Hostnames are never shortened; the end of the domain stays visible.
        assert not item["hostOverflows"], item
    hosts = [i["host"] for i in listing["items"]]
    assert hosts == ["very-long-subdomain-segment-for-layout-testing.department.example-university.ac.uk",
                     "en.wikipedia.org", "search.example.com", "xn--bcher-kva.example"]
    # The long title is clamped visually; its whole text is still the link's text.
    assert listing["items"][0]["titleClamped"]
    assert listing["items"][0]["title"].endswith("in considerable detail")
    assert snap["notice"]["scrollWidth"] <= snap["notice"]["clientWidth"]
    # Addresses with parentheses, queries and fragments are unchanged.
    assert [i["href"] for i in listing["items"]][1:3] == [PAREN, QUERY]


def test_duplicate_results_are_shown_once_as_the_server_lists_them(browser_results):
    _, message = rendered(browser_results, "fixture_duplicates")
    listing = assert_presented(message, "duplicates")
    assert [i["href"] for i in listing["items"]] == [FED, REUTERS]
    # Both answer links to the listed address remain ordinary links.
    body_links = [a for a in clickable(message) if not a["inSourceList"]]
    assert [a["href"] for a in body_links] == [FED, FED]


def test_missing_titles_show_the_address_never_an_invented_title(browser_results):
    _, message = rendered(browser_results, "fixture_missing_titles")
    listing = assert_presented(message, "missing_titles")
    assert [i["title"] for i in listing["items"]] == [i["href"] for i in listing["items"]]


def test_malformed_addresses_are_never_listed_or_clickable(browser_results):
    _, message = rendered(browser_results, "fixture_malformed_urls")
    listing = assert_presented(message, "malformed_urls")
    assert [i["href"] for i in listing["items"]] == ["https://usable.example/page"]
    assert [a["href"] for a in clickable(message)] == ["https://usable.example/page"]


def test_no_usable_address_means_no_list_at_all(browser_results):
    _, message = rendered(browser_results, "fixture_all_unusable")
    assert message["list"] is None and clickable(message) == []
    assert "Sources" not in message["text"] and "Search results" not in message["text"]


@pytest.mark.parametrize("suffix", ("", "_360x640"))
def test_hostile_titles_are_literal_text(browser_results, suffix):
    snap, message = rendered(browser_results, "fixture_malicious_titles" + suffix)
    listing = assert_presented(message, "malicious_titles")
    assert message["forbidden"] == 0 and message["handlers"] == [] and snap["executed"] == []
    titles = [i["title"] for i in listing["items"]]
    assert titles[0].startswith("<img src=x onerror=")
    assert titles[1] == f"[click here]({EVIL})"
    assert titles[2] == "**bold** _em_ `code` # heading"
    assert titles[4].startswith("</li></ul><script>")
    assert titles[5] == "Escaped \\(y\\) and \\[z\\] delimiters"
    # Control-like text is removed by the server, never shown.
    assert "kalillac-sources" not in message["text"]
    assert all(a["href"] != EVIL for a in message["anchors"])
    assert len(clickable(message)) == len(listing["items"])


@pytest.mark.parametrize("suffix", ("", "_360x640"))
def test_titles_that_look_like_math_stay_literal_text(browser_results, suffix):
    _, message = rendered(browser_results, "fixture_math_title" + suffix)
    listing = assert_presented(message, "math_title")
    assert listing["items"][0]["title"] == "Price $$x^2$$ explained"
    assert "katex" not in message["html"]


@pytest.mark.parametrize("suffix", ("", "_360x640"))
def test_unauthorized_answer_links_stay_unclickable(browser_results, suffix):
    snap, message = rendered(browser_results, "fixture_malicious_body" + suffix)
    assert_presented(message, "malicious_body")
    hrefs = [a["href"] for a in clickable(message)]
    assert hrefs == [FED, FED], hrefs
    assert message["forbidden"] == 0 and message["handlers"] == [] and snap["executed"] == []
    for label in ("raw anchor", "script link", "unselected", "www.evil.example", "contact@evil.example"):
        assert label in message["text"]


def test_unselected_result_addresses_in_the_answer_stay_unclickable(browser_results):
    _, message = rendered(browser_results, "fixture_sources_selected")
    assert REUTERS in message["text"]
    assert REUTERS not in [a["href"] for a in clickable(message)]
    assert [a["href"] for a in clickable(message) if not a["inSourceList"]] == [FED]


@pytest.mark.parametrize("name", [n for n in SERVER])
def test_every_clickable_address_is_one_the_server_listed(browser_results, name):
    _, entries = expected_entries(name)
    allowed = {e["href"] for e in entries}
    for suffix in ("", "_360x640"):
        _, message = rendered(browser_results, f"fixture_{name}{suffix}")
        for anchor in clickable(message):
            assert anchor["href"] in allowed, (name, anchor)
            assert anchor["target"] == "_blank" and anchor["rel"] == "noopener noreferrer"


def test_source_lookalikes_inside_code_and_math_are_untouched(browser_results):
    _, message = rendered(browser_results, "fixture_code_and_math")
    assert_presented(message, "code_and_math")
    assert "**Sources**" in message["text"] and "[not a source](https://code.example/)" in message["text"]
    # The answer's own mathematics is still typeset; the list is not.
    assert 'class="katex' in message["html"]
    assert message["list"]["items"][0]["title"] == "Federal Reserve issues FOMC statement"
    assert all("code.example" not in a["href"] for a in clickable(message))


@pytest.mark.parametrize("name", [n for n in RAW if n.startswith("forged_")])
def test_lists_the_server_did_not_shape_are_left_as_rendered(browser_results, name):
    _, message = rendered(browser_results, f"fixture_{name}")
    assert message["list"] is None, name
    assert "source-" not in message["html"], name
    for anchor in message["anchors"]:
        assert anchor["href"] is None or anchor["href"].startswith(("https://a.example", "https://b.example", "/local")), anchor
        assert not (anchor["href"] or "").lower().startswith("javascript")


def test_ordinary_answers_are_unchanged(browser_results):
    _, message = rendered(browser_results, "fixture_plain_answer")
    assert message["list"] is None
    assert message["html"].count("<li>") == 2
    assert message["anchors"] == [{"href": "https://docs.example/guide", "text": "the docs", "inSourceList": False,
                                   "target": "_blank", "rel": "noopener noreferrer"}]


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_sourced_conversation_fits_every_viewport(browser_results, viewport):
    snap = step(scenario(browser_results, f"layout_{viewport}"), "conversation")
    assert snap["notice"]["scrollWidth"] <= snap["notice"]["clientWidth"], viewport
    assert len(snap["messages"]) == 2
    for message in snap["messages"]:
        assert message["list"] is not None
        for item in message["list"]["items"]:
            assert item["linkRect"]["right"] <= message["bubble"]["right"] + 0.5, (viewport, item)
            assert not item["hostOverflows"], (viewport, item)
    # Two columns where the reading column is wide enough, one otherwise.
    items = snap["messages"][0]["list"]["items"]
    side_by_side = abs(items[0]["linkRect"]["top"] - items[1]["linkRect"]["top"]) < 1
    assert side_by_side == (int(viewport.split("x")[0]) >= 820), viewport


# --- 3. existing behaviour and security boundaries -----------------------------------------------


def test_copy_and_history_keep_the_server_reply_exactly(browser_results):
    result = scenario(browser_results, "preservation")
    assert result["copied"] == [GENUINE_SOURCES]
    second = result["requests"][1]["body"]
    assert second["history"][-1] == {"role": "assistant", "content": GENUINE_SOURCES}
    assert "source_list" not in second and all("source_list" not in turn for turn in second["history"])
    snap = step(result, "after")
    assert snap["messages"][0]["list"] is not None and snap["messages"][1]["list"] is None


def presentation_source() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("  /* ---- the server's source list: presentation only")
    end = source.index("  // ---- icons")
    return source[start:end]


def test_presentation_builds_nodes_from_text_only():
    code = presentation_source()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                      "DOMParser", "createContextualFragment", "marked", "eval(", "Function("):
        assert forbidden not in code, forbidden
    assert code.count("textContent =") == 4
    # It reads the sanitized href and only ever writes that same value back.
    assert code.count('setAttribute("href"') == 1
    assert 'setAttribute("href", entries[i].href)' in code


def test_presentation_runs_after_the_existing_sanitizer_pipeline():
    source = APP_JS.read_text(encoding="utf-8")
    body = source[source.index("  function renderAssistantText(shell, text, sourceList) {"):]
    body = body[:body.index("\n  }\n")]
    order = ["renderMarkdownSafe(", "restoreMathPlaceholders(", "presentSourceList(", "renderMathSafe(",
             "wrapTables(", "hardenLinks(", "highlightCode("]
    positions = [body.index(call) for call in order]
    assert positions == sorted(positions)


def test_math_rendering_skips_only_the_presented_list():
    source = APP_JS.read_text(encoding="utf-8")
    math = block(source, "  function renderMathSafe(container) {", "\n  }\n")
    base = block(git_show("frontend/app.js"), "  function renderMathSafe(container) {", "\n  }\n")
    assert math.replace('        ignoredClasses: ["source-list"],\n', "") == base
    assert "trust: false" in math


def git_show(path: str) -> str:
    git = shutil.which("git")
    assert git, "git is required for this check"
    return subprocess.run([git, "show", f"{BASE_COMMIT}:{path}"], cwd=REPO, capture_output=True,
                          text=True, encoding="utf-8", check=True).stdout


def block(source: str, start: str, end: str) -> str:
    return source[source.index(start):source.index(end, source.index(start))]


@pytest.mark.parametrize("start, end", (
    ("  var PURIFY_CONFIG = {", "  function escapeHtml"),
    ("  function renderMarkdownSafe(text) {", "  // Wrap every table"),
    ("  function hardenLinks(container) {", "\n  }\n"),
))
def test_sanitizer_and_link_hardening_are_unchanged(start, end):
    assert block(APP_JS.read_text(encoding="utf-8"), start, end) == block(git_show("frontend/app.js"), start, end)


# --- 4. server metadata decides; Markdown alone never does --------------------------------------


@pytest.mark.parametrize("name", sorted(FALLBACK))
@pytest.mark.parametrize("suffix", ("", "_360x640"))
def test_without_matching_server_metadata_the_list_stays_ordinary_markdown(browser_results, name, suffix):
    snap, message = rendered(browser_results, f"fixture_{name}{suffix}")
    assert message["list"] is None, name
    assert "source-" not in message["html"], name
    # The ordinary rendering: the footer heading and list exactly as Markdown
    # shows them, with the same links the reply always had (no more, no fewer).
    assert "<p><strong>" in message["html"] and "<ul>" in message["html"]
    for anchor in clickable(message):
        assert anchor["target"] == "_blank" and anchor["rel"] == "noopener noreferrer"
    reply = FIXTURES[name]["reply"]
    assert len(clickable(message)) == reply.count("](https://")
    assert_notice_visible(snap["notice"], name)


@pytest.mark.parametrize("name", ["sources_selected", "search_results"])
def test_the_same_footer_is_styled_only_with_its_metadata(browser_results, name):
    _, signed = rendered(browser_results, f"fixture_{name}")
    unsigned = rendered(browser_results, f"fixture_fallback_unsigned_{'sources' if name == 'sources_selected' else 'search_results'}")[1]
    assert signed["list"] is not None and unsigned["list"] is None
    assert [a["href"] for a in clickable(signed)] == [a["href"] for a in clickable(unsigned)]


def test_retry_uses_the_metadata_of_the_response_that_succeeded(browser_results):
    result = scenario(browser_results, "retry_with_metadata")
    snap = step(result, "after_retry")
    assert len(snap["messages"]) == 1 and snap["messages"][0]["list"] is not None
    assert snap["messages"][0]["list"]["kind"] == "sources"
    assert [r["body"]["message"] for r in result["requests"]] == ["question"] * 3
    plain = step(result, "after_plain_retry")
    assert plain["messages"][-1]["list"] is None


def test_metadata_never_reaches_copy_history_or_requests(browser_results):
    for name in SCENARIOS:
        for request in scenario(browser_results, name)["requests"]:
            assert "source_list" not in request["body"], name
