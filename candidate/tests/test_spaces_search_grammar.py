"""Space search grammar: colon separators, Unicode targets, fail-closed
command parsing and bounds (kalillac_spaces.search_intent).

Colons are separators at command boundaries only -- after a polite prefix,
after the search verb, after a provider in command position, after a
connector -- with or without spaces around them. Command words, provider
names, polite words and separators never become the subject; a message that
clearly starts as a search command the grammar cannot resolve asks for a
subject. Targets are tokenized over Unicode letters, digits and combining
marks, so names in any script are subjects, while punctuation, symbols and
emoji never are. With a pronoun present, a place or time qualifier ("in
東京") does not resolve it.

End to end: an unresolved message gets the clarification with zero provider
calls, zero model calls and no search allowance; a valid one searches exactly
once with the untouched original message; saved history changes neither.
No real model, search or network."""

from __future__ import annotations

import time
import unicodedata
from pathlib import Path

import pytest

import app_fastapi_candidate as app
from kalillac_spaces import search_intent
from kalillac_spaces.config import USER_MESSAGE_MAX
from kalillac_spaces.facts import SPACE_SEARCH_CLARIFICATION
from kalillac_spaces.search_intent import parse
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

COLON_CLARIFY = [
    "Search Google: for his full biography.",
    "Search Google : for his full biography.",
    "Search: his full biography.",
    "Search : his full biography.",
    "Please: search Google for his full biography.",
    "Please : search Google for his full biography.",
    "Please: search Google: for his full biography.",
    "Could you: search the Web for where she was born?",
    "Find: where he was born.",
    "Look up: her biography.",
]
COLON_SEARCH = [
    "Search: Microsoft history.",
    "Search Google: for Satya Nadella’s biography.",
    "Please: search for Google and its current CEO.",
    "Find: where Microsoft was founded.",
    "Look up: the WebAssembly memory model.",
]
UNICODE_SEARCH = [
    "Search for 東京.",
    "Search for 李白.",
    "Search for Мария Кюри.",
    "Search for ابن سينا.",
    "Search for सत्यजित राय.",
    "Search for Beyoncé.",
    "Search for O’Connor.",
]
UNRESOLVED = [
    "Search for his biography in 東京.",        # 東京 qualifies "his biography"; it is not the subject
    "Search for his biography.",
    "Search Google: for her full profile.",
    "Find where they were born.",
    "Search for ???.",                          # punctuation only
    "Search for !!! ### ...",
    "Search for —/—.",
    "Search for 😀😀.",                          # emoji only
    "Search for 🇯🇵 🎉",
    "Search for.",                              # empty
    "Search:",
    "Search Google:",
]
# Clearly a search command, but the grammar cannot resolve it: fail closed.
FAIL_CLOSED = [
    "Search; his biography.",
    "Search—Google for Satya Nadella.",
    "Please— search Google for his biography.",
    "Please... search for Microsoft history.",
    "Look up; Microsoft.",
    "Google; his biography.",
]
# Not lookups: ordinary messages that merely contain command vocabulary.
NOT_LOOKUPS = [
    "Can you search the web?",
    "Search-engine tips?",
    "Google's market share is large.",
    "Find the bug in my function.",
    "Look at this code.",
    "Here is my plan: search for bugs later.",
]


def colon_variants(message):
    """No space after the colon, spaces on both sides, upper and lower case,
    other sentence endings."""
    base = message.rstrip(".?!")
    tight = base.replace(" : ", ":").replace(": ", ":")
    spaced = tight.replace(":", " : ")
    out = {message, tight, spaced, message.upper(), message.lower(), base, base + "?", base + "!", base + "..."}
    return sorted(out)


def unicode_variants(message):
    base = message.rstrip(".?!")
    return sorted({message, base, base + "?", base + "!", message.upper(), message.lower(),
                   base.replace("Search for", "Search:"), base.replace("Search for", "Search Google: for"),
                   base.replace("Search for", "Please: search for")})


REPEATED_POLITE = [
    ("Please please: search Google: for his full biography.", "clarify"),
    ("Please, please: search for Google and its current CEO.", "search"),
    ("Could you please: search the Web for Satya Nadella’s biography?", "search"),
    ("Kalillac: please: search Google : for where she was born!", "clarify"),
]

# --- the parser ------------------------------------------------------------------------------


@pytest.mark.parametrize("message", COLON_CLARIFY)
def test_colon_commands_without_a_subject_need_one(message):
    for variant in colon_variants(message):
        intent = parse(variant)
        assert intent.explicit_command and intent.needs_subject and intent.subject == (), (variant, intent)


