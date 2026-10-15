"""Technical identifiers survive search-query cleanup.

make_concise_query() used to replace every ?.!,;: with a space, turning
"3.14.8" into "3 14 8" and "site:python.org/downloads/release" into
"site python org/downloads/release". Punctuation is now stripped only at
token edges, and site: operators are kept whole (a bare-domain operator is
removed only when that domain is already sent separately as a requested
domain).

Every assertion reads the actual outgoing provider payload through the
existing scripted transports. Restrictions (Brave goggles, Tavily
include_domains) are derived from the raw query and must not change.
Correct queries do not by themselves prove better live answers.
"""

from __future__ import annotations

import pytest

import app_fastapi_candidate as app
from kalillac_routing import search_providers as sp
from kalillac_routing.bounded_transport import TransportHTTPError
from test_search_provider_policy import (  # noqa: F401  (fixtures)
    _lookup_reply, _lookup_tool, _run_lookup, brave_result, native_lookup, providers, tavily_result,
)


REPORTED = "site:python.org/downloads/release Python 3.14.8 latest stable release Python 3.15.0"
REPORTED_SENT = ("site:python.org/downloads/release Python 3.14.8 latest stable release Python 3.15.0 "
                 "official release history")
BASIC = "site:python.org Python 3.15.0 stable final release"
IN_DOMAIN = "https://www.python.org/downloads/"
GOGGLES = "$discard\n$site=python.org"


def query_of(call):
    payload = call["payload"]
    return payload.get("q", payload.get("query"))


# --- the reported query on every provider path ------------------------------------------------------


def test_brave_primary_sends_the_reported_query_intact(providers):
    providers["brave"] = [brave_result(url=IN_DOMAIN)]
    app.run_web_search(REPORTED)
    [brave] = providers["calls"]
    assert query_of(brave) == REPORTED_SENT
    assert brave["payload"]["goggles"] == GOGGLES


def test_brave_failure_fallback_sends_the_reported_query_intact(providers):
    providers["brave"] = [TransportHTTPError(500)]
    providers["tavily"] = [tavily_result(url=IN_DOMAIN)]
    app.run_web_search(REPORTED)
    brave, tavily = providers["calls"]
    assert query_of(brave) == query_of(tavily) == REPORTED_SENT
    assert brave["payload"]["goggles"] == GOGGLES
    assert tavily["payload"]["include_domains"] == ["python.org"]


def test_legacy_tavily_only_sends_the_reported_query_intact(providers, monkeypatch):
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", sp.SearchProviderPolicy("tavily", "none"))
    providers["tavily"] = [tavily_result(url=IN_DOMAIN), tavily_result(url=IN_DOMAIN)]
    app.run_web_search(REPORTED)
    assert providers["calls"]
    assert all(query_of(call) == REPORTED_SENT for call in providers["calls"])
    assert all(call["payload"].get("include_domains") is None for call in providers["calls"])  # unchanged policy


def test_native_brave_and_tavily_follow_up_send_the_reported_query_intact(native_lookup):
    state = native_lookup
    state["brave"] = [brave_result(content="Evidence.")]
    state["tavily"] = [tavily_result(content="Evidence.")]
    state["turns"] = [_lookup_tool(REPORTED), _lookup_tool(REPORTED, "lookup-2"), _lookup_reply("Scripted.")]
    _run_lookup(state, message="Search the web: what is the latest stable Python release as of today?")
    brave, tavily = state["calls"]
    assert query_of(brave) == query_of(tavily) == REPORTED_SENT
    assert brave["payload"]["goggles"] == GOGGLES
    assert tavily["payload"]["include_domains"] == ["python.org"]


def test_the_already_correct_basic_query_is_unchanged(providers):
    providers["brave"] = [brave_result(url=IN_DOMAIN)]
    app.run_web_search(BASIC)
    [brave] = providers["calls"]
    assert query_of(brave) == BASIC
    assert brave["payload"]["goggles"] == GOGGLES


# --- generic identifiers ------------------------------------------------------------------------------


@pytest.mark.parametrize("query,identifiers", [
    ("Search the web for the latest Example Runtime 2.10.0rc1 and 3.0.0-beta.2 release notes.",
     ["2.10.0rc1", "3.0.0-beta.2"]),
    ("Search the web for Node.js and ASP.NET Core release notes.", ["Node.js", "ASP.NET"]),
    ("Search the web for the latest .NET 9.0.1 servicing update.", [".NET", "9.0.1"]),
    ("Search the web for example-lib v1.2.3 changelog, please.", ["example-lib", "v1.2.3"]),
])
def test_versions_prereleases_and_dotted_names_survive(providers, query, identifiers):
    providers["brave"] = [brave_result()]
    app.run_web_search(query)
    sent = query_of(providers["calls"][0])
    for identifier in identifiers:
        assert identifier in sent.split(), (identifier, sent)
    assert not sent.endswith((".", ",", "?"))


