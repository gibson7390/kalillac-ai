"""Conflicting native search evidence: instructions and preserved evidence.

The native evidence-recovery instructions now say that conflicting evidence
must be reconciled before a confident current or latest claim, that later
results do not override earlier evidence by arriving later, that a
candidate's own release page or "latest in its series" does not establish
latest overall, that URL variants of one page are one source, that an
unresolved conflict justifies the existing targeted-verification search,
and that an unresolved conflict must be stated rather than decided.

The providers and the model are scripted (fictional product, fictional
sites). These tests prove what the model receives in every round and that
the bounded lookup, provider order and earlier evidence are unchanged. They
do not prove that a real model reconciles conflicts; that needs staging.
"""

from __future__ import annotations

import pytest

import app_fastapi_candidate as app
from kalillac_routing import search_providers as sp, tavily_transport
from kalillac_routing.bounded_transport import TransportHTTPError
from test_search_provider_policy import (  # noqa: F401  (fixtures)
    _lookup_outputs, _lookup_reply, _lookup_tool, _run_lookup, native_lookup, providers,
)


QUESTION = "Search the web: what is the latest stable Example Runtime release as of today?"
OFFICIAL = "https://runtime.example.test/downloads"
THIRD_PARTY = "https://tracker.example.test/runtime"
SERIES_PAGE = "https://runtime.example.test/releases/8.4.2"
STATUS_PAGE = "https://runtime.example.test/status"
OFFICIAL_CLAIM = "Example Runtime 9.0 is the latest release."
THIRD_PARTY_CLAIM = "Example Runtime 9.0 is a preview build; 8.4 is the current stable series."
SERIES_CLAIM = "Example Runtime 8.4.2 is the latest maintenance release in the 8.4 series."
STATUS_CLAIM = "Example Runtime 9.0 reached general availability and is the current stable release."

RULES = [
    "including that candidate's own release or announcement page or a statement that it is the latest "
    "within its own series, does not establish that it is the latest, newest, current, highest, lowest, "
    "first or most recent overall.",
    "Weigh all evidence gathered so far together. A later search result does not override earlier evidence "
    "merely because it arrived later.",
    "Several URL variants of the same page are one source, not independent corroboration.",
    "When sources disagree about which candidate is current or about a candidate's status, reconcile that "
    "conflict before making a confident current or latest claim.",
    "or the evidence leaves a conflict between candidates or about a candidate's status unresolved, request "
    "one narrower search_web in another later model round.",
    "Focus that query on the conflict or the missing fact; do not repeat or broaden an earlier query.",
    "If a conflict remains unresolved, state which candidates or statuses the sources disagree about instead "
    "of declaring one of them current or latest.",
]


def conflicting_brave():
    return {"grounding": {"generic": [
                {"url": OFFICIAL, "title": "Downloads", "snippets": [OFFICIAL_CLAIM]},
                {"url": THIRD_PARTY, "title": "Release tracker", "snippets": [THIRD_PARTY_CLAIM]}]},
            "sources": {OFFICIAL: {"title": "Downloads"}, THIRD_PARTY: {"title": "Release tracker"}}}


def series_page_variants():
    variants = [SERIES_PAGE, SERIES_PAGE + "?utm_source=feed", SERIES_PAGE + "/", SERIES_PAGE + "#notes"]
    return {"results": [{"url": url, "title": "8.4.2", "content": SERIES_CLAIM, "score": 0.8} for url in variants]}


def status_result():
    return {"results": [{"url": STATUS_PAGE, "title": "Release status", "content": STATUS_CLAIM, "score": 0.9}]}


def conflict_flow(state, verification=None, extra_turns=()):
    state["brave"] = [conflicting_brave()]
    state["tavily"] = [series_page_variants(), verification if verification is not None else status_result()]
    state["turns"] = [_lookup_tool("latest stable Example Runtime release"),
                      _lookup_tool("Example Runtime latest stable version", "lookup-2"),
                      _lookup_tool("Example Runtime 9.0 stable or preview status", "lookup-3"),
                      *extra_turns, _lookup_reply("Scripted answer.")]


def contents(output):
    return [result["content"] for result in output["results"]]


