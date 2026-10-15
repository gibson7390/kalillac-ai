"""User-authored -site: exclusions never become positive domain restrictions.

get_search_domain_filters(message) turns domains the user names into the
explicit include_domains / goggles allowlist for both chat search paths. A
domain that appears only inside a -site: exclusion must not be turned into
that allowlist; a separate positive mention of the same host still counts.

The chat paths run with scripted providers and a scripted model; any other
network access fails. Assertions read the real outgoing provider payloads.
"""

from __future__ import annotations

import pytest

import app_fastapi_candidate as app
from test_search_provider_policy import (  # noqa: F401  (fixtures)
    _lookup_reply, _lookup_tool, brave_result, native_lookup, providers,
)


EXCLUSION_ONLY = "Search the web for Example Runtime 2.10.0 release notes -site:archive.example.org"
EXCLUDED_PATH = "Search the web for Example Runtime 2.10.0 release notes -site:archive.example.org/old"
MIXED = "Search the web for site:docs.example.org Example Runtime 2.10.0 release notes -site:archive.example.org"
SAME_HOST = ("Search the web on docs.example.org for Example Runtime 2.10.0 release notes "
             "-site:docs.example.org/old")

CASES = [
    # message, model search query (native), expected filters, expected Brave goggles
    (EXCLUSION_ONLY, "Example Runtime 2.10.0 release notes -site:archive.example.org", [], None),
    (EXCLUDED_PATH, "Example Runtime 2.10.0 release notes -site:archive.example.org/old", [], None),
    (MIXED, "site:docs.example.org Example Runtime 2.10.0 release notes -site:archive.example.org",
     ["docs.example.org"], "$discard\n$site=docs.example.org"),
    (SAME_HOST, "Example Runtime 2.10.0 release notes -site:docs.example.org/old",
     ["docs.example.org"], "$discard\n$site=docs.example.org"),
]
IDS = ["exclusion-only", "excluded-path", "mixed", "same-host"]


def query_of(call):
    return call["payload"].get("q", call["payload"].get("query"))


@pytest.mark.parametrize("message,query,filters,goggles", CASES, ids=IDS)
def test_the_domain_filter_boundary_ignores_exclusions(message, query, filters, goggles):
    assert app.get_search_domain_filters(message) == filters


@pytest.mark.parametrize("message,query,filters,goggles", CASES, ids=IDS)
def test_native_chat_search_does_not_allowlist_an_excluded_domain(native_lookup, message, query, filters, goggles):
    state = native_lookup
    state["brave"] = [brave_result(url="https://docs.example.org/notes")]
    state["turns"] = [_lookup_tool(query), _lookup_reply("Scripted answer.")]
    app.chat(message, [], session_id="native-exclusion")
    [brave] = state["calls"]
    assert query_of(brave) == query                                     # the operator reaches Brave intact
    assert brave["payload"].get("goggles") == goggles


@pytest.mark.parametrize("message,query,filters,goggles", CASES, ids=IDS)
def test_legacy_chat_search_does_not_allowlist_an_excluded_domain(
        native_lookup, monkeypatch, message, query, filters, goggles):
    state = native_lookup
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    assert app.classify_request(message, []) == "web_search"
    state["brave"] = [brave_result(url="https://docs.example.org/notes")]
    state["turns"] = [_lookup_reply("Scripted answer.")]
    app.chat(message, [], session_id="legacy-exclusion")
    [brave] = state["calls"]
    sent = query_of(brave)
    assert "-site:" + query.split("-site:")[1] in sent.split()           # the exclusion stays an exclusion
    assert " - " not in sent
    assert brave["payload"].get("goggles") == goggles


@pytest.mark.parametrize("message,filters", [
    ("Search the web on docs.example.org for release notes", ["docs.example.org"]),
    ("Search https://www.example.org/docs for the release notes", ["www.example.org"]),
    ("Search the web for site:docs.example.org release notes", ["docs.example.org"]),
    ("Search -site:a.example.org and site:b.example.org for notes", ["b.example.org"]),
])
def test_positive_domains_urls_and_site_operators_still_count(message, filters):
    assert app.get_search_domain_filters(message) == filters
