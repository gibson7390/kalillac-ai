"""End-to-end: any provider refusal part replaces the complete visible output
with the fixed Kalillac reply, through chat(), on both generation paths, with
no network and no second provider request. Partial model text and tool calls
never survive a refusal, and no source footer is attached.
"""
from __future__ import annotations

import socket

import pytest

import app_fastapi_candidate as app
from kalillac_routing import provider_refusal

# A clearly recognizable synthetic string. It must never reach the user when a
# refusal part is present.
PARTIAL = "Partial unsafe content the model started."
DECLINE = "The provider declined this request."


def _msg(parts):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": parts}]}


def _ot(text):
    return {"type": "output_text", "text": text}


def _rf(text=DECLINE):
    return {"type": "refusal", "refusal": text}


# Every mixed shape the brief enumerates: text before, after, both sides,
# multiple text and refusal parts, refusal-only, and multiple refusals.
MIXED_SHAPES = {
    "text_before_refusal": [_ot(PARTIAL), _rf()],
    "refusal_before_text": [_rf(), _ot(PARTIAL)],
    "text_both_sides": [_ot(PARTIAL), _rf(), _ot(PARTIAL + " 2")],
    "multiple_text_and_refusals": [_ot(PARTIAL), _rf("no1"), _ot(PARTIAL + " 2"), _rf("no2")],
    "refusal_only": [_rf()],
    "multiple_refusals": [_rf("no1"), _rf("no2")],
}


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "_chat_semaphore", None)
    monkeypatch.setattr(socket.socket, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))


@pytest.mark.parametrize("shape", sorted(MIXED_SHAPES))
def test_legacy_refusal_replaces_all_visible_text(no_network, monkeypatch, shape):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    calls = {"n": 0}

    def one(payload, **kw):
        calls["n"] += 1
        return _msg(MIXED_SHAPES[shape])

    monkeypatch.setattr(app, "_post_openai_responses", one)
    reply = app.chat("Explain the topic.", [], session_id=f"legacy-{shape}")
    assert reply == provider_refusal.REFUSAL_RESPONSE
    assert PARTIAL not in reply
    assert DECLINE not in reply  # raw refusal text never copied
    assert calls["n"] == 1  # no continuation, no second provider request


@pytest.mark.parametrize("shape", sorted(MIXED_SHAPES))
def test_native_refusal_replaces_all_visible_text(no_network, monkeypatch, shape):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    calls = {"n": 0}

    def one_native(input_items, instructions):
        calls["n"] += 1
        return _msg(MIXED_SHAPES[shape])

    monkeypatch.setattr(app, "_invoke_openai_native_tools", one_native)
    reply = app.chat("Tell me about tide pools.", [], session_id=f"native-{shape}")
    assert reply == provider_refusal.REFUSAL_RESPONSE
    assert PARTIAL not in reply and DECLINE not in reply
    assert calls["n"] == 1


def test_continuation_partial_then_refusal_is_discarded(no_network, monkeypatch):
    # A cut-off turn accumulates partial text, then the continuation refuses.
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    seq = [
        {"status": "incomplete", "incomplete_details": {"reason": app.OUTPUT_TOKEN_LIMIT_REASON},
         "output": [{"type": "message", "role": "assistant", "content": [_ot(PARTIAL)]}]},
        _msg([_ot(PARTIAL + " continued"), _rf()]),
    ]
    calls = {"n": 0}

    def scripted(payload, **kw):
        calls["n"] += 1
        return seq.pop(0)

    monkeypatch.setattr(app, "_post_openai_responses", scripted)
    reply = app.chat("Explain at length.", [], session_id="legacy-cont")
    assert reply == provider_refusal.REFUSAL_RESPONSE
    assert PARTIAL not in reply
    assert calls["n"] == 2  # one continuation was attempted, then refused


def test_native_tool_call_beside_refusal_runs_no_tool(no_network, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    ran = []

    def one_native(input_items, instructions):
        # A function_call item beside a refusal in the same model turn.
        return {"status": "completed", "output": [
            {"type": "message", "role": "assistant", "content": [_ot(PARTIAL), _rf()]},
            {"type": "function_call", "name": "search_web", "call_id": "c1", "arguments": {"query": "x"}},
        ]}

    monkeypatch.setattr(app, "_invoke_openai_native_tools", one_native)
    monkeypatch.setattr(app, "run_web_search",
                        lambda *a, **k: ran.append("search") or ("ok", []))
    reply = app.chat("Search the web for something.", [], session_id="native-tool-refuse")
    assert reply == provider_refusal.REFUSAL_RESPONSE
    assert ran == []  # no tool executed
    assert PARTIAL not in reply


def test_web_search_route_refusal_has_no_source_footer(no_network, monkeypatch):
    # A legacy web-search turn that refuses must not carry a **Sources** footer.
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "classify_request", lambda m, h: "web_search")
    monkeypatch.setattr(app, "requires_web_verification", lambda m, h: False)
    monkeypatch.setattr(app, "session_search_allowed", lambda state: True)
    monkeypatch.setattr(app, "run_web_search", lambda *a, **k: ("ok", [
        {"url": "https://a.example.test/x", "title": "A", "content": "c", "published": None}]))
    monkeypatch.setattr(app, "_post_openai_responses",
                        lambda payload, **kw: _msg([_ot(PARTIAL), _rf()]))
    reply = app.chat("Search the web for example reference material.", [], session_id="legacy-web-refuse")
    assert reply == provider_refusal.REFUSAL_RESPONSE
    assert "**Sources**" not in reply and "**Search results**" not in reply
    assert PARTIAL not in reply


def test_code_route_refusal_returns_fixed_reply(no_network, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "_post_openai_responses",
                        lambda payload, **kw: _msg([_ot(PARTIAL), _rf()]))
    reply = app.chat("Write a Python function to reverse a string.", [], session_id="legacy-code-refuse")
    assert reply == provider_refusal.REFUSAL_RESPONSE
    assert PARTIAL not in reply


def test_normal_output_without_refusal_is_unchanged(no_network, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "_post_openai_responses",
                        lambda payload, **kw: _msg([_ot("A perfectly ordinary helpful answer about rivers.")]))
    reply = app.chat("Explain rivers.", [], session_id="legacy-normal")
    assert "ordinary helpful answer about rivers" in reply
    assert reply != provider_refusal.REFUSAL_RESPONSE


def test_empty_without_refusal_is_provider_unavailable(no_network, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "_post_openai_responses", lambda payload, **kw: _msg([_ot("")]))
    with pytest.raises(app.ModelProviderUnavailable):
        app.chat("Explain rivers.", [], session_id="legacy-empty")


def test_genuine_exception_is_provider_unavailable(no_network, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)

    def boom(payload, **kw):
        raise ConnectionError("down")

    monkeypatch.setattr(app, "_post_openai_responses", boom)
    with pytest.raises(app.ModelProviderUnavailable):
        app.chat("Explain rivers.", [], session_id="legacy-boom")


def test_refusal_is_a_normal_successful_string(no_network, monkeypatch):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", False)
    monkeypatch.setattr(app, "_post_openai_responses", lambda payload, **kw: _msg([_rf()]))
    reply = app.chat("Explain gravity.", [], session_id="refuse-200")
    assert isinstance(reply, str) and reply == provider_refusal.REFUSAL_RESPONSE
