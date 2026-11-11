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
        # Privacy was materially updated again for the homepage composer's handoff; Terms was not.
        "Last updated: November 6, 2026" if path == "/privacy" else "Last updated: October 27, 2026",
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


# --- public branding: contact address, footer and positioning ------------------------------------

CONTACT = "contact@arogetech.com"
ACCURACY_NOTICE = "Kalillac can make mistakes. Verify important information."
FOOTER_LINKS = [["Product", "/#product"], ["Privacy", "/privacy"], ["How it works", "/#how-it-works"],
                ["Terms", "/terms"]]
CONTACT_SENTENCE = {
    "/privacy": f"Questions and corrections can be sent to Kalillac AI at {CONTACT}.",
    "/terms": f"Questions about these Terms can be sent to Kalillac AI at {CONTACT}.",
}
# Positioning the public copy must not carry: Kalillac is presented as a product.
OUTDATED_POSITIONING = re.compile(
    r"\bstudents?\b|computer[ -]science|\bhobby\b|side project|personal project|class project|school project"
    r"|programming exercise|learning (?:demonstration|exercise|project)|\bexperiment(?:al|s)?\b", re.I)


def public_source_files():
    """The public website's own source and the chat page's markup."""
    files = [p for p in (SITE / "src").rglob("*") if p.suffix in (".ts", ".tsx", ".css")]
    files += [SITE / "index.html", SITE / "public-metadata.ts", REPO / "frontend/index.html"]
    assert len(files) > 5 and all(p.is_file() for p in files)
    return files


def test_public_source_and_build_have_no_linkedin_reference():
    built_files = [p for p in SITE_DIST.rglob("*") if p.suffix in (".js", ".html", ".css", ".xml", ".txt")]
    assert built_files and any(p.suffix == ".js" for p in built_files)
    for path in public_source_files() + built_files + [REPO / "frontend/app.js", REPO / "frontend/app.css"]:
        assert not re.search(r"linkedin|lnkd[.]in", path.read_text(encoding="utf-8"), re.I), path


def test_public_source_has_no_outdated_positioning():
    for path in public_source_files():
        found = OUTDATED_POSITIONING.search(path.read_text(encoding="utf-8"))
        assert not found, (path, found.group(0))


def test_contact_address_is_exactly_the_two_legal_contact_links():
    source = (SITE / "src/App.tsx").read_text(encoding="utf-8")
    link = f'<a href="mailto:{CONTACT}" data-testid="link-%s-contact">{CONTACT}</a>'
    for page in ("privacy", "terms"):
        assert source.count(link % page) == 1, page
    assert source.count("arogetech") == 4 and source.count("mailto:") == 2
    # No formation claim: the operator's legal form is not stated anywhere public.
    for path in public_source_files():
        assert not re.search(r"\bLLC\b|Aroge Tech", path.read_text(encoding="utf-8")), path
    # The approved accuracy notice stays in the footer and beneath the chat composer.
    assert source.count(f"<span>{ACCURACY_NOTICE}</span>") == 1
    assert (REPO / "frontend/index.html").read_text(encoding="utf-8").count(ACCURACY_NOTICE) == 1


@pytest.mark.parametrize("viewport", ["desktop", "mobile"])
@pytest.mark.parametrize("path", ROUTES)
def test_public_pages_render_footer_contact_and_current_branding(path, viewport, tmp_path_factory):
    browser = require_browser_and_build()
    profile = tmp_path_factory.mktemp("edge-public-branding")
    site = Site()
    with site.http:
        with browser_page(browser, profile, VIEWPORTS[viewport], viewport == "mobile") as (dt, session):
            page = Page(dt, session)
            dt.call("Page.navigate", {"url": site.http.origin + path}, session)
            page.wait_for("document.querySelector('.site-footer .footer-bottom')"
                          + ("" if path == "/" else " && document.querySelector('.doc-updated')"))
            seen = page.eval("""
                const links = (root) => Array.from(root.querySelectorAll('a')).map(a => {
                  const r = a.getBoundingClientRect();
                  return {text: a.textContent.trim(), href: a.getAttribute('href'), target: a.getAttribute('target'),
                          tabIndex: a.tabIndex, visible: r.width > 0 && r.height > 0,
                          inside: r.left >= -0.5 && r.right <= window.innerWidth + 0.5};
                });
                const footer = document.querySelector('.site-footer');
                const contact = document.querySelector('section#contact');
                return {
                  text: document.body.innerText,
                  hrefs: Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href')),
                  footerLinks: links(footer.querySelector('.footer-links')),
                  footerBottom: footer.querySelector('.footer-bottom').innerText,
                  footerHeight: footer.getBoundingClientRect().height,
                  contactText: contact ? contact.innerText : null,
                  contactLinks: contact ? links(contact) : null,
                  overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                };
            """)
    shutil.rmtree(profile, ignore_errors=True)

    # The shared footer: the four site links, nothing external, and the approved notice.
    assert [[link["text"], link["href"]] for link in seen["footerLinks"]] == FOOTER_LINKS
    assert all(link["visible"] and link["inside"] and link["tabIndex"] == 0 and link["target"] is None
               for link in seen["footerLinks"]), seen["footerLinks"]
    assert ACCURACY_NOTICE in seen["footerBottom"] and "Adults 18+" in seen["footerBottom"]
    assert seen["footerHeight"] > 0 and seen["overflow"] <= 0

    # No LinkedIn link or mention, and no outdated positioning, anywhere on the page.
    assert not [href for href in seen["hrefs"] if re.search(r"linkedin|lnkd[.]in", href, re.I)]
    assert not re.search(r"linkedin", seen["text"], re.I)
    found = OUTDATED_POSITIONING.search(seen["text"])
    assert not found, found.group(0)

    if path == "/":
        assert seen["contactLinks"] is None
        assert not [href for href in seen["hrefs"] if href.startswith("mailto:")]
        return
    # Privacy and Terms: one ordinary mailto link in the Contact section.
    assert seen["contactText"].split("\n", 1)[0].strip() == "Contact"
    assert CONTACT_SENTENCE[path] in seen["contactText"]
    assert seen["contactLinks"] == [{"text": CONTACT, "href": "mailto:" + CONTACT, "target": None,
                                     "tabIndex": 0, "visible": True, "inside": True}]
    assert [href for href in seen["hrefs"] if href.startswith("mailto:")] == ["mailto:" + CONTACT]
