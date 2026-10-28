"""Public identity and legal contracts, checked against sources and the built site.

All browser requests stay on loopback using the existing isolated harness.
The former hostname is allowed only in the exact deployment-contract sentences.
"""
from __future__ import annotations

from html.parser import HTMLParser
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from test_homepage_handoff import (
    REPO, SITE, SITE_DIST, VIEWPORTS, Site, browser_page, require_browser_and_build,
)
from test_route_navigation import Page

ORIGIN = "https://kalillac.ai"
ROUTES = {"/": "Kalillac AI", "/privacy": "Privacy", "/terms": "Terms of Use"}


class Head(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def values(self, tag, key, value, attr):
        return [attrs.get(attr) for name, attrs in self.tags
                if name == tag and attrs.get(key) == value]

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def built(path):
    file = SITE_DIST / ("index.html" if path == "/" else path.lstrip("/") + ".html")
    assert file.is_file(), "Build the site before public identity verification"
    return file.read_text(encoding="utf-8")


def test_former_domain_is_confined_to_exact_legacy_redirect_documentation():
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=REPO,
    ).decode().split("\0")
    found = []
    for name in filter(None, names):
        data = (REPO / name).read_bytes()
        if b"\0" in data:
            continue
        for line in data.decode("utf-8", errors="replace").splitlines():
            if re.search(r"kalillac[.]com", line, re.I):
                found.append((name, line))
    # Each exception is an exact, reviewed deployment-contract sentence.
    legacy = "kalillac" + ".com"
    expected_lines = [
        f"The legacy redirect domain is {legacy}; it remains operational until the final verified redirect cutover.",
        f"The future {legacy} redirect must preserve normal GET paths and query strings.",
        f"The anonymous visitor cookie is host-only and will not transfer between {legacy} and kalillac.ai; "
        "visitors receive a one-time new anonymous identity on the new domain.",
    ]
    assert found == [("site/DOMAIN-MIGRATION.md", line) for line in expected_lines], found


@pytest.mark.parametrize("path", ROUTES)
def test_built_public_page_has_apex_canonical_and_social_identity(path):
    html = built(path)
    head = Head(html)
    assert head.values("link", "rel", "canonical", "href") == [ORIGIN + path]
    assert head.values("meta", "property", "og:url", "content") == [ORIGIN + path]
    for key, attribute in (("og:image", "property"), ("twitter:image", "name")):
        assert head.values("meta", attribute, key, "content") == [
            ORIGIN + "/assets/kalillac-ai-logo.png"
        ]
    assert (SITE_DIST / "assets/kalillac-ai-logo.png").is_file()
    assert ROUTES[path] in re.search(r"<title>([^<]+)</title>", html).group(1)
    assert head.values("meta", "name", "robots", "content") == ["index, follow"]
    for key, attribute in (("description", "name"), ("og:description", "property"),
                           ("twitter:description", "name")):
        values = head.values("meta", attribute, key, "content")
        assert len(values) == 1 and values[0]
    assert head.values("link", "rel", "icon", "href") == ["/favicon.ico"]
    assert not re.search(r"kalillac[.]com|https?://www[.]kalillac[.]ai", html, re.I)


def test_build_owns_complete_apex_crawler_files_and_preserves_other_live_assets():
    tree = ET.fromstring((SITE_DIST / "sitemap.xml").read_text(encoding="utf-8"))
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    assert [node.text for node in tree.findall("s:url/s:loc", ns)] == [
        ORIGIN + path for path in ROUTES
    ]
    assert (SITE_DIST / "robots.txt").read_text(encoding="utf-8") == (
        "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /app/\n"
        f"Sitemap: {ORIGIN}/sitemap.xml\n"
    )
    preserved = (SITE / "deployment-preserve-live-assets.txt").read_text().splitlines()
    assert "/robots.txt" not in preserved and "/sitemap.xml" not in preserved
    assert "/site.webmanifest" in preserved and "/favicon.ico" in preserved
    for path in preserved:
        assert not (SITE_DIST / path.lstrip("/")).exists()
    assert not (SITE_DIST / "app").exists()
    chat = (REPO / "frontend/index.html").read_text(encoding="utf-8")
    assert Head(chat).values("meta", "name", "robots", "content") == ["noindex, follow"]


def test_built_public_content_has_no_former_canonical_or_obsolete_provider_claim():
    files = [p for p in SITE_DIST.rglob("*") if p.suffix in (".js", ".html", ".css", ".xml", ".txt")]
    assert files and any(p.suffix == ".js" for p in files)
    for path in files:
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"kalillac[.]com|https?://www[.]kalillac[.]ai", source, re.I), path
        # An explicit negative statement remains accurate; positive legacy claims cannot ship.
        source = source.replace(
            "Cloudflare Workers AI and Groq are not part of the current active model-provider path.", ""
        )
        assert not re.search(r"\bGroq\b|Workers AI", source), path


@pytest.mark.parametrize("path", ["/privacy", "/terms"])
def test_built_legal_pages_describe_current_service(path, tmp_path_factory):
    browser = require_browser_and_build()
    profile = tmp_path_factory.mktemp("edge-public-legal")
    site = Site()
    with site.http:
        with browser_page(browser, profile, VIEWPORTS["desktop"], False) as (dt, session):
            page = Page(dt, session)
            dt.call("Page.navigate", {"url": site.http.origin + path}, session)
            page.wait_for("document.querySelector('.doc-updated')")
            text = page.eval("return document.querySelector('main').innerText;")
            assert page.eval("return document.querySelector('link[rel=canonical]').href;") == ORIGIN + path
    shutil.rmtree(profile, ignore_errors=True)
    for phrase in (
        "kalillac.ai", "10 successful responses per browser per UTC day",
        "__Host-kalillac_visitor", "SHA-256", "PostgreSQL", "GPT-6 Luna",
        "OpenAI", "Brave", "Tavily", "Cloudflare", "DigitalOcean",
        "cannot allow OpenAI, Brave, Tavily, Cloudflare, or DigitalOcean to process.",
        "No account or paid subscription is currently offered.",
        # Privacy was materially updated again for End & erase; Terms was not.
        "Last updated: October 28, 2026" if path == "/privacy" else "Last updated: October 27, 2026",
    ):
        assert phrase in text, (path, phrase)
    assert re.search(r"18|eighteen", text)
    if path == "/privacy":
        for phrase in (
            "The anonymous daily allowance is active in the current service.",
            "HttpOnly, Secure, SameSite=Lax, Path=/ and no Domain attribute",
            "does not store the raw visitor value in its PostgreSQL database",
            "does not save chat transcripts as persistent conversations",
            "They do not mean that no copy, log, or metadata can exist anywhere else.",
        ):
            assert phrase in text, phrase
    else:
        assert "no automatic model-provider fallback" in text
        assert "host-only" in text
    text = text.replace(
        "Cloudflare Workers AI and Groq are not part of the current active model-provider path.", ""
    )
    assert not re.search(r"\bGroq\b|Workers AI", text)