@pytest.mark.parametrize("message", COLON_SEARCH)
def test_colon_commands_with_a_subject_search(message):
    for variant in colon_variants(message):
        intent = parse(variant)
        assert intent.explicit_command and not intent.needs_subject, (variant, intent)


@pytest.mark.parametrize("message,expected", REPEATED_POLITE)
def test_repeated_polite_prefixes_with_colons(message, expected):
    assert parse(message).explicit_command
    assert parse(message).needs_subject is (expected == "clarify")


def test_the_colon_grammar_resolves_each_position():
    for message in ("Search Google: for his full biography.", "Search Google : for his full biography.",
                    "Please: search Google: for his full biography.", "Please : search Google for his full biography."):
        intent = parse(message)
        assert (intent.verb, intent.provider, intent.target) == ("search", "google", "his full biography."), message
    intent = parse("Search: Microsoft history.")
    assert (intent.verb, intent.provider, intent.target, intent.subject) == ("search", None, "microsoft history.", ("microsoft",))
    assert parse("Find: where Microsoft was founded.").subject == ("microsoft",)
    assert parse("Look up: the WebAssembly memory model.").verb == "look up"
    # A provider stays a subject in the target, colon or not.
    assert parse("Please: search for Google and its current CEO.").subject == ("google",)
    assert parse("Search Google: for Google Gemini updates.").subject == ("google", "gemini")


def test_command_vocabulary_is_never_the_subject():
    vocabulary = {"search", "look", "find", "up", "google", "bing", "brave", "web", "internet", "online",
                  "please", "could", "can", "kalillac", ":"}
    for message in COLON_CLARIFY + FAIL_CLOSED + UNRESOLVED:
        for variant in colon_variants(message):
            assert not set(parse(variant).subject) & vocabulary, variant


@pytest.mark.parametrize("message", FAIL_CLOSED)
def test_an_unresolvable_search_command_fails_closed(message):
    for variant in (message, message.upper(), message.lower()):
        intent = parse(variant)
        assert intent.explicit_command and intent.needs_subject and intent.subject == (), (variant, intent)


@pytest.mark.parametrize("message", UNICODE_SEARCH)
def test_unicode_names_are_subjects(message):
    for variant in unicode_variants(message):
        intent = parse(variant)
        assert intent.explicit_command and not intent.needs_subject, (variant, intent)


@pytest.mark.parametrize("message", UNRESOLVED)
def test_unresolved_targets_need_a_subject(message):
    for variant in (message, message.upper(), message.lower()):
        intent = parse(variant)
        assert intent.is_lookup and intent.needs_subject, (variant, intent)


@pytest.mark.parametrize("message", NOT_LOOKUPS)
def test_command_vocabulary_alone_is_not_a_lookup(message):
    assert not parse(message).is_lookup


def test_a_place_qualifier_never_resolves_a_pronoun_in_any_script():
    for place in ("東京", "Tokyo", "Москва", "القاهرة", "दिल्ली", "Zürich"):
        assert parse(f"Search for his biography in {place}.").needs_subject, place
        assert parse(f"Search for where she lived during {place} years.").needs_subject, place
        # Without a pronoun the place is the subject; a named person before
        # the pronoun resolves it; nothing after the pronoun does (review 8).
        assert not parse(f"Search for restaurants in {place}.").needs_subject, place
        assert not parse(f"Search for Satya Nadella and his time in {place}.").needs_subject, place
        assert parse(f"Search for his biography: {place}.").needs_subject, place


TOKENS = {
    "東京": ["東京"],
    "李白": ["李白"],
    "東京の天気": ["東京の天気"],
    "Мария Кюри": ["мария", "кюри"],
    "ابن سينا": ["ابن", "سينا"],
    "सत्यजित राय": ["सत्यजित", "राय"],
    "Beyoncé": ["beyoncé"],
    "Beyoncé": ["beyoncé"],                # decomposed: the combining mark stays in its word
    "O’Connor": ["o'connor"],
    "Nadella’s": ["nadella"],                    # possessive removed
    "it's 2024": ["it", "2024"],
    "?!… —/— ###": [],
    "😀 🇯🇵 🎉 ✨": [],
    "́́": [],                          # marks alone are not a word
    "snake_case": ["snake", "case"],
}


@pytest.mark.parametrize("text", TOKENS)
def test_unicode_tokenization(text):
    assert search_intent._tokens(search_intent.normalize(text)) == TOKENS[text]


