"""Spaces and the source_list signal.

A Space send runs the same /api/chat worker (and so the same request-scoped
source-footer record) but answers through Spaces finalization, which saves
the exchange and replays the stored messages. Stored messages carry no
provenance record, so the Spaces response contract stays exactly as it was:
no source_list anywhere, and the saved reply is the reply, unchanged. Any
future Spaces client therefore shows ordinary Markdown for its lists.

Model, search and network are replaced; no provider is called.
"""
from __future__ import annotations

import pytest

import app_fastapi_candidate as app
from kalillac_routing import source_trust as st
from test_spaces_chat import (  # noqa: F401  (provider, world and api are fixtures)
    api, new_conversation, new_space, provider, send, world,
)


RESULTS = [{"title": "Federal Reserve issues FOMC statement",
            "url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261105a.htm",
            "content": "Result text."}]


def keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(v) for v in value)) if value else set()
    return set()


@pytest.mark.parametrize("ids", [(1,), None], ids=["sources", "search_results"])
def test_a_space_send_never_carries_a_source_list_and_saves_the_reply_unchanged(api, monkeypatch, ids):
    expected = {}

    def fake_chat(message, history, *args, **kwargs):
        reply = app._with_source_footer("A sourced answer.", RESULTS, st.SourceSelection(ids))
        expected["reply"] = reply
        return reply

    monkeypatch.setattr(app, "chat", fake_chat)
    space_id = new_space(api)
    conversation_id = new_conversation(api, space_id)
    response = send(api, space_id, conversation_id, "What did the Fed decide?")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"user_message", "assistant_message", "conversation"}
    assert "source_list" not in keys(body)
    assert body["assistant_message"]["content"] == expected["reply"]
    assert expected["reply"].endswith(st.render_source_footer(RESULTS, st.SourceSelection(ids)))
