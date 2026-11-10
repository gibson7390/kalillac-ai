"""The live-information channel of Space search routing (implementation review 9).

`_space_route_search` stays the only authority: an explicit parser decision
("search", "clarify", "no_search") is final; only when the parser has no
decision may the independent channels run -- now including
kalillac_spaces.live_intent: a real question or request, with a subject,
whose answer is time-sensitive or likely to have changed. No command word
ever establishes live-information intent, and freshness words in prose,
quotations, code, writing requests or questions about language never do.

Proved end to end with the fake provider (exact provider calls, model calls
where the clarification is expected, allowance effect, original query
bytes, independence from four saved histories), and by a deterministic
matrix whose expected results come from an oracle written here, not from
the production function. No real model, search or network."""

from __future__ import annotations

import itertools

import pytest

import app_fastapi_candidate as app
from kalillac_spaces import live_intent
from kalillac_spaces.live_intent import live_information
from kalillac_spaces.search_intent import parse
from test_spaces_chat import (  # noqa: F401  (pytest fixtures)
    api,
    new_conversation,
    new_space,
    provider,
    send,
    world,
)
from test_spaces_history_and_search import HISTORIES, history_with_a_named_person, observe, search_window, seed

POSITIVE = [
    "What is Google's stock price today?",
    "What is Google's current market share?",
    "What's the weather in Terre Haute right now?",
    "What is the latest Colts score?",
    "When is the next Indiana Pacers game?",
    "What is MGK's newest song?",
    "Who is the current president of France?",
    "Is GitHub down right now?",
    "What is the current USD to EUR exchange rate?",
    "What are today's headlines about OpenAI?",
    "What is the latest stable Python release?",
]
NEGATIVE = [
    "Google's market share",
    "Explain what a stock price is.",
    "Write a paragraph containing the word “today”.",
    "In my story, today was rainy.",
    "Is “latest” an adjective?",
    "Compare the words current and recent.",
    "Search-engine tips",
    "Here is my plan: search for bugs later.",
    'What does {"date": "today", "build": "latest"} mean for my parser?',
    "Why does `datetime.today()` return the current time?",
    'print("the latest price today")',
    "Can you search the web?",
]
# Further negatives: past questions, programming objects, conceptual and
# private questions, a freshness word with no subject.
NEGATIVE_MORE = [
    "What was the weather in Paris in 1990?",
    "What is the current directory in Python?",
    "How do I fix my current bug?",
    "What's the latest?",
    "Today I learned about Python.",
    "Describe the latest trends in poetry.",
]

# --- the channel and the router -------------------------------------------------------------


@pytest.mark.parametrize("message", POSITIVE)
def test_a_time_sensitive_question_is_live_information(message):
    assert parse(message).decision is None                  # the parser has no decision ...
    assert live_information(message) == "subject"           # ... so the live channel decides
    assert app._space_route_search(message) == "search"


@pytest.mark.parametrize("message", NEGATIVE + NEGATIVE_MORE)
def test_freshness_words_outside_a_live_request_never_search(message):
    assert live_information(message) is None or app._space_route_search(message) is None
    assert app._space_route_search(message) is None


def test_a_pronoun_only_live_question_asks_instead_of_searching():
    for message in ("What is his current salary?", "Where is she playing tonight?", "What's their latest album?"):
        assert live_information(message) == "pronoun", message
        assert app._space_route_search(message) == "clarify", message


def test_command_words_never_establish_live_intent():
    for word in ("search", "google", "look up", "web", "bing", "online"):
        assert live_information(f"What is {word}?") is None, word
        assert live_information(f"{word.title()}?") is None, word
    assert app._space_route_search("Google's market share") is None
    assert app._space_independent_search("Google's market share") is False


def test_the_parser_decision_outranks_every_independent_channel(monkeypatch):
    monkeypatch.setattr(live_intent, "live_information", lambda message: "subject")
    monkeypatch.setattr(app, "requires_web_verification", lambda *args, **kwargs: True)
    monkeypatch.setattr(app, "extract_domains", lambda *args, **kwargs: ["example.com"])
    monkeypatch.setattr(app, "is_incidental_domain_reference", lambda *args, **kwargs: False)
    for message in ("Here is my plan: search for the latest bugs today?", "Search-engine tips for today?",
                    "Can you search the web?"):
        assert parse(message).decision == "no_search", message
        assert app._space_route_search(message) is None, message
    for message in ("Search for his current salary.", "Search", "Search for ™."):
        assert parse(message).decision == "clarify", message
        assert app._space_route_search(message) == "clarify", message
    assert app._space_route_search("Search for Google's stock price today.") == "search"


# --- the deterministic matrix ---------------------------------------------------------------

SUBJECTS = ["Google", "the Indiana Pacers", "東京", "Terre Haute", "Мария Кюри", "Beyoncé"]
# noun phrase template -> does it ask for something time-sensitive?
NOUN_PHRASES = {
    "the current stock price of {s}": True,
    "{s}'s headlines today": True,
    "the weather in {s} right now": True,
    "the latest news about {s}": True,
    "the newest release from {s}": True,
    "the next game for {s}": True,
    "the live score for {s}": True,
    "the most recent results for {s}": True,
    "the founder of {s}": False,
    "the population of {s}": False,
}
# structure -> is it a real question or request?
STRUCTURES = {
    "What is {np}?": True,
    "Tell me {np}.": True,
    "Can you tell me {np}?": True,
    "Please tell me {np}.": True,
    "Do you know {np}?": True,
    "{NP}?": True,
    "I read about {np} yesterday.": False,
    "In my story, {np} changed everything.": False,
    "Write a short poem about {np}.": False,
    "Is “{np}” a good title?": False,
    'print("{np}")': False,
    '{{"query": "{np}"}}': False,
    "Is the phrase “{np}” grammatical?": False,
}


