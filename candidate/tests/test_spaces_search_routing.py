"""One authoritative Space search-routing contract (implementation review 8).

`kalillac_spaces.search_intent.parse(message).decision` decides whenever it
can: "search", "clarify" or "no_search". Only when it returns None may the
application's independent live-information channels (named-identity
verification, a domain or URL, an explicit request for sources or links)
search -- and they never match command words. The legacy classifier can
never override a parser decision.

Covered here, end to end with the fake provider and as a deterministic
grammar matrix:
  1. ordinary messages that use search vocabulary never search or ask;
  2. targetless imperatives ask for a subject; a capability question is not
     a lookup;
  3. pronoun-headed targets fail closed whatever modifiers follow;
  4. only original Unicode letters are subject evidence;
and that saved history cannot change any decision or provider query.
No real model, search or network."""

from __future__ import annotations

import itertools

import pytest

import app_fastapi_candidate as app
from kalillac_spaces.search_intent import DECISIONS, parse
from test_spaces_chat import (  # noqa: F401  (pytest fixtures)
    api,
    new_conversation,
    new_space,
    provider,
    send,
    world,
)
from test_spaces_history_and_search import HISTORIES, history_with_a_named_person, observe, seed

# --- the required cases ----------------------------------------------------------------------

ORDINARY = [
    "Here is my plan: search for bugs later.",
    "Search-engine tips",
    "Google's market share",
]
TARGETLESS = ["Search", "Search Google", "Search;", "Search Google;", "Please search", "Look up"]
CAPABILITY = ["Can you search the web?"]
PRONOUN_HEADED = [
    "Search for his biography in Tokyo, Japan.",
    "Search for his biography in Tokyo (Japan).",
    "Search for his detailed biography.",
    "Search for his biography from last year.",
    "Search for his complete biography during 2025.",
    "Search for her recent work in 東京.",
]
REFERENT_FIRST = [
    "Search for Beyoncé and where she was born.",
    "Search for Satya Nadella and where he is from.",
    "Search for restaurants in 東京.",
    "Search for Tokyo, Japan.",
    "Search for Google and its current CEO.",
]
NO_LETTER_EVIDENCE = [
    "Search for 1️⃣.",                  # digit + VS16 + combining enclosing keycap
    "Search for ™.",                     # NFKC: "TM"
    "Search for ©.",
    "Search for ™™.",
    "Search for ℠ ㎏ ㏂.",                # more compatibility expansions into letters
    "Search for ?!…",                    # punctuation only
    "Search for 😀 🇯🇵.",                  # emoji only
    "Search for 　  .",    # whitespace only
    "Search for ​.",                # a format character
]
UNICODE_NAMES = [
    "Search for 東京.",
    "Search for 李白.",
    "Search for Мария Кюри.",
    "Search for ابن سينا.",
    "Search for सत्यजित राय.",
    "Search for Beyoncé.",
    "Search for O’Connor.",
]
# Independent live-information channels the product already supports: they
# still search, with the original message, when the parser has no decision.
INDEPENDENT = [
    "Who is Sundar Pichai?",                                # named-identity verification
    "Tell me about groq.com",                               # a domain
    "Summarize https://example.com/news",                   # a URL
    "Give me sources for the Higgs boson discovery.",       # an explicit request for sources
]

CLARIFY = TARGETLESS + PRONOUN_HEADED + NO_LETTER_EVIDENCE
SEARCH = REFERENT_FIRST + UNICODE_NAMES
NOT_SEARCHED = ORDINARY + CAPABILITY

# --- the parser's decision -------------------------------------------------------------------


@pytest.mark.parametrize("message", CLARIFY)
def test_unresolved_lookups_decide_clarify(message):
    for variant in (message, message.upper(), message.lower()):
        assert parse(variant).decision == "clarify", variant
        assert app._space_route_search(variant) == "clarify", variant


@pytest.mark.parametrize("message", SEARCH)
def test_resolved_lookups_decide_search(message):
    for variant in (message, message.upper(), message.lower()):
        assert parse(variant).decision == "search", variant
        assert app._space_route_search(variant) == "search", variant


