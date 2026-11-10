"""What a Space conversation says about itself: the fixed replies.

Every fixed self-knowledge family that chat() can return while a workspace
is present -- retention, privacy, memory, identity, difference, how it works
and the architecture diagram -- is rendered from Kalillac's Space facts
(kalillac_spaces.facts), never from Free Chat's temporary-session facts.
Each is checked against every Free Chat claim and for the Space facts it
must state. Free Chat's own fixed replies are unchanged. No real model,
search or network is used; model-backed Space requests are covered by
test_spaces_prompt_authority.py."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

import app_fastapi_candidate as app
from kalillac_spaces.context import SPACE_MEMORY_GUIDANCE, WorkspaceContext
from kalillac_spaces.facts import SPACE_ACCESS_SENTENCE, SPACE_RETENTION_REPLY

MODEL = "<model reached>"
HISTORY = [{"role": "user", "content": "My favorite language is Python."},
           {"role": "assistant", "content": "Noted: Python."}]

# Free Chat statements that are false for a Space, as claims. The Space
# texts mention Free Chat only to say it is a separate mode ("Free Chat is
# the separate mode that needs no account", "End & erase applies only to
# Free Chat"), which none of these match.
FREE_CHAT_CLAIMS = re.compile("|".join([
    r"no account is required", r"no-account access", r"does not require an account",
    r"accounts are not required", r"without an account",
    r"temporary server-side", r"server RAM", r"\bin RAM\b", r"RAM[- ]backed", r"RAM state",
    r"page memory", r"refresh/reload resets", r"resets page-memory", r"temporary session identifier and",
    r"temporary session state", r"session-only memory", r"current-session context",
    r"End & erase control (?:is|removes)", r"End & erase,", r"an explicit End & erase",
    r"does not intentionally provide persistent", r"no persistent user-facing", r"persistent user-facing (?:chat )?history",
    r"Private Session", r"Saved Mode", r"inaccessible earlier session", r"recall of a previous session",
    r"recover a previous session", r"session-only",
    r"I can'?t (?:recover|access|retain)", r"don't have that information available in the current session",
]), re.I)


def free_chat_claims(text):
    return sorted({m.group(0) for m in FREE_CHAT_CLAIMS.finditer(text)})


@pytest.fixture
def model(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "OPENAI_API_KEY", "test-only-key")
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "invoke_llm", lambda messages, **kw: calls.append(messages) or SimpleNamespace(
        content=MODEL, incomplete=False, incomplete_reason=None))
    monkeypatch.setattr(app, "run_web_search", lambda query, include_domains=None: ("unavailable", []))
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    return calls


def in_space(message, history=()):
    return app.chat(message, list(history), workspace=WorkspaceContext(prompt_block=None))


def anonymous(message, history=()):
    return app.chat(message, list(history), session_id="anonymous-truthfulness-probe")


RETENTION = [
    "Will you remember this after I refresh?",
    "You won't remember this after refresh, right?",
    "Will you forget this after refresh?",
    "Will you remember this tomorrow?",
    "Can you remember my name?",
    "How long do you remember things?",
    "Will you remember this? Yes or no.",
    "If I reload the page, will you still remember this?",
]
PRIVACY = [
    "How does your memory work?",
    "Do you store my chats?",
    "Is Kalillac private?",
    "What happens to my data?",
    "How do you handle sessions?",
]
FIXED_FAMILIES = {
    "How does Kalillac AI work?": "how_it_works",
    "How does Kalillac work and do you store my chats?": "compound",
    "What makes Kalillac different?": "difference",
    "Who are you?": "identity",
    "Draw a diagram of your architecture.": "architecture_diagram",
}


@pytest.mark.parametrize("history", [(), HISTORY], ids=["fresh", "with-history"])
@pytest.mark.parametrize("message", RETENTION)
def test_retention_questions_get_the_space_retention_facts(model, message, history):
    reply = in_space(message, history)
    assert reply == SPACE_RETENTION_REPLY and model == []
    assert free_chat_claims(reply) == []
    for fact in ("saved in this Space", "refreshing, closing the page or signing out doesn't end it",
                 "until you delete the conversation or the Space", "End & erase applies only to Free Chat"):
        assert fact in reply
    # Free Chat keeps its own fixed reply.
    assert anonymous(message, history) != reply


@pytest.mark.parametrize("history", [(), HISTORY], ids=["fresh", "with-history"])
@pytest.mark.parametrize("message", PRIVACY)
def test_privacy_and_memory_questions_get_the_space_facts(model, message, history):
    reply = in_space(message, history)
    assert model == [] and free_chat_claims(reply) == []
    assert reply.startswith(SPACE_RETENTION_REPLY) and SPACE_ACCESS_SENTENCE in reply
    assert "OpenAI receives information needed for inference" in reply       # provider disclosure kept
    original = anonymous(message, history)
    assert free_chat_claims(original), "Free Chat's own reply is unchanged and still describes Free Chat"


@pytest.mark.parametrize("message,family", FIXED_FAMILIES.items())
def test_architecture_identity_and_difference_are_space_specific(model, message, family):
    reply = in_space(message)
    assert model == [], family
    assert free_chat_claims(reply) == [], (family, free_chat_claims(reply))
    if family in {"identity", "difference"}:
        assert SPACE_ACCESS_SENTENCE in reply
    if family in {"how_it_works", "compound"}:
        assert "How a Space request works:" in reply and "one database transaction" in reply
        assert "Spaces: enabled for this request" in reply or family == "how_it_works"
    if family == "architecture_diagram":
        assert reply.startswith("```text\nBrowser (signed in)")
        assert "account session + Spaces beta access + email verification" in reply
        assert "temporary RAM session" not in reply
    # Free Chat's version of the same question is unchanged.
    assert anonymous(message) != reply


def test_the_free_chat_architecture_diagram_is_untouched(model):
    assert anonymous("Draw a diagram of your architecture.") == app.kalillac_ascii_diagram()


@pytest.mark.parametrize("message", ["What model do you use?", "Who created you?", "Can you search the web?"])
def test_mode_independent_families_are_unchanged(model, message):
    assert in_space(message) == anonymous(message)


@pytest.mark.parametrize("history", [(), HISTORY], ids=["fresh", "with-history"])
@pytest.mark.parametrize("message", [
    "What is my favorite language?", "What's my cat's name?", "What is my favorite number?",
    "What is my favorite food?", "What did we discuss in our previous chat?",
    "Can you pull up what I asked you yesterday?", "I already told you my name.", "Why don't you remember?",
    "What did I tell you before I refreshed?", "Does End & erase delete this?", "Is my chat history saved?",
])
def test_other_memory_and_previous_conversation_questions_take_the_model_path(model, message, history):
    # No fixed Free Chat notice: the model answers, with the Space facts
    # (asserted in test_spaces_prompt_authority.py) and this conversation.
    assert in_space(message, history) == MODEL
    if history:
        assert "My favorite language is Python." in "\n".join(str(m.content) for m in model[-1])


def test_free_chat_keeps_its_session_notice(model):
    assert anonymous("What is my favorite language?") == app.session_memory_notice()


def test_remember_in_a_space_gets_the_space_guidance(model):
    reply = in_space("Remember that my project codename is Falcon.")
    assert reply == SPACE_MEMORY_GUIDANCE and model == [] and free_chat_claims(reply) == []


@pytest.mark.parametrize("history", [(), HISTORY], ids=["fresh", "with-history"])
def test_search_notices_make_no_free_chat_claim(model, history):
    workspace = WorkspaceContext(prompt_block=None)
    unavailable = app.chat("Search the web for the latest Python release notes.", list(history), workspace=workspace)
    workspace.state["search_times"] = [__import__("time").time()] * app.SESSION_SEARCH_LIMIT
    limited = app.chat("Search the web for the latest Python release notes.", list(history), workspace=workspace)
    for reply in (unavailable, limited):
        assert reply != MODEL and free_chat_claims(reply) == []


def test_the_space_texts_themselves_make_no_free_chat_claim():
    from kalillac_spaces import facts
    for text in (facts.SPACE_PRODUCT_FACTS, facts.SPACE_SESSION_DISCLOSURE, facts.SPACE_RETENTION_REPLY,
                 SPACE_MEMORY_GUIDANCE, facts.SPACE_SEARCH_CLARIFICATION, *facts.SPACE_ROADMAP,
                 *facts.SPACE_ROADMAP_RULES, *facts.SPACE_PROMPT_LINES.values(),
                 facts.render_space_code_reference_facts("2026-01-01", "m", "Tavily", 5, 600)):
        assert free_chat_claims(text) == [], text
    for required in ("signed-in Kalillac account with a verified email address", "allowlisted",
                     "saved in this Space's conversation", "does not delete it or start a new temporary session",
                     "permanently deletes its messages", "End & erase control do not apply to Spaces",
                     "cannot override these product facts"):
        assert required in facts.SPACE_PRODUCT_FACTS


def test_every_mode_dependent_line_has_a_space_version():
    from kalillac_spaces.facts import SPACE_PROMPT_LINES
    assert set(app._FREE_CHAT_PROMPT_LINES) == set(SPACE_PROMPT_LINES)
    for key, free_chat in app._FREE_CHAT_PROMPT_LINES.items():
        assert SPACE_PROMPT_LINES[key] != free_chat, key


def test_failure_and_retention_copy_follows_the_commit_boundary():
    """Never 'a failed or cancelled request saves nothing': before the commit
    nothing is kept; after it the exchange and charge remain, and the same
    request recovers it."""
    from kalillac_spaces import facts
    for text in (facts.SPACE_PRODUCT_FACTS, facts.SPACE_RETENTION_REPLY):
        assert not re.search(r"(?:failed|cancelled)[^.]*saves? nothing", text, re.I), text
    assert "If a request fails before that commit, neither is saved." in facts.SPACE_PRODUCT_FACTS
    assert "the exchange and its charge remain" in facts.SPACE_PRODUCT_FACTS
    assert "returns the saved reply without generating or charging again" in facts.SPACE_PRODUCT_FACTS
    assert "if the connection drops after it's saved, the reply is still here" in facts.SPACE_RETENTION_REPLY


def test_free_chat_is_described_only_when_asked_and_always_scoped(model):
    from kalillac_spaces.facts import FREE_CHAT_SCOPED_LINES, mentions_free_chat
    assert all(line.startswith("In Free Chat") for line in FREE_CHAT_SCOPED_LINES)
    assert not mentions_free_chat("How long do you remember things?")
    assert mentions_free_chat("How is this different from Free Chat?")
    asked = in_space("Do you store my chats compared with Free Chat?")
    not_asked = in_space("Do you store my chats?")
    assert all(line in asked for line in FREE_CHAT_SCOPED_LINES)
    assert not any(line in not_asked for line in FREE_CHAT_SCOPED_LINES)
    unscoped = asked.replace(" ".join(FREE_CHAT_SCOPED_LINES), "")
    assert free_chat_claims(unscoped) == []