def oracle(structure_is_request, phrase_is_fresh):
    return structure_is_request and phrase_is_fresh


def matrix():
    for subject, (phrase, fresh), (structure, request) in itertools.product(
            SUBJECTS, NOUN_PHRASES.items(), STRUCTURES.items()):
        np = phrase.format(s=subject)
        message = structure.format(np=np, NP=np[0].upper() + np[1:])
        yield message, oracle(request, fresh)


MATRIX = list(matrix())


def test_the_matrix_covers_both_outcomes():
    assert len(MATRIX) == len(SUBJECTS) * len(NOUN_PHRASES) * len(STRUCTURES) == 780
    assert {expected for _, expected in MATRIX} == {True, False}


def test_the_live_information_matrix():
    wrong = [(message, expected) for message, expected in MATRIX
             if (live_information(message) == "subject") is not expected]
    assert wrong == []


def test_the_live_matrix_through_the_router():
    # Every expected live request searches; nothing in a non-request
    # context or without freshness reaches the live channel. (Other
    # independent channels -- verification, domains -- are not involved:
    # none of these messages names an identity to verify or a domain.)
    wrong = [message for message, expected in MATRIX
             if expected and app._space_route_search(message) != "search"]
    wrong += [message for message, expected in MATRIX
              if not expected and parse(message).decision is None and app._space_route_search(message) == "search"]
    assert wrong == []


def test_the_live_matrix_ignores_case():
    wrong = [message for message, expected in MATRIX[::5]
             if (live_information(message.upper()) == "subject") is not expected
             or (live_information(message.lower()) == "subject") is not expected]
    assert wrong == []


# --- end to end ------------------------------------------------------------------------------

@pytest.mark.parametrize("message", POSITIVE)
def test_a_live_question_searches_once_with_the_original_message(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, window) == (False, [message], 1)
    assert queries[0].encode("utf-8") == message.encode("utf-8")


@pytest.mark.parametrize("message", NEGATIVE + NEGATIVE_MORE)
def test_a_negative_control_never_searches_or_asks(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, window) == (False, [], 0)


