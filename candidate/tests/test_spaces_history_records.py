"""Space history as saved records, selected by identity or position.

Every model-bound consumer of a Space's history -- normal and follow-up
context, code continuation, code revision, code history and comparison,
previous-answer revision, search-assisted answers -- selects saved records
by sequence or position and sends each one framed byte for byte with its
sequence (kalillac_spaces.context.frame_saved_message). A normalized or
notice-stripped copy is only ever a separately marked derived frame; no
record is ever found again by searching for its normalized text.

The route matrix proves it from the model request itself: every framed
record recovered from the request is exactly the stored record with that
sequence, and once the framed records are removed no saved content is left
anywhere -- so nothing reached the model through a normalized lookup. No
real model, search or network."""

from __future__ import annotations

import re

import pytest

import app_fastapi_candidate as app
from kalillac_spaces.context import (
    WorkspaceContext,
    frame_saved_message,
    recover_saved_records,
)
from test_spaces_chat import (  # noqa: F401  (pytest fixtures)
    api,
    new_conversation,
    new_space,
    provider,
    send,
    world,
)
from test_spaces_history_and_search import model_text, seed

# Two code answers that are identical after trimming (normalize_history_content)
# but differ in indentation, trailing spaces, CRLF/LF and blank lines.
CODE_A = "```python\ndef area(r):\n    return 3.14 * r * r\n```"
CODE_B = "\r\n\r\n    " + CODE_A + "   \r\n\r\n\n"
# Two that are identical once each line is trimmed and line endings unified.
CODE_C = "```python\ndef area(r):\n    return 3.14 * r * r\n\n```\n"
CODE_D = "```python\r\ndef area(r):   \r\n    return 3.14 * r * r\t\r\n\r\n```\r\n"


def records(*pairs):
    """A Space history window: records with seq 1, 2, ... in order."""
    out = []
    for user_text, reply_text in pairs:
        out.append({"id": f"id-{len(out) + 1}", "seq": len(out) + 1, "role": "user", "content": user_text})
        out.append({"id": f"id-{len(out) + 1}", "seq": len(out) + 1, "role": "assistant", "content": reply_text})
    return out


def space_prompt(message, history, route):
    token = app._SPACE_REQUEST.set(WorkspaceContext(prompt_block=None))
    try:
        return "\n".join(m.content for m in app.build_messages(message, history, route, []))
    finally:
        app._SPACE_REQUEST.reset(token)


_DERIVED = re.compile(r"DERIVED ANALYSIS[^\n]*\n<(space_derived_[0-9a-f]{32})[^>]*>\n.*?\n</\1>", re.DOTALL)


def selected(text, history):
    """The records framed in `text`, each checked against the stored record
    with that sequence; and the text left once every frame is removed."""
    by_seq = {record["seq"]: record for record in history}
    found = recover_saved_records(text)
    residue = text
    for seq, role, content in found:
        assert seq in by_seq, seq
        assert (role, content) == (by_seq[seq]["role"], by_seq[seq]["content"])   # identity, byte for byte
        residue = residue.replace(frame_saved_message(role, content, seq), "", 1)
    residue = _DERIVED.sub("", residue)
    return [seq for seq, _, _ in found], residue


def test_identical_after_trimming_is_the_premise():
    assert app.normalize_history_content(CODE_A) == app.normalize_history_content(CODE_B)
    assert CODE_A != CODE_B
    unify = lambda s: "\n".join(line.strip() for line in s.replace("\r\n", "\n").strip().split("\n"))  # noqa: E731
    assert unify(CODE_C) == unify(CODE_D) and CODE_C != CODE_D
    for code in (CODE_A, CODE_B, CODE_C, CODE_D):
        assert app.previous_answer_looks_like_code(app.normalize_history_content(code))


@pytest.mark.parametrize("first,second", [(CODE_A, CODE_B), (CODE_B, CODE_A), (CODE_C, CODE_D)])
@pytest.mark.parametrize("message", ["Compare the two versions of the code above.",
                                     "Revise both versions of the code above."])
