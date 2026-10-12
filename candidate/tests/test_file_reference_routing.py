"""Offline file-reference boundaries and public documentation regressions.

Existing scripted provider fixtures exercise actual payload construction and
shared search policy; no external provider or private content is used.
"""
import copy
import json

import pytest
import app_fastapi_candidate as app
from kalillac_routing.provider_usage import EvaluationCollector, observation_scope
from kalillac_routing.request_budget import RequestBudget, budget_scope
from kalillac_routing.bounded_transport import TransportHTTPError
from test_search_provider_policy import providers, brave_result, tavily_result
from test_router_semantic_behavior import models


DOCUMENTATION_CASES = [
    (17, "Search current Node.js documentation for AbortController behavior. Restrict sources to nodejs.org.", "nodejs.org"),
    (18, "Search current MDN documentation for Fetch AbortSignal. Restrict sources to developer.mozilla.org.", "developer.mozilla.org"),
]
PUBLIC_VARIANTS = [
    "Search the documentation for the latest public API behavior.",
    "Search documentation indexes for a current public reference.",
    "Search the documentation for the public standard and explain the relevant behavior with context and examples from current authoritative references.",
    "Explain how documentation helps developers understand an API.",
    "Search the documentary catalogue for public release dates.",
    "Search the profile of a public institution.",
]
VAGUE = "do that thing with the file"
TEXT_HISTORY = [
    {"role": "user", "content": "Please summarize the file in one sentence. Here is the file: Mercury completes an orbit in about eighty-eight Earth days."},
    {"role": "assistant", "content": "I can summarize the supplied text in one sentence."},
]
CODE_HISTORY = [
    {"role": "user", "content": "Please replace the loop with a list comprehension.\n```python\nvalues = [1, 2, 3]\nresult = []\nfor value in values:\n    result.append(value * 2)\n```"},
    {"role": "assistant", "content": "I can replace the supplied loop with a list comprehension."},
]
UNCLEAR_HISTORY = [
    {"role": "user", "content": "Here is the file: Mercury completes an orbit in about eighty-eight Earth days."},
    {"role": "assistant", "content": "What would you like to do with that text?"},
]