@pytest.mark.parametrize("message", ["What is his current salary?", "What's their latest album?"])
def test_a_pronoun_only_live_question_asks_with_no_provider_model_or_allowance(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    assert observe(world, api, provider, space_id, conversation_id, message) == (True, [], 0, 0)


@pytest.mark.parametrize("message", POSITIVE + NEGATIVE[:4] + ["What is his current salary?"])
def test_history_never_changes_the_live_decision_or_query(world, api, provider, message):  # noqa: F811  (pytest fixtures)
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


def test_the_live_channel_is_bounded():
    import time
    from kalillac_spaces.config import USER_MESSAGE_MAX
    worst = [
        "What is the " + "latest " * ((USER_MESSAGE_MAX - 13) // 7) + "?",
        "Is " + "x " * ((USER_MESSAGE_MAX - 9) // 2) + "down?",
        "“" * (USER_MESSAGE_MAX // 2) + "today?",
        "What is " + "a(" * ((USER_MESSAGE_MAX - 8) // 2),
        "please " * (USER_MESSAGE_MAX // 7),
    ]
    for message in worst:
        assert len(message) <= USER_MESSAGE_MAX
        started = time.perf_counter()
        live_information(message)
        assert time.perf_counter() - started < 0.5, message[:30]
    assert live_information("What is the latest news about Google? " + "x" * USER_MESSAGE_MAX) is None


# --- review 10: present-relative periods and sports scores ----------------------------------

PERIOD_POSITIVE = [
    "How are the Colts doing this season?",
    "How are the Indiana Pacers doing this season?",
    "What have the Colts done so far this season?",
    "Who is leading the NBA standings this season?",
    "What has changed in Python this year?",
    "What are the major OpenAI announcements this month?",
]
SCORE_POSITIVE = [
    "What is the latest Colts score?",
    "What was the Pacers score tonight?",
    "What is the live score of the game?",
]
SCORE_NEGATIVE = [
    "What is the score of Beethoven's Ninth?",
    "What is a musical score?",
    "Explain the score in this composition.",
]
PERIOD_NEGATIVE = [
    "What season comes after winter?",
    "Explain the phrase “this season.”",
    "Write a story about this season.",
    "Is “this week” a noun phrase?",                        # quoted text containing "this week"
    'Why does {"period": "this year"} fail to parse?',       # JSON containing "this year"
    "Why does `report(this_year)` fail?",                    # code containing "this year"
    "How did the Colts do during the 2020 season?",          # a past period, not present-relative
    "What was the score of the 1990 World Cup final?",
    "Google's market share",
    "Search-engine tips",
    "Can you search the web?",
]
REVIEW_10_POSITIVE = PERIOD_POSITIVE + SCORE_POSITIVE
REVIEW_10_NEGATIVE = SCORE_NEGATIVE + PERIOD_NEGATIVE


@pytest.mark.parametrize("message", REVIEW_10_POSITIVE)
def test_a_present_period_or_sports_score_question_is_live(message):
    assert parse(message).decision is None
    assert live_information(message) == "subject"
    assert app._space_route_search(message) == "search"


@pytest.mark.parametrize("message", REVIEW_10_NEGATIVE)
def test_a_musical_score_or_a_period_outside_a_live_request_never_searches(message):
    assert app._space_route_search(message) is None


def test_score_needs_freshness_or_sports_context():
    assert live_information("What is the score of Beethoven's Ninth?") is None
    assert live_information("What is the score of the Pacers game?") == "subject"     # sports context
    assert live_information("What is the score of Beethoven's Ninth today?") == "subject"   # freshness
    assert live_information("What was the score of the 1990 World Cup final?") is None    # past


PERIODS = ["this season", "so far this season", "this week", "this month", "this year", "year to date",
           "tonight", "tomorrow"]
PERIOD_STRUCTURES = {
    "How are the Colts doing {p}?": True,
    "What has changed in Python {p}?": True,
    "Tell me what OpenAI announced {p}.": True,
    "In my story, the Colts won {p}.": False,
    "Write a story about the Colts {p}.": False,
    "Is “the Colts {p}” a good title?": False,
    'print("the Colts {p}")': False,
    "Explain the phrase “{p}”.": False,
}
PERIOD_MATRIX = [(structure.format(p=period), request)
                 for period, (structure, request) in itertools.product(PERIODS, PERIOD_STRUCTURES.items())]


def test_the_present_period_matrix():
    assert len(PERIOD_MATRIX) == 64 and {e for _, e in PERIOD_MATRIX} == {True, False}
    wrong = [(message, expected) for message, expected in PERIOD_MATRIX
             if (live_information(message) == "subject") is not expected]
    assert wrong == []


@pytest.mark.parametrize("message", REVIEW_10_POSITIVE)
def test_a_review_10_live_question_searches_once_with_the_original_message(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, window) == (False, [message], 1)
    assert queries[0].encode("utf-8") == message.encode("utf-8")


@pytest.mark.parametrize("message", REVIEW_10_NEGATIVE)
def test_a_review_10_negative_never_searches_or_asks(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, window) == (False, [], 0)


@pytest.mark.parametrize("message", PERIOD_POSITIVE[:3] + SCORE_POSITIVE[1:2] + SCORE_NEGATIVE[:1] + PERIOD_NEGATIVE[:1])
def test_history_never_changes_a_review_10_decision(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    outcomes = {}
    for name, pairs in HISTORIES.items():
        app._SPACE_SEARCH_STATE.clear()
        space_id = new_space(api, "", name=name)
        conversation_id = new_conversation(api, space_id)
        seed(world, conversation_id, pairs)
        outcomes[name] = observe(world, api, provider, space_id, conversation_id, message)[1:4:2]
    assert len(HISTORIES) >= 4 and len(set(map(repr, outcomes.values()))) == 1, outcomes


def test_a_question_about_the_assistant_is_not_live_information():
    # Found by test_spaces_truthfulness in review 10: "tomorrow" must not turn
    # a question about Kalillac itself into a web search.
    for message in ("Will you remember this tomorrow?", "What do you think about the latest iPhone?",
                    "Are you down right now?", "Will your answers change this year?"):
        assert live_information(message) is None, message
        assert app._space_route_search(message) is None, message
    for message in ("Can you tell me the current stock price of Google?", "Do you know the latest Colts score?"):
        assert app._space_route_search(message) == "search", message


# --- review 11: self-reference after the request prefix, structural language
# questions, explicit past dates ------------------------------------------------------------

from datetime import date  # noqa: E402

from kalillac_spaces.facts import SPACE_RETENTION_REPLY  # noqa: E402

SEARCH_UNAVAILABLE = "Live web search is temporarily unavailable"
SPACE_SAVING_FACT = ("Each completed exchange (the user's message and Kalillac's reply) is saved in this Space's "
                     "conversation in Kalillac's database")
# message -> the established Space path it takes: the fixed retention reply
# (app.is_memory_retention_question, shared with Free Chat and unchanged), or
# the model with the trusted Space facts, which state how the Space saves.
SELF_REFERENCE_CASES = {
    "Can you tell me whether you will remember this tomorrow?": "retention reply",
    "Do you know whether your memory lasts until tomorrow?": "retention reply",
    "Could you tell me what you'll remember next week?": "model with Space facts",
    "Can you explain whether you retain this conversation tomorrow?": "retention reply",
}
PREFIXED_LIVE = [
    "Can you tell me tomorrow's weather in Terre Haute?",
    "Do you know the latest Colts score?",
    "Could you tell me the current USD to EUR exchange rate?",
]
PRODUCT_NAMES = [
    "What is the latest version of Microsoft Word?",
    "What is the current version of Microsoft Word?",
    "What is the newest Microsoft Word release?",
    "What is the latest version of the C language standard?",
]
LANGUAGE_QUESTIONS = [
    "Is “latest” an adjective?",
    "What does the word “current” mean?",
    "Define the word “latest”.",
    "How do you pronounce “current”?",
    "Compare the words current and recent.",
]
PAST_DATES = [
    "What is the final score of the 1990 World Cup final?",
    "What is the score of the 1990 World Cup final?",
    "What was the stock price on January 3, 1990?",
    "What was the weather in Paris on July 14, 1980?",
]
DATED_BUT_CURRENT = [
    "What was the Pacers score tonight?",
    "What is the latest Colts score?",
    "What is Google's stock price today?",
    "What is the latest update for Windows Server 2022?",
]
REVIEW_11_SEARCH = PREFIXED_LIVE + PRODUCT_NAMES + DATED_BUT_CURRENT
REVIEW_11_NO_SEARCH = LANGUAGE_QUESTIONS + PAST_DATES


def test_self_reference_is_checked_after_the_request_prefix():
    assert app.is_memory_retention_question("Could you tell me what you'll remember next week?") is False
    for message, path in SELF_REFERENCE_CASES.items():
        assert live_information(message) is None, message
        assert app._space_route_search(message) is None, message
        assert app.is_memory_retention_question(message) is (path == "retention reply"), message
    for message in PREFIXED_LIVE:
        assert live_information(message) == "subject", message


@pytest.mark.parametrize("message,path", SELF_REFERENCE_CASES.items())
def test_a_self_referential_request_takes_the_space_retention_path(world, api, provider, message, path):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    queries, payloads, window = len(provider.queries), len(provider.payloads), search_window(world.users["a"])
    response = send(api, space_id, conversation_id, message)
    assert response.status_code == 200
    reply = response.json()["assistant_message"]["content"]
    assert provider.queries[queries:] == []                       # zero provider calls
    assert search_window(world.users["a"]) == window              # zero allowance
    assert SEARCH_UNAVAILABLE not in reply and "can't reliably verify" not in reply
    if path == "retention reply":
        assert reply == SPACE_RETENTION_REPLY and provider.payloads[payloads:] == []     # fixed, no model call
    else:
        assert len(provider.payloads) - payloads == 1                                    # the model answers ...
        facts = provider.payloads[-1]["input"][1]["content"]
        assert SPACE_SAVING_FACT in facts                                                # ... from the Space facts


@pytest.mark.parametrize("message", REVIEW_11_SEARCH)
def test_a_review_11_live_question_searches_once_with_the_original_message(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, calls, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, calls, window) == (False, [message], 0, 1)
    assert queries[0].encode("utf-8") == message.encode("utf-8")


@pytest.mark.parametrize("message", REVIEW_11_NO_SEARCH)
def test_a_review_11_negative_never_searches_or_asks(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, calls, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, calls, window) == (False, [], 1, 0)          # the model answers normally


def test_a_product_name_is_never_suppressed_by_a_language_token():
    for name in ("Microsoft Word", "WordPress", "Grammarly", "the C language standard", "Language Server Protocol",
                 "Words With Friends", "Spelling Bee", "PhraseExpress", "Term Sheet Pro", "Noun Project"):
        for template in ("What is the latest version of {n}?", "What is the newest {n} release?",
                         "Is {n} down right now?"):
            message = template.format(n=name)
            assert live_information(message) == "subject", message
            assert app._space_route_search(message) == "search", message


def test_a_language_question_is_recognized_by_structure():
    for message in LANGUAGE_QUESTIONS + [
            "What is the meaning of “current” today?", "Is “today” a noun?", "How is “latest” spelled?",
            "What are synonyms for current?", "What is the plural of latest?", "Is current a verb?"]:
        assert live_information(message) is None, message


def test_an_explicit_past_date_blocks_only_the_changing_data_category():
    today = date(2026, 10, 30)
    for message in PAST_DATES + ["What is the price of Office 2019?", "What is the score of the 2025 NBA finals?",
                                 "What was the exchange rate on 2026-01-02?", "What is the weather on 3 March 2026?"]:
        assert live_information(message, today) is None, message
    # The present year or a future date is not the past; explicit freshness always wins.
    for message in ["What is the score of the 2026 NBA finals?", "What is the weather forecast for December 1, 2026?",
                    "What is the latest update for Windows Server 2022?", "What is Office 2019's price today?",
                    "How are the Colts doing this season?"]:
        assert live_information(message, today) == "subject", message
    assert live_information("What is the score of the 2026 NBA finals?", date(2027, 1, 1)) is None


def test_parser_commands_and_source_requests_keep_their_precedence_over_past_dates():
    assert app._space_route_search("Search for the score of the 1990 World Cup final.") == "search"
    assert app._space_route_search("Give me sources for the score of the 1990 World Cup final.") == "search"
    assert app._space_route_search("What is the score of the 1990 World Cup final?") is None


def test_the_review_11_rules_are_bounded():
    import time
    from kalillac_spaces.config import USER_MESSAGE_MAX
    worst = [
        "What does " + "x " * ((USER_MESSAGE_MAX - 16) // 2) + "mean?",
        "Is " + "x " * ((USER_MESSAGE_MAX - 12) // 2) + "a noun?",
        "What is the score on " + "january 3, " * ((USER_MESSAGE_MAX - 30) // 11) + "?",
        "What is the score " + "1990 " * ((USER_MESSAGE_MAX - 20) // 5) + "?",
        "Can you tell me " + "you " * ((USER_MESSAGE_MAX - 17) // 4),
    ]
    for message in worst:
        assert len(message) <= USER_MESSAGE_MAX
        started = time.perf_counter()
        live_information(message)
        assert time.perf_counter() - started < 0.5, message[:30]


@pytest.mark.parametrize("message", list(SELF_REFERENCE_CASES)[:2] + PREFIXED_LIVE[:1] + PRODUCT_NAMES[:1] + PAST_DATES[:1])
def test_history_never_changes_a_review_11_decision(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    outcomes = {}
    for name, pairs in HISTORIES.items():
        app._SPACE_SEARCH_STATE.clear()
        space_id = new_space(api, "", name=name)
        conversation_id = new_conversation(api, space_id)
        seed(world, conversation_id, pairs)
        outcomes[name] = observe(world, api, provider, space_id, conversation_id, message)[1:4:2]
    assert len(HISTORIES) >= 4 and len(set(map(repr, outcomes.values()))) == 1, outcomes


# --- review 12: the language exclusion needs a complete grammatical predicate ----------------

PRODUCT_STATUS = [
    "Is the Noun Project down right now?",
    "Is the Noun Project offline today?",
    "Are the Noun Project servers down right now?",
    "Is Microsoft Word down right now?",
    "Is the Spelling Bee app offline today?",
    "Is LanguageTool down right now?",
    "Is Verb Energy currently operating?",
]
# Article-free variants where grammatical.
PRODUCT_STATUS_VARIANTS = [
    "Is Noun Project down right now?",
    "Are Noun Project servers down right now?",
    "Is Spelling Bee offline today?",
    "Is the Word web app down right now?",
    "Is the LanguageTool server down right now?",
    "Is the Verb Energy plant currently operating?",
]
GRAMMAR_QUESTIONS = [
    "Is “run” a noun?",
    "Is the word “run” a noun?",
    "Is “latest” an adjective?",
    "Is “quickly” an adverb?",
    "What does the word “current” mean?",
    "How do you pronounce “current”?",
    "How do you spell “necessary”?",
    "Compare the words current and recent.",
]


@pytest.mark.parametrize("message", PRODUCT_STATUS + PRODUCT_STATUS_VARIANTS)
def test_a_product_status_question_is_never_a_language_question(message):
    assert live_information(message) == "subject"
    assert app._space_route_search(message) == "search"


@pytest.mark.parametrize("message", GRAMMAR_QUESTIONS)
def test_a_complete_grammatical_question_is_not_live(message):
    assert live_information(message) is None
    assert app._space_route_search(message) is None


# The collision matrix: products whose names contain a language token, with
# current-status predicates, must all be live; quoted terms with complete
# grammatical predicates must all be excluded. Expected values come from the
# labels here, not from the production function.
COLLISION_SUBJECTS = ["Microsoft Word", "the Word web app", "the Noun Project", "Noun Project", "Verb Energy",
                      "the Spelling Bee app", "LanguageTool", "the Language Server Protocol", "Grammarly",
                      "the Grammar Book site"]
STATUS_PREDICATES = [
    "Is {s} down right now?",
    "Is {s} offline today?",
    "What is the current version of {s}?",
    "What is the latest release of {s}?",
    "Is {s} currently operating?",
]
GRAMMAR_TERMS = ["run", "latest", "current", "quickly", "this week", "Noun", "Word", "Spelling"]
GRAMMAR_PREDICATES = [
    "Is “{t}” a noun?",
    "Is the word “{t}” a verb?",
    "Is “{t}” an adjective or a noun?",
    "Is “{t}” an adverb today?",
    "What does the word “{t}” mean?",
    "What does “{t}” mean right now?",
    "How do you pronounce “{t}”?",
    "How do you spell “{t}” today?",
    "How is “{t}” spelled?",
    "What is the meaning of “{t}” today?",
    "What are synonyms for “{t}” today?",
]
COLLISION_MATRIX = (
    [(predicate.format(s=subject), True) for subject in COLLISION_SUBJECTS for predicate in STATUS_PREDICATES]
    + [(predicate.format(t=term), False) for term in GRAMMAR_TERMS for predicate in GRAMMAR_PREDICATES])


def test_the_collision_matrix():
    assert len(COLLISION_MATRIX) == 10 * 5 + 8 * 11 == 138
    wrong = [(message, expected) for message, expected in COLLISION_MATRIX
             if (live_information(message) == "subject") is not expected]
    assert wrong == []


def test_the_collision_matrix_through_the_router():
    wrong = [message for message, expected in COLLISION_MATRIX
             if (app._space_route_search(message) == "search") is not expected]
    assert wrong == []


def test_a_subject_token_alone_never_activates_the_language_exclusion():
    for token in ("Word", "Noun", "Verb", "Adjective", "Spelling", "Pronunciation", "Language", "Grammar",
                  "Meaning", "Definition", "Synonym", "Phrase", "Term"):
        for template in ("Is {t} Labs down right now?", "What is the latest release of {t} Studio?",
                         "Is the {t} app offline today?"):
            message = template.format(t=token)
            assert live_information(message) == "subject", message


@pytest.mark.parametrize("message", PRODUCT_STATUS)
def test_a_product_status_question_searches_once_with_the_original_message(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, calls, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, calls, window) == (False, [message], 0, 1)
    assert queries[0].encode("utf-8") == message.encode("utf-8")


@pytest.mark.parametrize("message", GRAMMAR_QUESTIONS)
def test_a_grammatical_question_never_searches(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, window) == (False, [], 0)


@pytest.mark.parametrize("message", PRODUCT_STATUS[:2] + GRAMMAR_QUESTIONS[:2])
def test_history_never_changes_a_review_12_decision(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    outcomes = {}
    for name, pairs in HISTORIES.items():
        app._SPACE_SEARCH_STATE.clear()
        space_id = new_space(api, "", name=name)
        conversation_id = new_conversation(api, space_id)
        seed(world, conversation_id, pairs)
        outcomes[name] = observe(world, api, provider, space_id, conversation_id, message)[1:4:2]
    assert len(HISTORIES) >= 4 and len(set(map(repr, outcomes.values()))) == 1, outcomes


def test_the_grammatical_predicates_are_bounded():
    import time
    from kalillac_spaces.config import USER_MESSAGE_MAX
    worst = [
        "Is " + "x " * ((USER_MESSAGE_MAX - 12) // 2) + "a noun?",
        "Is “" + "x " * ((USER_MESSAGE_MAX - 14) // 2) + "” a noun?",
        "What does " + "“" * (USER_MESSAGE_MAX - 16) + " mean?",
        "How is " + "x " * ((USER_MESSAGE_MAX - 16) // 2) + "spelled?",
        "Compare the words " + "x " * ((USER_MESSAGE_MAX - 20) // 2),
    ]
    for message in worst:
        assert len(message) <= USER_MESSAGE_MAX
        started = time.perf_counter()
        live_information(message)
        assert time.perf_counter() - started < 0.5, message[:30]


# --- review 13: the language exclusion is a whole-message decision ---------------------------

from kalillac_spaces.live_intent import _analysis, _is_language_question  # noqa: E402

LIVE_STATUS_13 = [
    "Are the Word web and mobile apps down right now?",
    "Is the Word web or mobile app offline today?",
    "Is Microsoft Word down right now? The error says “the word ‘current’ is invalid.”",
    "Is the Noun Project down right now?",
    "Are the Noun Project servers down right now?",
    "Is the Spelling Bee web and mobile service offline today?",
    "Is LanguageTool down right now? The error mentions “pronounce”.",
    "Is Verb Energy currently operating?",
    "Are the Grammar web and desktop apps down right now?",
]
LANGUAGE_13 = [
    "Is “run” a noun?",
    "Is the word “run” a noun?",
    "Is “latest” an adjective?",
    "Is “quickly” an adverb today?",
    "Are “current” and “recent” adjectives?",
    "What does the word “current” mean?",
    "What is the meaning of “current”?",
    "How do you pronounce “current”?",
    "How do you spell “necessary”?",
    "How is “necessary” spelled?",
    "Compare the words current and recent.",
]


def whole(message):
    return _is_language_question(_analysis(message)[0])


@pytest.mark.parametrize("message", LANGUAGE_13)
def test_a_complete_language_question_is_excluded(message):
    assert whole(message)
    assert live_information(message) is None
    assert app._space_route_search(message) is None


@pytest.mark.parametrize("message", LIVE_STATUS_13)
def test_the_main_status_request_wins_over_embedded_language(message):
    assert not whole(message)
    assert live_information(message) == "subject"
    assert app._space_route_search(message) == "search"


def test_main_intent_versus_embedded_content():
    # A complete language question is excluded ...
    assert whole("Is the word “run” a noun?")
    # ... but the same fragment inside a status question, a coordinated
    # subject, an error quotation or a second sentence is not.
    for message in ("Is the “is “run” a noun?” quiz site down right now?",
                    "Are the Word and Noun apps down right now?",
                    "Is Word down right now? It says “Is “run” a noun?” on the screen.",
                    "Is the Grammar app offline today? I wanted to ask how you spell “necessary”.",
                    "Is the Verb Energy site down? Also, is “run” a noun?"):
        assert not whole(message), message
    # The status predicate is evaluated as the main request.
    for predicate in ("down right now", "offline today", "currently operating"):
        assert live_information(f"Is the Word noun-and-verb app {predicate}?") == "subject"
    for predicate in ("the current version", "the latest release"):
        assert live_information(f"What is {predicate} of the Word, Noun and Verb apps?") == "subject"


def test_a_bounded_qualifier_never_absorbs_a_status_predicate():
    # The old "up to three trailing words" wildcard is gone: only the listed
    # qualifiers may follow a part of speech.
    for message in ("Is the noun down right now?", "Is “run” a noun app offline today?",
                    "Is the word app a verb today now?", "Is Word a noun site currently operating?"):
        assert not whole(message), message
    for message in ("Is “run” a noun today?", "Is “run” a noun right now?", "Is “run” a noun in English?",
                    "Is “run” a verb in British English?", "Is “run” a noun in this sentence?"):
        assert whole(message), message


# The collision matrix. Expected values are labels written here, not values
# computed by the classifier: every status request is live; every complete
# grammar predicate is excluded.
TOKENS_13 = ["Word", "Noun", "Verb", "Spelling", "Language", "Grammar"]
SUBJECT_FORMS = [
    "the {t} app",                                  # singular
    "{t}",                                          # bare name
    "the {t} web and mobile apps",                  # coordinated, and
    "the {t} web or mobile app",                    # coordinated, or
    "the {t} desktop and web services",
    "the {t} mobile or desktop servers",
]
STATUS_FORMS = [
    "Is {s} down right now?",
    "Is {s} offline today?",
    "What is the current version of {s}?",
    "What is the latest release of {s}?",
    "Is {s} currently operating?",
]
ERROR_SNIPPETS = [
    " The error says “the word ‘current’ is invalid.”",
    " The error mentions “pronounce”.",
    " It shows “Is ‘run’ a noun?”",
    " The banner reads “define latest”.",
]
GRAMMAR_FORMS_13 = [
    "Is “{t}” a noun?",
    "Is the word “{t}” a verb?",
    "Are “{t}” and “current” adjectives?",
    "Is “{t}” an adverb today?",
    "What does the word “{t}” mean?",
    "What is the meaning of “{t}”?",
    "How do you pronounce “{t}”?",
    "How do you spell “{t}” right now?",
    "How is “{t}” spelled?",
    "Compare the words {t} and latest.",
]
COLLISION_13 = (
    [(status.format(s=subject.format(t=token)), True)
     for token in TOKENS_13 for subject in SUBJECT_FORMS for status in STATUS_FORMS]
    + [(f"Is {subject.format(t=token)} down right now?{snippet}", True)
       for token in TOKENS_13 for subject in SUBJECT_FORMS[:3] for snippet in ERROR_SNIPPETS]
    + [(grammar.format(t=token.lower()), False) for token in TOKENS_13 for grammar in GRAMMAR_FORMS_13]
)


def test_the_whole_message_collision_matrix():
    assert len(COLLISION_13) == 6 * 6 * 5 + 6 * 3 * 4 + 6 * 10 == 312
    wrong = [(message, expected) for message, expected in COLLISION_13
             if (live_information(message) == "subject") is not expected]
    assert wrong == []


def test_the_whole_message_collision_matrix_through_the_router():
    wrong = [message for message, expected in COLLISION_13
             if (app._space_route_search(message) == "search") is not expected]
    assert wrong == []


def test_only_grammar_rows_are_whole_message_language_questions():
    wrong = [message for message, expected in COLLISION_13 if whole(message) is expected]
    assert wrong == []                          # live rows never match; grammar rows always do


@pytest.mark.parametrize("message", LIVE_STATUS_13)
def test_a_review_13_status_question_searches_once_with_the_original_message(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, calls, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, calls, window) == (False, [message], 0, 1)
    assert queries[0].encode("utf-8") == message.encode("utf-8")   # quotation marks and vocabulary unchanged


@pytest.mark.parametrize("message", LANGUAGE_13)
def test_a_review_13_language_question_never_searches(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, window) == (False, [], 0)


@pytest.mark.parametrize("message", LIVE_STATUS_13[:3] + LANGUAGE_13[:2])
def test_history_never_changes_a_review_13_decision(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    outcomes = {}
    for name, pairs in HISTORIES.items():
        app._SPACE_SEARCH_STATE.clear()
        space_id = new_space(api, "", name=name)
        conversation_id = new_conversation(api, space_id)
        seed(world, conversation_id, pairs)
        outcomes[name] = observe(world, api, provider, space_id, conversation_id, message)[1:4:2]
    assert len(HISTORIES) >= 4 and len(set(map(repr, outcomes.values()))) == 1, outcomes


def test_the_whole_message_classifier_is_bounded():
    import time
    from kalillac_spaces.config import USER_MESSAGE_MAX
    worst = [
        "Is " + "“x” and " * ((USER_MESSAGE_MAX - 12) // 8) + "a noun?",
        "Is “" + "x" * (USER_MESSAGE_MAX - 13) + "” a noun?",
        "Is " + "x " * ((USER_MESSAGE_MAX - 12) // 2) + "a noun?",
        "Compare the words " + "x and " * ((USER_MESSAGE_MAX - 20) // 6),
        "What does " + "“" * (USER_MESSAGE_MAX - 16) + " mean?",
    ]
    for message in worst:
        assert len(message) <= USER_MESSAGE_MAX
        started = time.perf_counter()
        live_information(message)
        assert time.perf_counter() - started < 0.5, message[:30]


# --- review 14: apostrophes inside quoted terms -------------------------------------------------

from kalillac_spaces.live_intent import _language_copy  # noqa: E402

APOSTROPHE_LANGUAGE = [
    "Is “isn’t” a verb right now?",
    "Is \"isn't\" a verb right now?",
    "Is «isn’t» a verb right now?",
    "Is “today’s” an adjective right now?",
    "Is “O’Connor” a noun right now?",
    "Is 'O'Connor' a noun right now?",
    "Is ‘isn’t’ a verb right now?",
    "Are “isn’t” and “wasn’t” verbs right now?",
    "Is “rock ’n’ roll” a noun in English?",
]
APOSTROPHE_LIVE = [
    "Is O’Connor’s Pub down right now?",
    "Is Microsoft Word down right now? The error says “isn’t valid.”",
    "Are the Word web and mobile apps down right now?",
]


def lang(message):
    return _is_language_question(_language_copy(message))


@pytest.mark.parametrize("message", APOSTROPHE_LANGUAGE)
def test_a_quoted_term_with_an_apostrophe_is_a_language_question(message):
    assert lang(message)
    assert live_information(message) is None
    assert app._space_route_search(message) is None


@pytest.mark.parametrize("message", APOSTROPHE_LIVE + LIVE_STATUS_13 + PRODUCT_STATUS)
def test_a_status_request_with_apostrophes_still_searches(message):
    assert not lang(message)
    assert app._space_route_search(message) == "search"


# The quote matrix. Expected values are labels written here.
QUOTE_PAIRS = [("“", "”"), ('"', '"'), ("«", "»"), ("‘", "’"), ("'", "'")]
SINGLE_PAIRS = {("‘", "’"), ("'", "'")}
APOSTROPHE_TERMS = ["isn’t", "isn't", "wasn’t", "today’s", "it’s", "O’Connor", "O'Connor", "don't", "New York",
                    "rock ’n’ roll"]
GRAMMAR_FORMS_14 = [
    "Is {q} a noun right now?",
    "Is {q} a verb?",
    "Is the word {q} an adjective in English?",
    "Is {q} a noun in this sentence?",
    "What does {q} mean today?",
    "How do you spell {q}?",
    "How is {q} pronounced?",
]


def quote_rows():
    rows = []
    for (opening, closing), term in itertools.product(QUOTE_PAIRS, APOSTROPHE_TERMS):
        if (opening, closing) in SINGLE_PAIRS and "’ " in term:
            continue        # documented: an apostrophe before a space closes a single-quoted term
        quoted = f"{opening}{term}{closing}"
        rows += [(form.format(q=quoted), True) for form in GRAMMAR_FORMS_14]
    # Coordinated terms, same pair.
    for opening, closing in QUOTE_PAIRS:
        rows.append((f"Are {opening}isn’t{closing} and {opening}wasn’t{closing} verbs right now?", True))
        rows.append((f"Are {opening}O’Connor{closing}, {opening}today’s{closing} and {opening}it’s{closing} "
                     f"nouns in English?", True))
    # Bare words with apostrophes where the predicate is unambiguous.
    for term in ("isn't", "isn’t", "O’Connor", "today’s"):
        rows += [(f"Is {term} a verb right now?", True), (f"How do you spell {term}?", True)]
    # Unmatched or crossed delimiters never form a term.
    for (opening, _), (_, closing) in itertools.product(QUOTE_PAIRS, QUOTE_PAIRS):
        if (opening, closing) not in QUOTE_PAIRS:
            rows.append((f"Is {opening}isn’t{closing} a verb right now?", False))
    for opening, closing in QUOTE_PAIRS:
        rows.append((f"Is {opening}isn’t a verb right now?", False))
        rows.append((f"Is isn’t{closing} a verb right now?", False))
    return rows


QUOTE_ROWS = quote_rows()
# Embedded quoted grammar fragments inside product-status questions: live.
EMBEDDED_ROWS = [
    (f"Is {product} down right now? It says {opening}isn’t{closing} is a verb.", product)
    for product in ("O’Connor’s Pub", "Microsoft Word", "the Noun Project", "the Word web and mobile apps")
    for opening, closing in QUOTE_PAIRS
]


def test_the_quote_matrix():
    language_rows = [row for row in QUOTE_ROWS if row[1]]
    # (5 pairs x 10 terms - 2 documented single-quote skips) x 7 forms + 10 coordinated + 8 bare
    # = 354 language rows; 20 crossed + 10 unmatched = 30 non-language rows.
    assert len(QUOTE_ROWS) == 384 and len(language_rows) == 354
    wrong = [(message, expected) for message, expected in QUOTE_ROWS if lang(message) is not expected]
    assert wrong == []


def test_the_quote_matrix_language_rows_never_search():
    wrong = [message for message, expected in QUOTE_ROWS if expected and app._space_route_search(message) is not None]
    assert wrong == []


def test_embedded_quoted_fragments_never_veto_a_status_question():
    assert len(EMBEDDED_ROWS) == 20
    wrong = [message for message, _ in EMBEDDED_ROWS
             if lang(message) or app._space_route_search(message) != "search"]
    assert wrong == []


def test_the_language_copy_keeps_the_typed_quotes():
    assert _language_copy("Please: Is ‘isn’t’ a verb?") == "is ‘isn’t’ a verb?"
    assert _language_copy("Is \"O'Connor\" a noun?") == "is \"o'connor\" a noun?"


@pytest.mark.parametrize("message", APOSTROPHE_LANGUAGE)
def test_an_apostrophe_language_question_makes_no_provider_call(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, _, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, window) == (False, [], 0)


@pytest.mark.parametrize("message", APOSTROPHE_LIVE)
def test_an_apostrophe_status_question_searches_once_with_the_original_message(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id, conversation_id = history_with_a_named_person(api, provider)
    clarified, queries, calls, window = observe(world, api, provider, space_id, conversation_id, message)
    assert (clarified, queries, calls, window) == (False, [message], 0, 1)
    assert queries[0].encode("utf-8") == message.encode("utf-8")


@pytest.mark.parametrize("message", APOSTROPHE_LANGUAGE[:2] + APOSTROPHE_LIVE[:2])
def test_history_never_changes_a_review_14_decision(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    outcomes = {}
    for name, pairs in HISTORIES.items():
        app._SPACE_SEARCH_STATE.clear()
        space_id = new_space(api, "", name=name)
        conversation_id = new_conversation(api, space_id)
        seed(world, conversation_id, pairs)
        outcomes[name] = observe(world, api, provider, space_id, conversation_id, message)[1:4:2]
    assert len(HISTORIES) >= 4 and len(set(map(repr, outcomes.values()))) == 1, outcomes


def test_the_quoted_term_parser_is_bounded():
    import time
    from kalillac_spaces.config import USER_MESSAGE_MAX
    worst = [
        "Is ‘" + "a’" * ((USER_MESSAGE_MAX - 16) // 2) + " a verb?",
        "Is '" + "a'" * ((USER_MESSAGE_MAX - 16) // 2) + " a verb?",
        "Is " + "a’" * ((USER_MESSAGE_MAX - 12) // 2) + "a noun?",
        "Is " + "‘a’ and " * ((USER_MESSAGE_MAX - 12) // 8) + "a noun?",
        "Is “" + "’" * (USER_MESSAGE_MAX - 12) + " a noun?",
    ]
    for message in worst:
        assert len(message) <= USER_MESSAGE_MAX
        started = time.perf_counter()
        live_information(message)
        assert time.perf_counter() - started < 0.5, message[:30]