def test_colliding_code_answers_are_two_records_each_sent_once(first, second, message):
    history = records(("Write an area function.", first), ("Write it again.", second))
    text = space_prompt(message, history, "code_history")
    found = recover_saved_records(text)
    assert found == [(2, "assistant", first), (4, "assistant", second)]      # by sequence, each exactly once
    tags = re.findall(r"<(space_message_[0-9a-f]{32}) seq=", text)
    assert len(tags) == 2 and len(set(tags)) == 2                            # two distinct identities


@pytest.mark.parametrize("first,second", [(CODE_A, CODE_B), (CODE_B, CODE_A), (CODE_C, CODE_D)])
def test_revising_after_a_collision_selects_the_latest_code_record(first, second):
    history = records(("Write an area function.", first), ("Write it again.", second))
    assert recover_saved_records(space_prompt("Make it faster.", history, "revision")) == [(4, "assistant", second)]
    # The last answer is prose: the latest code record is chosen by position.
    history = records(("Write an area function.", first), ("Write it again.", second), ("Thanks!", "  You're welcome.  "))
    assert recover_saved_records(space_prompt("Make the code faster.", history, "revision")) == [(4, "assistant", second)]


def test_continuation_sends_the_raw_records_and_a_marked_derived_copy():
    cut = app.mark_incomplete_reply("```python\ndef area(r):\n    return 3.14 *  ")
    assert app.has_incomplete_notice(cut)
    history = records(("  Write an area function with tabs\t\n", "Sure.  \n"), ("\nNow write area for squares too.\r\n", cut))
    text = space_prompt("continue", history, "code_continuation")
    seqs, residue = selected(text, history)
    assert seqs == [1, 3, 4]                                  # recent user tasks, then the cut-off answer
    derived = re.findall(r'<space_derived_[0-9a-f]{32} from_seq="(\d+)" kind="([a-z_]+)" chars="(\d+)">\n', text)
    assert derived == [("4", "incomplete_notice_removed", str(len(app.strip_incomplete_notice(cut))))]
    assert app.strip_incomplete_notice(cut) in text
    assert app.INCOMPLETE_RESPONSE_NOTICE not in residue      # the notice only inside the raw record


def test_continuation_without_a_notice_has_no_derived_copy():
    history = records(("Write an area function.", CODE_B))
    text = space_prompt("continue", history, "code_continuation")
    assert recover_saved_records(text) == [(1, "user", "Write an area function."), (2, "assistant", CODE_B)]
    assert "space_derived_" not in text


def test_free_chat_continuation_is_unchanged():
    cut = app.mark_incomplete_reply("```python\ndef area(r):\n    return 3.14 *  ")
    history = [{"role": "user", "content": "Write an area function."}, {"role": "assistant", "content": cut}]
    assert app._SPACE_REQUEST.get() is None
    text = "\n".join(m.content for m in app.build_messages("continue", history, "code_continuation", []))
    assert app.strip_incomplete_notice(app.normalize_history_content(cut)) in text
    assert "space_message_" not in text and "space_derived_" not in text


def test_no_content_based_identity_helper_remains():
    assert not hasattr(app, "_model_history_text")
    source = open(app.__file__, encoding="utf-8").read()
    assert "_model_history_text" not in source


# --- the route matrix ------------------------------------------------------------------------

def marked_history():
    """Six records, each with a unique marker and whitespace that a
    normalization would change."""
    return records(
        ("  MARK_U1 Write an area function.\t\n", "\n```python\ndef area(r):  # MARK_A2\n    return r * r\n```\r\n\r\n"),
        ("MARK_U3 Explain it?  ", "  MARK_A4 It squares r.\r\n"),
        ("\tMARK_U5 Now improve it.\n", "```python\r\ndef area(r):  # MARK_A6\r\n    return r ** 2   \r\n```\n\n"),
    )


MATRIX = [
    ("general", "Explain how encryption uses keys."),
    ("followup", "Why?"),
    ("unclear", "Hmm."),
    ("personal", "How do I stay motivated?"),
    ("logic", "If all cats are animals, are some animals cats?"),
    ("self_knowledge", "What model do you use?"),
    ("code", "Add type hints to it."),
    ("code_continuation", "continue"),
    ("code_history", "Compare the two versions of the code above."),
    ("revision", "Make the code faster."),
    ("memory", "What did I ask earlier?"),
]