def test_conversational_cleanup_still_applies(providers):
    providers["brave"] = [brave_result()]
    app.run_web_search("Can you search the web for the latest Example Widget 4.2.1 release, and tell me its date?")
    sent = query_of(providers["calls"][0])
    assert "4.2.1" in sent.split()
    for removed in ("Can you", "search the web", "tell me", "release,"):
        assert removed.lower() not in sent.lower()


# --- site: restrictions ----------------------------------------------------------------------------


def test_a_bare_site_operator_for_a_requested_domain_is_removed_whole(providers):
    providers["brave"] = [brave_result(url="https://docs.example.test/widget")]
    app.run_web_search("site:docs.example.test latest Example Widget 4.2.1 release", ["docs.example.test"])
    [brave] = providers["calls"]
    sent = query_of(brave)
    assert "site:" not in sent and "docs.example.test" not in sent       # carried by the restriction instead
    assert "4.2.1" in sent.split()
    assert brave["payload"]["goggles"] == "$discard\n$site=docs.example.test"


def test_a_path_site_operator_is_kept_whole_even_with_a_requested_domain(providers):
    providers["brave"] = [brave_result(url="https://docs.example.test/api/widget")]
    app.run_web_search("site:docs.example.test/api latest Example Widget 4.2.1 release", ["docs.example.test"])
    [brave] = providers["calls"]
    sent = query_of(brave)
    assert "site:docs.example.test/api" in sent.split()
    assert "site:/api" not in sent and "site: " not in sent
    assert brave["payload"]["goggles"] == "$discard\n$site=docs.example.test"


def test_a_site_operator_for_another_domain_is_not_dropped(providers):
    providers["brave"] = [brave_result(url="https://vendor.example.test/notes")]
    app.run_web_search("Search the web: site:vendor.example.test latest Example Widget 4.2.1 notes")
    sent = query_of(providers["calls"][0])
    assert "site:vendor.example.test" in sent.split()


# --- site: exclusions --------------------------------------------------------------------------------


def run_with_fallback(providers, query, domains=None):
    """Brave fails, so both the Brave and the Tavily fallback payloads are captured."""
    providers["brave"] = [TransportHTTPError(500)]
    providers["tavily"] = [tavily_result(url="https://docs.example.test/a")]
    app.run_web_search(query, domains)
    brave, tavily = providers["calls"]
    assert query_of(brave) == query_of(tavily)
    return query_of(brave), brave["payload"].get("goggles"), tavily["payload"].get("include_domains")


@pytest.mark.parametrize("query,expected", [
    ("Search the web for latest Example Runtime 2.10.0 -site:archive.example.test",
     "latest Example Runtime 2.10.0 -site:archive.example.test"),
    ("Search the web for Example Runtime 2.10.0 notes -site:archive.example.test/old",
     "Example Runtime 2.10.0 notes -site:archive.example.test/old"),
])
def test_an_exclusion_keeps_its_sign_and_never_becomes_an_allowlist(providers, query, expected):
    sent, goggles, include_domains = run_with_fallback(providers, query)
    assert sent == expected
    assert " - " not in sent and not sent.endswith("-")
    assert goggles is None and include_domains is None


def test_mixed_inclusion_and_exclusion_operators_stay_distinct(providers):
    sent, goggles, include_domains = run_with_fallback(
        providers, "Search the web for site:docs.example.test Example Runtime 2.10.0 -site:archive.example.test")
    assert sent == "site:docs.example.test Example Runtime 2.10.0 -site:archive.example.test"
    assert goggles == "$discard\n$site=docs.example.test"               # only the positive operator
    assert include_domains == ["docs.example.test"]


def test_an_exclusion_is_kept_even_when_its_domain_is_requested(providers):
    sent, goggles, include_domains = run_with_fallback(
        providers, "Search the web for Example Runtime 2.10.0 -site:archive.example.test", ["archive.example.test"])
    assert sent == "Example Runtime 2.10.0 -site:archive.example.test"
    assert goggles == "$discard\n$site=archive.example.test"            # the explicit request, unchanged policy
    assert include_domains == ["archive.example.test"]


def test_positive_operators_and_identifiers_are_unaffected_by_exclusions(providers):
    sent, goggles, _ = run_with_fallback(
        providers, "Search the web for site:docs.example.test/api Example Runtime 3.0.0-beta.2 -site:old.example.test")
    assert sent.split() == ["site:docs.example.test/api", "Example", "Runtime", "3.0.0-beta.2",
                            "-site:old.example.test"]
    assert goggles == "$discard\n$site=docs.example.test"