@pytest.mark.parametrize("message", NOT_SEARCHED)
def test_ordinary_and_capability_messages_never_search(message):
    # "no_search" when search vocabulary is present; "Google's market share"
    # has none, so it is no decision with no independent channel (below).
    expected = None if message == "Google's market share" else "no_search"
    assert parse(message).decision == expected
    assert app._space_route_search(message) is None


def test_google_possessive_is_no_decision_and_no_independent_channel():
    # Not a command and no search vocabulary: the parser has no decision,
    # and no independent channel fires -- the "google" substring is not one.
    assert parse("Google's market share").decision is None
    assert app._space_independent_search("Google's market share") is False


@pytest.mark.parametrize("message", INDEPENDENT)
def test_independent_channels_still_decide_when_the_parser_has_no_decision(message):
    assert parse(message).decision is None
    assert app._space_route_search(message) == "search"


def test_a_pronoun_never_takes_a_later_referent():
    for tail in (" in Tokyo, Japan", " (Japan)", " from last year", " during 2025", ": Satya Nadella",
                 " and Satya Nadella", " in 東京", " — Microsoft", ", the CEO of Microsoft"):
        for head in ("his biography", "his detailed biography", "her recent work", "their history", "its CEO"):
            assert parse(f"Search for {head}{tail}.").decision == "clarify", (head, tail)
    for before in ("Satya Nadella and", "Beyoncé and", "Microsoft and", "東京 and"):
        assert parse(f"Search for {before} his biography in Tokyo.").decision == "search", before


def test_letter_evidence_comes_from_the_original_characters():
    intent = parse("Search for ™.")
    assert intent.target == "tm." and intent.subject == ()       # NFKC letters exist, evidence does not
    assert parse("Search for Ｍｉｃｒｏｓｏｆｔ.").subject == ("microsoft",)   # full-width letters are letters
    assert parse("Search for Beyoncé.").subject == ("beyoncé",)    # composed canonically
    assert parse("Search for ™ Microsoft.").subject == ("microsoft",)
    assert parse("Search for 1️⃣ 東京.").subject == ("東京",)


def test_punctuation_is_grammar_not_normalized_away():
    assert parse("Search;").decision == "clarify"
    assert parse("Search; Microsoft").decision == "clarify"            # an unaccepted separator: fail closed
    assert parse("Search Google; for Satya Nadella.").decision == "clarify"
    assert parse("Search for; Satya Nadella.").decision == "clarify"
    assert parse("Search Google: for Satya Nadella.").decision == "search"
    assert parse("Google.com is down?").decision is None                # a domain dot is not a separator
    assert app._space_route_search("Google.com is down?") == "search"    # the domain channel decides
    assert parse("Can you search the web?").decision == "no_search"
    assert parse("Can you search the web;").decision == "clarify"


# --- the deterministic grammar matrix --------------------------------------------------------

POLITE = ["", "Please ", "Please: ", "Kalillac, please ", "Could you, ", "can you "]
VERBS = [("Search", True), ("Look up", True), ("Google", False)]       # (verb, takes a provider)
PROVIDERS = ["", " Google", " the web", " on Bing", " online"]
BOUNDARIES = [" ", ", ", ": ", " : ", "; ", "—", " — "]
TARGETS = {
    "for Satya Nadella": "search",
    "Satya Nadella": "search",
    "for 東京": "search",
    "for Мария Кюри": "search",
    "for Beyoncé": "search",
    "for his biography in Tokyo, Japan": "clarify",
    "for his detailed biography (2025)": "clarify",
    "her recent work in 東京": "clarify",
    "for ™™": "clarify",
    "for 1️⃣": "clarify",
    "for 😀": "clarify",
}
ENDINGS = ["", ".", "?", ";", ":"]


def matrix():
    for polite, (verb, takes_provider), provider_name in itertools.product(POLITE, VERBS, PROVIDERS):
        if provider_name and not takes_provider:
            continue
        head = f"{polite}{verb}{provider_name}"
        question = polite.strip(" ,").lower() in ("could you", "can you")
        for boundary, (target, expected) in itertools.product(BOUNDARIES, TARGETS.items()):
            if boundary.strip(" ") in (";", "—"):
                expected = "clarify"                              # unaccepted separator: fail closed
            yield f"{head}{boundary}{target}.", expected
        for ending in ENDINGS:                                    # empty targets
            capability = question and ending in ("", ".", "?")
            yield f"{head}{ending}", "no_search" if capability else "clarify"