@pytest.mark.parametrize("route,message", MATRIX)
def test_every_route_selects_records_and_never_a_normalized_copy(route, message):
    history = marked_history()
    text = space_prompt(message, history, route)
    seqs, residue = selected(text, history)
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))   # conversation order, each record once
    leaked = re.findall(r"MARK_[UA]\d", residue)
    assert leaked == [], (route, leaked)                            # no saved content outside its record frame


EXPECTED_SELECTION = {
    "code_continuation": [1, 3, 5, 6],                  # recent user tasks, then the last answer
    "code_history": [2, 6],                             # the code answers, by sequence
    "revision": [6],                                    # the latest code answer
}


@pytest.mark.parametrize("route", sorted(EXPECTED_SELECTION))
def test_record_selection_by_sequence(route):
    message = dict(MATRIX)[route]
    seqs, _ = selected(space_prompt(message, marked_history(), route), marked_history())
    assert seqs == EXPECTED_SELECTION[route]


@pytest.mark.parametrize("route", ["followup", "unclear", "logic", "personal", "self_knowledge", "memory"])
def test_context_routes_frame_a_window_of_records(route):
    seqs, _ = selected(space_prompt(dict(MATRIX)[route], marked_history(), route), marked_history())
    assert seqs and seqs == list(range(seqs[0], 7))     # the last N records, contiguous and in order


# --- end to end: stored identity reaches the model --------------------------------------------

def stored(world, conversation_id):  # noqa: F811  (pytest fixtures)
    import uuid
    from kalillac_db.models import Message
    with world.factory() as session:
        rows = session.query(Message).filter(Message.conversation_id == uuid.UUID(conversation_id)).all()
        return {row.seq: (row.role, row.content) for row in rows}


@pytest.mark.parametrize("message", ["Compare the two versions of the code above.",
                                     "What changed between the two versions of the code above?"])
def test_a_space_comparison_sends_both_colliding_records(world, api, provider, message):  # noqa: F811  (pytest fixtures)
    space_id = new_space(api, "")
    conversation_id = new_conversation(api, space_id)
    seed(world, conversation_id, [("Write an area function.", CODE_A), ("Write it again.", CODE_B)])
    assert send(api, space_id, conversation_id, message).status_code == 200
    found = recover_saved_records(model_text(provider.payloads[-1]))
    db = stored(world, conversation_id)
    assert [(seq, role, content) for seq, role, content in found if role == "assistant"] == [
        (2, "assistant", CODE_A), (4, "assistant", CODE_B)]
    assert all(db[seq] == (role, content) for seq, role, content in found)


def test_a_space_revision_sends_the_latest_record(world, api, provider):  # noqa: F811  (pytest fixtures)
    space_id = new_space(api, "")
    conversation_id = new_conversation(api, space_id)
    seed(world, conversation_id, [("Write an area function.", CODE_B), ("Write it again.", CODE_A)])
    assert send(api, space_id, conversation_id, "Make the code faster.").status_code == 200
    found = recover_saved_records(model_text(provider.payloads[-1]))
    assert (4, "assistant", CODE_A) in found and (2, "assistant", CODE_B) not in found
    assert all(stored(world, conversation_id)[seq] == (role, content) for seq, role, content in found)


def test_a_space_search_answer_frames_its_context_records(world, api, provider, monkeypatch):  # noqa: F811  (pytest fixtures)
    results = [{"title": "WebAssembly memory", "url": "https://example.test/wasm", "published": "",
                "content": "Linear memory is a contiguous byte array."}]

    def search(query, include_domains=None):
        provider.queries.append(query)
        return "ok", results
    monkeypatch.setattr(app, "run_web_search", search)
    space_id = new_space(api, "")
    conversation_id = new_conversation(api, space_id)
    history = marked_history()
    seed(world, conversation_id, [(history[i]["content"], history[i + 1]["content"]) for i in range(0, 6, 2)])
    message = "Search for the WebAssembly memory model."
    assert send(api, space_id, conversation_id, message).status_code == 200
    assert provider.queries == [message]
    text = model_text(provider.payloads[-1])
    seqs, residue = selected(text, history)
    assert seqs == [3, 4, 5, 6]                          # the last four records, by sequence
    assert re.findall(r"MARK_[UA]\d", residue) == []
    assert "Linear memory is a contiguous byte array." in text
