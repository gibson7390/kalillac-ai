"""The corpus is well-formed, synthetic, and free of operational content.

These checks guard the repository corpus itself: schema, tier vocabulary,
unique ids, synthetic hosts only, and no ready-to-send operational payloads
(harmful cases must be neutral descriptors, not working methods).
"""
from __future__ import annotations

import re

from tests.safety_suite import loader

TIERS = {"ANSWER", "NARROW_BOUNDARY", "FULL_BOUNDARY", "CLARIFY", "CRISIS_FIXED"}
REQUIRED = {"id", "category", "subcategory", "tier", "turns", "route", "encoding",
            "search_mode", "expected", "rubric", "human_review", "control"}

# Any host captured under a real TLD must be a documentation host: a label that
# is exactly "example" (example.com/.org/.net), or a reserved TLD. Hosts under
# .test/.invalid/.localhost are inherently reserved and are not captured here.
_ALLOWED_HOST = re.compile(r"(?:^|\.)example(?:\.|$)|\.(?:test|invalid|localhost)$", re.I)
_ANY_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|gov|edu|io|co|ai|us|uk|de)\b", re.I)
# A few real product names appear as topics (groq.com) and are allowlisted.
# groq.com is a topic; findahelpline.com appears only inside the application's
# own fixed crisis reply, which some conversations quote back as history.
_HOST_ALLOWLIST = {"groq.com", "findahelpline.com"}


def test_corpus_loads_and_counts_are_in_range():
    corpus = loader.load_corpus()
    assert corpus["version"] == 1
    assert 300 <= len(corpus["cases"]) <= 450


def test_every_case_has_the_required_schema():
    for case in loader.cases():
        assert REQUIRED <= set(case), (case["id"], REQUIRED - set(case))
        assert case["tier"] in TIERS, case["id"]
        assert case["turns"] and all(t["role"] in {"user", "assistant", "system", "developer"} for t in case["turns"])
        assert isinstance(case["expected"], dict)
        assert isinstance(case["control"], bool) and isinstance(case["human_review"], bool)


def test_ids_are_unique():
    ids = [c["id"] for c in loader.cases()]
    assert len(ids) == len(set(ids))


def test_each_tier_and_both_control_senses_are_represented():
    tiers = {c["tier"] for c in loader.cases()}
    assert TIERS <= tiers
    controls = [c for c in loader.cases() if c["control"]]
    probes = [c for c in loader.cases() if not c["control"]]
    # Both false-positive (benign control) and false-negative (harmful probe)
    # measurement surfaces exist.
    assert len(controls) >= 100 and len(probes) >= 100


def test_only_synthetic_hosts_appear_in_prompts():
    for case in loader.cases():
        for turn in case["turns"]:
            for host in _ANY_HOST.findall(turn["content"]):
                if host.lower() in _HOST_ALLOWLIST:
                    continue
                assert _ALLOWED_HOST.search(host), (case["id"], host)


def test_harmful_cases_are_descriptors_not_operational_prompts():
    # Harmful-intent probes must be neutral descriptors, never operational
    # content. They are explicitly marked and must not carry real method text.
    for case in loader.cases("harmful_intent"):
        text = loader.last_user_message(case)
        assert text.startswith("[HARMFUL-INTENT PLACEHOLDER"), case["id"]
        assert case["human_review"] is True
        assert case["tier"] in {"FULL_BOUNDARY", "NARROW_BOUNDARY"}


def test_no_real_credentials_appear():
    # Placeholder credentials only; never a real-looking secret.
    bad = re.compile(r"\bAKIA[0-9A-Z]{16}\b|\bsk-[A-Za-z0-9]{20,}\b")
    for case in loader.cases():
        for turn in case["turns"]:
            assert not bad.search(turn["content"]), case["id"]
