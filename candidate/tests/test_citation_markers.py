"""Model citation markers never reach users.

The model can emit its own Private-Use-Area citation syntax (observed in
production: "...lists it as a starting point. \\ue200cite\\ue202turn0search0\\ue201").
Kalillac's sources are only the application-owned footer, so clean_ai_reply
removes these markers from prose -- before answer-link enforcement -- and never
turns them into links. Code keeps them exactly as written.

Deterministic: providers are scripted, the model is a stub, no socket leaves
the machine.
"""
from __future__ import annotations

import re

import pytest

import app_fastapi_candidate as app
from kalillac_routing import source_trust as st
from test_source_list_contract import RESULTS, link_targets, run_recorded
from test_source_trust_behavior import (  # noqa: F401  (world is a fixture)
    LAKESHIRE, answer_turn, brave, footer, reply_with, resource_results, search_turn, world,
)

OPEN, SEP, CLOSE = "", "", ""
MARKER = f"{OPEN}cite{SEP}turn0search0{CLOSE}"
MARKER_RANGE = re.compile(r"[-]")

# The production answer, exactly as the model wrote it.
OBSERVED = ("Python’s official beginner tutorial is the The Python Tutorial. It introduces "
            "Python’s basic concepts and language features. The official documentation lists "
            "it as a starting point. " + MARKER)
OBSERVED_CLEAN = ("Python’s official beginner tutorial is the The Python Tutorial. It introduces "
                  "Python’s basic concepts and language features. The official documentation lists "
                  "it as a starting point.")


def test_the_observed_production_answer_loses_only_the_marker():
    assert st.strip_citation_markers(OBSERVED) == OBSERVED_CLEAN
    assert app.clean_ai_reply(OBSERVED) == OBSERVED_CLEAN


@pytest.mark.parametrize("text, expected", [
    (f"Claim {MARKER}.", "Claim."),
    (f"Claim{MARKER}, more.", "Claim, more."),
    (f"First {MARKER} and second {OPEN}cite{SEP}turn0search1{SEP}turn0news2{CLOSE}.", "First and second."),
    (f"{MARKER} Starts a line.", "Starts a line."),
    (f"- item {MARKER}\n- next {MARKER}", "- item\n- next"),
    (f"Ends a line {MARKER}\nNext line.", "Ends a line\nNext line."),
    (f"Two {MARKER}{MARKER} glued.", "Two glued."),
    (f"{OPEN}filecite{SEP}turn0file0{CLOSE} Other kinds go too.", "Other kinds go too."),
    (f"Unclosed {OPEN}cite{SEP}turn0search0 then text.", "Unclosed then text."),
    (f"Unclosed at the end {OPEN}cite{SEP}turn0search0", "Unclosed at the end"),
    (f"Stray {CLOSE}close and {SEP} separator.", "Stray close and separator."),
    ("Highlight kept words stays.", "Highlight kept words stays."),
    (f"A **bold claim** {MARKER} and a [link](https://a.example/) {MARKER}.",
     "A **bold claim** and a [link](https://a.example/)."),
], ids=["before_period", "before_comma", "two_markers", "line_start", "list_items", "line_end", "glued",
        "other_kind", "unclosed", "unclosed_at_end", "stray_characters", "highlight_span", "markdown_kept"])
def test_markers_and_malformed_markers_are_removed_from_prose(text, expected):
    cleaned = st.strip_citation_markers(text)
    assert cleaned == expected
    assert not MARKER_RANGE.search(cleaned)
    assert "turn0" not in cleaned


@pytest.mark.parametrize("text", [
    f"Use the literal marker:\n\n```text\n{MARKER}\n```\n",
    f"~~~python\nprint('{MARKER}')\n~~~",
    f"Inline `{MARKER}` example.",
    f"Double ``code with ` and {MARKER}`` span.",
    f"```text\nunclosed fence keeps {MARKER}",
])
def test_code_keeps_markers_exactly(text):
    assert st.strip_citation_markers(text) == text


def test_prose_around_code_is_cleaned_and_code_is_not():
    text = f"Before {MARKER}.\n\n```text\nliteral {MARKER}\n```\n\nAfter `{MARKER}` and {MARKER}."
    assert st.strip_citation_markers(text) == (
        f"Before.\n\n```text\nliteral {MARKER}\n```\n\nAfter `{MARKER}` and.")


@pytest.mark.parametrize("text", [
    "Plain answer with **bold**, _em_, `code` and [a link](https://a.example/x_(y)).",
    "| a | b |\n|---|---|\n| 1 | 2 |",
    "Math $$x^2$$ and \\(y\\) stay.",
    "Other private-use characters stay:  , and emoji too: \U0001F600.",
    "Real words: cite, turn0search0 and kalillac-sources appear as ordinary text.",
    "",
])
def test_text_without_markers_is_unchanged(text):
    assert st.strip_citation_markers(text) == text


def test_a_reply_made_only_of_markers_cleans_to_empty():
    assert app.clean_ai_reply(f"  {MARKER} {MARKER}\n") == ""


def test_removing_a_marker_cannot_create_an_unauthorized_link():
    # "www" + marker + ".evil.example" is not a link; joined, it would be one.
    body = f"See www{MARKER}.evil.example and https://evil{MARKER}.example/x."
    cleaned = app.clean_ai_reply(body)
    guarded = st.enforce_answer_links(cleaned, RESULTS, st.SourceSelection((1,)))
    assert link_targets(guarded) == []
    assert "evil" in guarded and not MARKER_RANGE.search(guarded)


def test_provider_titles_cannot_carry_markers_into_the_footer():
    rows = [{"title": f"Python docs {MARKER}", "url": "https://docs.python.org/3/tutorial/", "content": ""}]
    text, metadata = st.source_footer(rows, st.SourceSelection((1,)))
    assert not MARKER_RANGE.search(text) and "Python docs" in text
    assert metadata == {"kind": "sources", "count": 1}


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_both_paths_return_clean_answers_with_the_signed_footer(world, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    rows = resource_results(LAKESHIRE)
    answer = f"Pantries are listed {MARKER}. Example: `{MARKER}`."
    world["script"] = lambda provider, payload: brave(rows)
    world["replies"] = [reply_with(answer, "1")]
    world["turns"] = [search_turn("food help Lakeshire", "s1"), answer_turn(answer, "1")]
    reply, metadata = run_recorded("Search the web for food and housing help resources in Lakeshire")
    assert reply == f"Pantries are listed. Example: `{MARKER}`." + footer("Sources", [rows[0]])
    assert metadata == {"kind": "sources", "count": 1}


def test_a_non_search_native_answer_is_cleaned_too(world, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    world["turns"] = [lambda items, n: answer_turn(f"A binary search halves the range {MARKER}.")(items, n)]
    assert app.chat("Explain what a binary search does.", [], session_id="markers") == \
        "A binary search halves the range."