@pytest.mark.parametrize("message", [c[1] for c in DOCUMENTATION_CASES] + PUBLIC_VARIANTS)
def test_public_words_are_not_unavailable_files(message):
    assert not app.is_file_reference_request(message)
    assert app.classify_request(message, []) != "file_unavailable"


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("fallback", [False, True], ids=["brave-success", "tavily-fallback"])
@pytest.mark.parametrize("case_number,message,domain", DOCUMENTATION_CASES)
def test_exact_documentation_cases_reach_provider_pipeline(providers, monkeypatch, native, fallback, case_number, message, domain):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    monkeypatch.setattr(app, "OPENAI_API_KEY", "offline-model-key")
    answer = "The scripted documentation evidence explains cancellation behavior."
    evidence = "Cancellation uses an abort signal; consult the linked documentation for its semantics."
    url = "https://" + domain + "/reference"
    providers["brave"] = [TransportHTTPError(500) if fallback else brave_result(url=url, content=evidence)]
    providers["tavily"] = [tavily_result(url=url, content=evidence)] if fallback else []

    def model(payload, budget, timeout, request_selected):
        providers["model"].append(copy.deepcopy(payload))
        outputs = [i for i in payload.get("input", []) if isinstance(i, dict) and i.get("type") == "function_call_output"]
        if native and not outputs:
            output = [{"type": "function_call", "name": "search_web", "call_id": "docs-search", "arguments": json.dumps({"query": message})}]
        else:
            if native:
                result = json.loads(outputs[-1]["output"])
                assert result["status"] == "ok" and result["results"][0]["url"] == url
            else:
                assert evidence in str(payload)
            output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": answer}]}]
        return {"status": "completed", "model": "gpt-5.6-luna", "service_tier": "default", "output": output,
                "usage": {"input_tokens": 120, "output_tokens": 20}}

    monkeypatch.setattr(app, "_post_openai_bounded", model)
    budget = RequestBudget(duration_seconds=45, max_model_attempts=6, max_search_attempts=6)
    collector = EvaluationCollector()
    with observation_scope(collector, case_number=case_number), budget_scope(budget):
        reply = app.chat(message, [], session_id="documentation-offline")
    assert app.classify_request(message, []) == "web_search"
    assert answer in reply and url in reply and reply.count("**Sources**") == 1
    assert [c["provider"] for c in providers["calls"]] == (["brave", "tavily"] if fallback else ["brave"])
    assert providers["calls"][0]["payload"]["goggles"] == "$discard\n$site=" + domain
    if fallback:
        assert providers["calls"][1]["payload"]["include_domains"] == [domain]
    assert budget.model_attempts == (2 if native else 1)
    assert budget.search_attempts == (2 if fallback else 1)
    assert message in str(providers["model"][0])
    row = collector.snapshot()["requests"][0]
    assert not row["observation_lost"] and not row["unobserved_operations"]
    assert len([a for a in row["attempts"] if a["provider"] == "openai"]) == budget.model_attempts


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("history,expected", [
    (TEXT_HISTORY, "Mercury completes an orbit in about eighty-eight Earth days."),
    (CODE_HISTORY, "```python\nresult = [value * 2 for value in values]\n```"),
    (UNCLEAR_HISTORY, "Would you like a summary, a rewrite, or an explanation of that text?"),
])
def test_vague_file_uses_supplied_task_or_clarifies(models, monkeypatch, native, history, expected):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    models["reply"] = expected
    assert app.is_targeted_clarification_needed(VAGUE)
    assert app.classify_request(VAGUE, history) == "unclear"
    assert app.chat(VAGUE, history, session_id="supplied-file") == expected
    payload = models["native"][-1][0] if native else models["legacy"][-1][0]
    delivered = "\n".join(item.get("content", "") for item in payload) if native else "\n".join(item.content for item in payload)
    assert VAGUE in delivered and history[0]["content"] in delivered


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("message", [VAGUE, "Use the document", "Summarize my notes", "Search my uploaded file for nodejs.org", "Search my notes for developer.mozilla.org", "Use the file on my laptop"])
def test_unavailable_private_files_do_not_call_providers(models, monkeypatch, native, message):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    assert app.is_file_reference_request(message)
    assert app.classify_request(message, []) == "file_unavailable"
    assert "doesn't have access" in app.chat(message, [], session_id="unavailable-file")
    assert not models["native"] and not models["legacy"]


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_prior_session_file_is_not_retrieved_from_current_history(models, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    reply = app.chat("Use the file we discussed in the previous session", TEXT_HISTORY, session_id="older-file")
    assert "can't access or recover a previous chat or session" in reply
    assert not models["native"] and not models["legacy"]


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("message,history", [
    ("Search the file", TEXT_HISTORY),
    ("Search my notes: Mercury completes an orbit in about eighty-eight Earth days.", []),
    ("Search the file:\n```text\nMercury completes an orbit in about eighty-eight Earth days.\n```", []),
])
def test_supplied_private_file_search_stays_local(models, monkeypatch, native, message, history):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    models["reply"] = "Which passage would you like me to find in the supplied text?"
    assert app.classify_request(message, history) not in {"file_unavailable", "web_search"}
    assert app.is_private_search_target(message)
    assert app.chat(message, history, session_id="local-file-search") == models["reply"]
    assert models["native"] if native else models["legacy"]


@pytest.mark.parametrize("history", [
    [{"role": "user", "content": "Here is the file: C:\\Users\\Example\\notes.txt"}],
    [{"role": "user", "content": "Here is the file: notes.txt"}],
    [{"role": "assistant", "content": "The file contains Mercury completes an orbit in about eighty-eight Earth days."}],
    TEXT_HISTORY + [{"role": "user", "content": "I uploaded the file from my laptop."}],
    TEXT_HISTORY + [{"role": "user", "content": "A different topic."}] * 4,
])
def test_paths_claims_and_out_of_window_contents_are_not_supplied_context(models, history):
    assert app.classify_request(VAGUE, history) == "file_unavailable"
    assert "doesn't have access" in app.chat(VAGUE, history, session_id="missing-supplied-file")
    assert not models["native"] and not models["legacy"]


@pytest.mark.parametrize("history", [TEXT_HISTORY, CODE_HISTORY])
def test_named_unavailable_file_does_not_borrow_unrelated_contents(models, history):
    message = "Use the file on my laptop"
    assert app.classify_request(message, history) == "file_unavailable"
    assert "doesn't have access" in app.chat(message, history, session_id="another-file")
    assert not models["native"] and not models["legacy"]


@pytest.mark.parametrize("message,history,query", [
    ("Search the file", TEXT_HISTORY, "Mercury orbital period"),
    ("Explain how file formats work.", [], "Search my uploaded file for nodejs.org"),
    ("Search my notes: Mercury completes an orbit in about eighty-eight Earth days.", [], "Mercury orbital period"),
])
def test_native_private_file_queries_are_rejected_before_external_search(models, monkeypatch, message, history, query):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    seen = []
    def native(items, instructions):
        outputs = [item for item in items if item.get("type") == "function_call_output"]
        if not outputs:
            return {"output": [{"type": "function_call", "name": "search_web", "call_id": "private-file", "arguments": json.dumps({"query": query})}]}
        result = json.loads(outputs[-1]["output"])
        seen.append(result)
        assert result["status"] == "rejected" and "private" in result["reason"]
        return {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "I can work with the supplied text locally."}]}]}
    monkeypatch.setattr(app, "_invoke_openai_native_tools", native)
    assert app.chat(message, history, session_id="private-file-tool") == "I can work with the supplied text locally."
    assert len(seen) == 1


@pytest.mark.parametrize("domain", ["developer.mozilla.org", "docs.example.org", "api.docs.example.com", "reference.example-government.gov"])
def test_explicit_bare_subdomains_are_preserved(domain):
    message = "Search current public documentation. Restrict sources to " + domain + "."
    assert app.extract_domains(message) == [domain]
    assert app.get_search_domain_filters(message) == [domain]


def test_email_address_is_not_a_public_domain_restriction():
    assert app.get_search_domain_filters("Summarize the text from author@docs.example.org") == []


@pytest.mark.parametrize("content", [
    "C:\\Users\\Example\\project notes\\todo list.txt",
    "```text\nC:\\Users\\Example\\notes.txt\n```",
    "```python\n\n```",
])
def test_paths_and_empty_fences_do_not_establish_file_contents(models, content):
    history = [{"role": "user", "content": "Here is the file: " + content}]
    assert app.classify_request(VAGUE, history) == "file_unavailable"
    assert "doesn't have access" in app.chat(VAGUE, history, session_id="file-metadata-only")
    assert not models["native"] and not models["legacy"]