def test_numbers_alone_are_never_a_subject():
    for message in ("Search for 2024.", "Search for ２０２４.", "Search for ٢٠٢٤.", "Search for १९४७."):
        assert parse(message).needs_subject, message
    assert not parse("Search for Windows 11.").needs_subject


def test_the_parser_is_bounded():
    assert parse("Search for Microsoft " + "x" * USER_MESSAGE_MAX).needs_subject    # over the bound: fail closed
    worst = [
        "please: " * (USER_MESSAGE_MAX // 8),
        "please " * (USER_MESSAGE_MAX // 7 - 2) + "search",
        "Search for " + "́" * (USER_MESSAGE_MAX - 11),
        "Search for " + "a'" * ((USER_MESSAGE_MAX - 11) // 2),
        "where " + "is he " * ((USER_MESSAGE_MAX - 6) // 6),
        "Search for " + "東京 " * ((USER_MESSAGE_MAX - 11) // 3),
        ": " * (USER_MESSAGE_MAX // 2),
    ]
    for message in worst:
        assert len(message) <= USER_MESSAGE_MAX
        started = time.perf_counter()
        parse(message)
        assert time.perf_counter() - started < 0.5, message[:40]


def test_the_parser_source_has_no_control_characters():
    source = Path(search_intent.__file__).read_bytes()
    bad = [i for i, byte in enumerate(source) if byte < 0x20 and byte not in (0x09, 0x0A, 0x0D)]
    assert bad == []                             # e.g. a "\b" that became a backspace
    assert all(unicodedata.category(ch) != "Cc" or ch in "\t\n\r" for ch in source.decode("utf-8"))


# --- end to end ------------------------------------------------------------------------------

CLARIFY_END_TO_END = COLON_CLARIFY + UNRESOLVED + FAIL_CLOSED + [m for m, e in REPEATED_POLITE if e == "clarify"]
SEARCH_END_TO_END = COLON_SEARCH + UNICODE_SEARCH + [m for m, e in REPEATED_POLITE if e == "search"] + [
    "Search:Microsoft history", "SEARCH GOOGLE :FOR SATYA NADELLA’S BIOGRAPHY!", "search for мария кюри?",
    "Search Google: for 李白", "Please: search for Beyoncé."]


@pytest.mark.parametrize("message", CLARIFY_END_TO_END)
def test_an_unresolved_message_asks_with_no_provider_model_or_allowance(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    assert observe(world, api, provider, space_id, conversation_id, message) == (True, [], 0, 0)


@pytest.mark.parametrize("message", SEARCH_END_TO_END)
def test_a_valid_message_searches_once_with_the_original_text(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, window) == (False, [message], 1)
    assert queries[0].encode("utf-8") == message.encode("utf-8")        # untouched, byte for byte


@pytest.mark.parametrize("message", NOT_LOOKUPS)
def test_command_vocabulary_alone_never_asks(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, _, _, _ = observe(world, api, provider, space_id, conversation_id, message)
    assert clarified is False


INVARIANCE = [
    "Search Google: for his full biography.", "Please: search Google: for his full biography.",
    "Search for his biography in 東京.", "Search for 😀😀.", "Search; his biography.",
    "Search: Microsoft history.", "Find: where Microsoft was founded.", "Search for 東京.",
    "Search for ابن سينا.", "Search for O’Connor.",
]


@pytest.mark.parametrize("message", INVARIANCE)
def test_the_decision_and_query_never_depend_on_history(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    outcomes = {}
    for name, pairs in HISTORIES.items():
        app._SPACE_SEARCH_STATE.clear()
        space_id = new_space(api, "", name=name)
        conversation_id = new_conversation(api, space_id)
        seed(world, conversation_id, pairs)
        clarified, queries, _, _ = observe(world, api, provider, space_id, conversation_id, message)
        outcomes[name] = (clarified, queries)
    assert len(set(map(repr, outcomes.values()))) == 1, outcomes


def test_free_chat_never_uses_the_space_parser(provider, monkeypatch):  # noqa: F811  (pytest fixtures)
    def forbidden(message):
        raise AssertionError("Free Chat must not consult the Space search parser")
    monkeypatch.setattr(search_intent, "parse", forbidden)
    history = [{"role": "user", "content": "Who is Satya Nadella?"},
               {"role": "assistant", "content": "Satya Nadella is the CEO of Microsoft."}]
    assert app._SPACE_REQUEST.get() is None
    for message in ("Search Google: for his full biography.", "Search for 東京.", "Search; his biography."):
        reply = app.chat(message, history, session_id="free-chat-grammar-probe")
        assert reply != SPACE_SEARCH_CLARIFICATION