MATRIX = list(matrix())


def test_the_matrix_is_large_and_covers_every_decision():
    assert len(MATRIX) > 1000
    assert {expected for _, expected in MATRIX} == {"search", "clarify", "no_search"}
    assert set(DECISIONS) == {"search", "clarify", "no_search", None}


def test_the_grammar_matrix():
    wrong = [(message, expected, parse(message).decision) for message, expected in MATRIX
             if parse(message).decision != expected]
    assert wrong == []


def test_the_grammar_matrix_ignores_case():
    wrong = [message for message, expected in MATRIX[::7]
             if parse(message.upper()).decision != expected or parse(message.lower()).decision != expected]
    assert wrong == []


# --- end to end ------------------------------------------------------------------------------

@pytest.mark.parametrize("message", CLARIFY)
def test_an_unresolved_lookup_asks_with_no_provider_model_or_allowance(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    assert observe(world, api, provider, space_id, conversation_id, message) == (True, [], 0, 0)


@pytest.mark.parametrize("message", SEARCH)
def test_a_resolved_lookup_searches_once_with_the_original_message(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, window) == (False, [message], 1)
    assert queries[0].encode("utf-8") == message.encode("utf-8")


@pytest.mark.parametrize("message", NOT_SEARCHED)
def test_an_ordinary_or_capability_message_never_searches_or_asks(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, window) == (False, [], 0)


@pytest.mark.parametrize("message", INDEPENDENT)
def test_an_independent_live_information_request_still_searches(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, len(queries), window) == (False, 1, 1)
    assert queries == [app.build_web_search_query(message, [])]          # never built from history


@pytest.mark.parametrize("message", NOT_SEARCHED + CLARIFY[:3] + SEARCH[:3])
def test_the_legacy_classifier_cannot_override_a_parser_decision(world, api, provider, monkeypatch, message):  # noqa: F811  (pytest fixtures)
    """Every legacy and independent channel is forced to say "search"; the
    parser's decision still stands."""
    monkeypatch.setattr(app, "classify_request", lambda *args, **kwargs: "web_search")
    monkeypatch.setattr(app, "is_web_search_request", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "requires_web_verification", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "_space_independent_search", lambda *args, **kwargs: True)
    space_id = new_space(api, "")
    conversation_id = new_conversation(api, space_id)
    clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
    decision = parse(message).decision
    if decision == "no_search":
        assert (clarified, queries, window) == (False, [], 0)
    elif decision == "clarify":
        assert (clarified, queries, window) == (True, [], 0)
    else:
        assert (clarified, queries, window) == (False, [message], 1)


INVARIANCE = (ORDINARY + CAPABILITY + TARGETLESS[:3] + PRONOUN_HEADED[:3] + NO_LETTER_EVIDENCE[:3]
              + REFERENT_FIRST[:2] + UNICODE_NAMES[:2] + INDEPENDENT[:2]
              + [MATRIX[0][0], MATRIX[len(MATRIX) // 2][0], MATRIX[-1][0]])


@pytest.mark.parametrize("message", INVARIANCE)
def test_history_never_changes_the_decision_or_the_query(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    assert len(HISTORIES) >= 4
    outcomes = {}
    for name, pairs in HISTORIES.items():
        app._SPACE_SEARCH_STATE.clear()
        space_id = new_space(api, "", name=name)
        conversation_id = new_conversation(api, space_id)
        seed(world, conversation_id, pairs)
        clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
        outcomes[name] = (clarified, queries, window)
    assert len(set(map(repr, outcomes.values()))) == 1, outcomes


def test_free_chat_never_consults_the_space_router(provider, monkeypatch):  # noqa: F811  (pytest fixtures)
    def forbidden(message):
        raise AssertionError("Free Chat must not consult the Space search router")
    monkeypatch.setattr(app, "_space_route_search", forbidden)
    assert app._SPACE_REQUEST.get() is None
    for message in ORDINARY + TARGETLESS[:2] + PRONOUN_HEADED[:1] + NO_LETTER_EVIDENCE[:1]:
        app.chat(message, [], session_id="free-chat-routing-probe")