def normalized(text):
    return " ".join(text.split())


def test_every_model_round_receives_the_conflict_rules_once(native_lookup):
    state = native_lookup
    conflict_flow(state)
    _run_lookup(state, message=QUESTION)
    assert len(state["model"]) == 4
    first = state["model"][0]["instructions"]
    assert all(round_["instructions"] == first for round_ in state["model"])
    text = normalized(first)
    for rule in RULES:
        assert text.count(rule) == 1, rule
    assert "targeted-verification" in text and "no additional provider search" in text


def test_conflicting_evidence_stays_available_through_the_focused_verification(native_lookup):
    state = native_lookup
    conflict_flow(state)
    answer, budget = _run_lookup(state, message=QUESTION)
    assert [c["provider"] for c in state["calls"]] == ["brave", "tavily", "tavily"]
    assert budget.search_attempts == 3
    assert state["calls"][2]["payload"]["query"] == "Example Runtime 9.0 stable or preview status"
    brave, discovery, verification = _lookup_outputs(state)
    # The final round still carries the earlier, conflicting evidence beside the later results.
    assert OFFICIAL_CLAIM in contents(brave) and THIRD_PARTY_CLAIM in contents(brave)
    assert contents(discovery) and set(contents(discovery)) == {SERIES_CLAIM}
    assert contents(verification) == [STATUS_CLAIM]
    assert answer.startswith("Scripted answer.")


def test_repeated_page_variants_reach_the_model_as_returned(native_lookup):
    state = native_lookup
    conflict_flow(state)
    _run_lookup(state, message=QUESTION)
    discovery = _lookup_outputs(state)[1]
    urls = [result["url"] for result in discovery["results"]]
    assert len(urls) <= app.MAX_SEARCH_RESULTS
    assert all(url.startswith(SERIES_PAGE) for url in urls)            # one page, several URL variants
    assert set(contents(discovery)) == {SERIES_CLAIM}
    assert set(urls) == {
        SERIES_PAGE,
        SERIES_PAGE + "?utm_source=feed",
        SERIES_PAGE + "/",
        SERIES_PAGE + "#notes",
    }


def test_unavailable_verification_keeps_earlier_evidence_and_the_uncertainty_rule(native_lookup):
    state = native_lookup
    conflict_flow(state, verification=TransportHTTPError(500))
    answer, _ = _run_lookup(state, message=QUESTION)
    assert [c["provider"] for c in state["calls"]] == ["brave", "tavily", "tavily"]
    outputs = _lookup_outputs(state)
    assert [o["status"] for o in outputs] == ["ok", "ok", "unavailable"]
    assert OFFICIAL_CLAIM in contents(outputs[0]) and SERIES_CLAIM in contents(outputs[1])
    assert RULES[-1] in normalized(state["model"][-1]["instructions"])
    assert answer.startswith("Scripted answer.")


def test_an_exhausted_lookup_rejects_a_further_search_without_a_provider_call(native_lookup):
    state = native_lookup
    conflict_flow(state, extra_turns=[_lookup_tool("Example Runtime release notes", "lookup-4")])
    _, budget = _run_lookup(state, message=QUESTION)
    assert [c["provider"] for c in state["calls"]] == ["brave", "tavily", "tavily"]
    assert [o["status"] for o in _lookup_outputs(state)] == ["ok", "ok", "ok", "rejected"]
    assert budget.search_attempts == 3


def test_the_recovery_rules_stay_out_of_lookups_without_a_follow_up(native_lookup, monkeypatch):
    state = native_lookup
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", sp.SearchProviderPolicy("brave", "none"))
    state["brave"] = [conflicting_brave()]
    state["turns"] = [_lookup_tool("latest stable Example Runtime release"), _lookup_reply("Scripted answer.")]
    _run_lookup(state, message=QUESTION)
    assert [c["provider"] for c in state["calls"]] == ["brave"]
    instructions = normalized(state["model"][0]["instructions"])
    assert "NATIVE SEARCH EVIDENCE RECOVERY" not in instructions
    assert all(rule not in instructions for rule in RULES)
    assert all(c["url"] != tavily_transport.SEARCH_URL for c in state["calls"])
