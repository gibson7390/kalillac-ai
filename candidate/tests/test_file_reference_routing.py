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
    assert not hasattr(app, "is_targeted_clarification_needed")
    assert app.classify_request(VAGUE, history) == "followup"
    assert app.chat(VAGUE, history, session_id="supplied-file") == expected
    payload = models["native"][-1][0] if native else models["legacy"][-1][0]
    delivered = "\n".join(item.get("content", "") for item in payload) if native else "\n".join(item.content for item in payload)
    assert VAGUE in delivered and history[0]["content"] in delivered


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("message", ["Summarize my notes", "Search my uploaded file for nodejs.org", "Search my notes for developer.mozilla.org", "Use my document that has not been supplied"])
def test_unavailable_private_files_do_not_call_providers(models, monkeypatch, native, message):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    assert app.is_file_reference_request(message)
    assert app.classify_request(message, []) == "file_unavailable"
    assert "doesn't support file uploads or attachments" in app.chat(message, [], session_id="unavailable-file")
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
    # Generic wording now reaches the model; metadata/claims do not grant access.
    models["reply"] = "Paste the relevant text or code here and say what you'd like done."
    assert app.classify_request(VAGUE, history) != "file_unavailable"
    assert app.is_private_search_target(VAGUE)
    assert app.chat(VAGUE, history, session_id="missing-supplied-file") == models["reply"]
    assert models["native"] or models["legacy"]


@pytest.mark.parametrize("history", [TEXT_HISTORY, CODE_HISTORY])
def test_named_unavailable_file_does_not_borrow_unrelated_contents(models, history):
    message = "Use my document that has not been supplied"
    assert app.classify_request(message, history) == "file_unavailable"
    assert "doesn't support file uploads or attachments" in app.chat(message, history, session_id="another-file")
    assert not models["native"] and not models["legacy"]


@pytest.mark.parametrize("message,history,query", [
    ("Search the file", TEXT_HISTORY, "Mercury orbital period"),
    ("Explain how file formats work.", [], "Search my uploaded file for nodejs.org"),
    ("Explain how file formats work.", [], "Search my attached PDF for nodejs.org"),
    ("Explain how file formats work.", [], r"Read C:\Users\Example\notes.txt"),
    ("Explain how file formats work.", [], "Read C:/Users/Example/Project Notes/my_notes-v2.txt"),
    ("Explain how file formats work.", [], r"Read \\server-name\shared_folder\Project Notes\notes-v2.txt"),
    ("Explain how file formats work.", [], "Read /home/example/my_notes-v2.txt"),
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
    assert not app._supplied_file_content_is_available(content)
    models["reply"] = "Paste the actual text or code; a path alone does not supply file contents."
    assert app.classify_request(VAGUE, history) != "file_unavailable"
    assert app.is_private_search_target(VAGUE)
    assert app.chat(VAGUE, history, session_id="file-metadata-only") == models["reply"]
    assert models["native"] or models["legacy"]

# Conversational repair regressions: actual provider payloads, scripted offline.
@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("message,history,answer", [
    (VAGUE, [], "What would you like done? Paste the relevant text or code here."),
    ("??", [], "Please say what you would like help with."),
    (VAGUE, TEXT_HISTORY, "Mercury completes an orbit in about eighty-eight Earth days."),
    (VAGUE, CODE_HISTORY, "```python\nresult = [value * 2 for value in values]\n```"),
    ("what?", [
        {"role": "user", "content": VAGUE},
        {"role": "assistant", "content": "Kalillac AI doesn't have access to personal documents, notes, or files. If you paste the relevant text into the chat, I can work with it directly."},
    ], "I mean I need the text here and the task you want done."),
    ("what?", [
        {"role": "user", "content": VAGUE},
        {"role": "assistant", "content": "Please paste the text here and say what you want done."},
        {"role": "user", "content": "what?"},
        {"role": "assistant", "content": "Upload the file or tell me what you want done with it."},
    ], "That upload suggestion was incorrect. This chat accepts pasted text, so paste the relevant content here."),
    ("How are uploaded files encrypted?", [], "This interface has no file-upload feature. File encryption in other systems depends on their design."),
    ("write code for me to sort a list", [], "```python\nvalues = [3, 1, 2]\nprint(sorted(values))\n```"),
    ("add dark theme", [
        {"role": "user", "content": "Make the page dark."},
        {"role": "assistant", "content": "```html\n<!DOCTYPE html><html><head><style>body{color:#111;background:#fff}</style></head><body>Hello</body></html>\n```"},
    ], "```html\n<!DOCTYPE html><html><head><style>body{color:#eee;background:#111}</style></head><body>Hello</body></html>\n```"),
])
def test_conversational_repair_actual_provider_payload(monkeypatch, native, message, history, answer):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    monkeypatch.setattr(app, "OPENAI_API_KEY", "offline-synthetic-key")
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    captured = []
    def post(payload):
        captured.append(copy.deepcopy(payload))
        return {"status": "completed", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": answer}]}],
                "usage": {"input_tokens": 50, "output_tokens": 20}}
    monkeypatch.setattr(app, "_post_openai_for_attempt", post)
    def no_search(*args, **kwargs):
        raise AssertionError("Private/vague requests must not dispatch external search")
    monkeypatch.setattr(app, "run_web_search", no_search)
    route = app.classify_request(message, history)
    assert route != "file_unavailable"
    if message == "what?":
        assert route == "followup"
    if message == "write code for me to sort a list":
        assert route == "code"
    if message == "add dark theme":
        assert route == "revision"
    reply = app.chat(message, history, session_id="conversational-repair")
    assert answer in reply
    assert len(captured) == 1
    payload = captured[0]
    assert ("instructions" in payload) == (native and app.native_semantic_route_eligible(route, history))
    inputs = "\n".join(item.get("content", "") for item in payload["input"] if isinstance(item, dict))
    delivered = inputs + "\n" + payload.get("instructions", "")
    assert message in inputs
    if route == "revision":
        # Existing revision prompts retain the delivered code, not every earlier turn.
        assert app.get_last_assistant_message(history) in inputs
    else:
        for turn in history:
            assert turn["content"] in inputs
    assert "The current public Kalillac chat does not support file uploads or attachments." in delivered
    assert "Never claim or imply that the user can upload or attach a file to the current Kalillac chat." in delivered
    assert "uploads or attachments in other services" in delivered
    assert "code that implements upload or attachment functionality" in delivered
    assert "Do not use document information." not in delivered
    assert "text or code pasted into the current conversation" in delivered
    assert delivered.count("CURRENT PUBLIC CHAT CAPABILITIES:") == 1
    assert payload["store"] is False
    assert payload["model"] == app.OPENAI_MODEL
    assert payload["reasoning"] == {"effort": app.OPENAI_REASONING_EFFORT}


@pytest.mark.parametrize("message", [
    "summarize my document", "I uploaded the PDF", "I attached the PDF",
    "I attached the screenshot",
])
@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_explicit_unavailable_content_gets_accurate_paste_boundary(models, monkeypatch, native, message):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    assert app.classify_request(message, []) == "file_unavailable"
    reply = app.chat(message, [], session_id="unavailable-content")
    assert "paste" in reply.lower()
    assert "doesn't support file uploads or attachments" in reply.lower()
    assert "upload the" not in reply.lower() and "attach the" not in reply.lower()
    assert not models["native"] and not models["legacy"]


@pytest.mark.parametrize("message", ["the file", "use the file", "fix the file", VAGUE])
def test_generic_file_reference_is_not_unavailable_but_search_stays_private(message):
    assert not app.is_file_reference_request(message)
    assert app.classify_request(message, []) != "file_unavailable"
    assert app.is_private_search_target("Search " + message)

@pytest.mark.parametrize("message", [
    "How do I read a file in Python?",
    r"How do I read C:\Users\Example\notes.txt in Python?",
    "Read https://docs.python.org/3/library/pathlib.html and explain it.",
    "Read https://example.test/reference?example=/home/user/notes.txt and explain it.",
    "Explain how attached documents are handled by other applications.",
])
def test_conceptual_and_public_file_questions_are_not_device_access(message):
    assert not app.is_file_reference_request(message)


def test_capability_rule_survives_legacy_automatic_continuation(monkeypatch):
    monkeypatch.setattr(app, "OPENAI_API_KEY", "offline-synthetic-key")
    seen = []
    messages = [app.SystemMessage(content="You are a precise code repairer."),
                app.HumanMessage(content="Repair the pasted code.")]
    def post(payload):
        seen.append(copy.deepcopy(payload))
        if len(seen) == 1:
            return {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "First part. "}]}]}
        return {"status": "completed", "output": [{"type": "message",
                "content": [{"type": "output_text", "text": "Remaining part."}]}]}
    monkeypatch.setattr(app, "_post_openai_for_attempt", post)
    result = app.invoke_llm(messages, max_tokens=123)
    assert "First part." in result.content and "Remaining part." in result.content
    assert len(seen) == 2
    assert [m.content for m in messages] == ["You are a precise code repairer.", "Repair the pasted code."]
    for payload in seen:
        assert sum(i.get("content") == app.CURRENT_INTERFACE_CAPABILITIES for i in payload["input"]) == 1
        assert payload["max_output_tokens"] == 123
        assert payload["store"] is False
        assert payload["reasoning"] == {"effort": app.OPENAI_REASONING_EFFORT}

@pytest.mark.parametrize("history", [[], [{"role": "assistant", "content": "```python\nprint(1)\n```"}]])
def test_code_history_does_not_forbid_supplied_document_information(history):
    messages = app.build_messages("Compare the supplied code.", history, "code_history", [])
    prompt = "\n".join(m.content for m in messages)
    assert "Do not use document information." not in prompt
    assert "Do not use inaccessible document information." in prompt

# Review acceptance: scripted routing/payload evidence, not live-model quality.
RAW_PRIVATE_PATHS = [
    r"C:\Users\Example\notes.txt",
    "C:/Users/Example/Project Notes/my_notes-v2.txt",
    r"\\server-name\shared_folder\Project Notes\notes-v2.txt",
    "/home/example/my_notes-v2.txt",
    "/var/log/My Project/log_file-v2",
    "/etc/nginx/nginx.conf",
    "/api/chat",
    r"C:\Users\Example\My Document Folder\notes-v2",
    "//server-name/shared_folder/My Notes/file-v2",
]

REVIEW_MODEL_CASES = [
    ("Use /api/v1/users in my fetch call", [], "```javascript\nfetch('/api/v1/users');\n```"),
    ("Show me how to call /v1/chat/completions with curl", [], "```bash\ncurl -X POST https://example.test/v1/chat/completions -H 'Content-Type: application/json' -d '{}'\n```"),
    ("Find /var/log entries with grep", [], "```bash\ngrep -R 'pattern' /var/log\n```"),
    ("Use /api/chat for the POST request in this pasted code", [
        {"role": "user", "content": "Here is the code: ```javascript\nfetch('/old', {method: 'POST'});\n```"},
        {"role": "assistant", "content": "I can change the supplied endpoint."},
    ], "```javascript\nfetch('/api/chat', {method: 'POST'});\n```"),
    ("Open /etc/nginx/nginx.conf and explain the default settings", [],
     "I can't open your device files. Paste the configuration here; I can explain typical settings without claiming they match your file."),
    (r"Read C:\Users\Example\notes.txt", [], "Paste the text here so I can work with its actual contents."),
    ("We attached a debugger and the breakpoint never hits", [], "Check that symbols and the running executable match."),
    ("We attached a listener", [], "Which event should the listener handle?"),
    ("I uploaded my site to Netlify and now it shows a 404", [], "Check the publish directory and route configuration."),
    ("I uploaded my photos to Google Photos; how do I share an album?", [], "Open the album and use that service's sharing controls."),
    ("How do I attach a file in Gmail?", [], "Use Gmail's attachment control when composing an email."),
    ("How do I upload a file to Google Drive?", [], "Use Google Drive's upload control."),
    ("Write an HTML upload form", [], "```html\n<form action='/upload' method='post' enctype='multipart/form-data'><label>File<input type='file' name='file'></label><button type='submit'>Upload</button></form>\n```"),
    ("Write Python code to upload a file to S3", [], "```python\nimport boto3\nclient = boto3.client('s3')\nclient.upload_file('example.txt', 'example-bucket', 'example.txt')\n```"),
    ("I uploaded the PDF to another service; how do I share it?", [], "Use that service's sharing controls; its exact steps depend on the service."),
    ("I attached the screenshot to an email; how do I resize it?", [], "You can resize the image before attaching it to an email."),
    ("Use /api/chat for the POST request in this pasted code\n```javascript\nfetch('/old', {method: 'POST'});\n```", [],
     "```javascript\nfetch('/api/chat', {method: 'POST'});\n```"),
]
REVIEW_MODEL_CASES += [
    ("Read " + path, [], "Paste the actual contents here; a path alone does not supply them.")
    for path in RAW_PRIVATE_PATHS
]
REVIEW_MODEL_CASES += [
    ("Write Python code that prints 'I uploaded the PDF'", [],
     "```python\nprint('I uploaded the PDF')\n```"),
    ("Open /home/example/notes.txt", [], "Paste the actual text so I can explain it."),
    ("Use the file on my laptop", [], "Paste the content and say what you'd like done."),
]


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("message,history,answer", REVIEW_MODEL_CASES)
def test_review_examples_reach_actual_model_payload(monkeypatch, native, message, history, answer):
    original = copy.deepcopy(history)
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    monkeypatch.setattr(app, "OPENAI_API_KEY", "offline-synthetic-key")
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    captured = []
    def post(payload):
        captured.append(copy.deepcopy(payload))
        return {"status": "completed", "output": [{"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": answer}]}],
                "usage": {"input_tokens": 50, "output_tokens": 20}}
    monkeypatch.setattr(app, "_post_openai_for_attempt", post)
    def no_search(*args, **kwargs):
        raise AssertionError("No external search is scripted in these payload tests")
    monkeypatch.setattr(app, "run_web_search", no_search)
    route = app.classify_request(message, history)
    assert route != "file_unavailable"
    reply = app.chat(message, history, session_id="review-acceptance")
    assert reply.strip()
    assert len(captured) == 1
    payload = captured[0]
    delivered = "\n".join(item.get("content", "") for item in payload["input"]) + "\n" + payload.get("instructions", "")
    assert message in delivered
    assert delivered.count("CURRENT PUBLIC CHAT CAPABILITIES:") == 1
    assert "Never instruct the user to upload or attach a file." not in delivered
    assert "to the current Kalillac chat" in delivered
    assert "uploads or attachments in other services" in delivered
    assert "code that implements upload or attachment functionality" in delivered
    assert ("instructions" in payload) == (native and app.native_semantic_route_eligible(route, history))
    assert payload["store"] is False
    assert payload["model"] == app.OPENAI_MODEL
    assert payload["reasoning"] == {"effort": app.OPENAI_REASONING_EFFORT}
    assert history == original





@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("path", RAW_PRIVATE_PATHS)
def test_review_raw_paths_are_private_without_canned_routing(monkeypatch, native, path):
    message = "Read " + path
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    assert not app.is_file_reference_request(message)
    assert app.classify_request(message, []) != "file_unavailable"
    assert app.is_private_search_target(message)


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("message", ["I uploaded the PDF", "I attached the screenshot", "summarize my document"])
def test_review_current_chat_file_claims_still_have_paste_boundary(models, monkeypatch, native, message):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    assert app.classify_request(message, []) == "file_unavailable"
    reply = app.chat(message, [], session_id="review-current-claim")
    assert "paste" in reply.lower()
    assert "doesn't support file uploads or attachments" in reply.lower()
    assert not models["native"] and not models["legacy"]


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("message", ["what?", "how?", "why?"])
def test_review_brief_repair_questions_use_recent_context(models, monkeypatch, native, message):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    history = [{"role": "user", "content": VAGUE},
               {"role": "assistant", "content": "Please paste the content and say what you want done."}]
    original = copy.deepcopy(history)
    assert app.classify_request(message, history) == "followup"
    assert app.chat(message, history, session_id="review-short-repair") == models["reply"]
    items = models["native"][-1][0] if native else models["legacy"][-1][0]
    prompt = "\n".join(i.get("content", "") for i in items) if native else "\n".join(i.content for i in items)
    assert all(t["content"] in prompt for t in history)
    assert history == original

@pytest.mark.parametrize("path", RAW_PRIVATE_PATHS)
def test_review_path_privacy_scans_raw_input_without_normalization(monkeypatch, path):
    def forbidden(*args, **kwargs):
        raise AssertionError("Path privacy must inspect raw input")
    monkeypatch.setattr(app, "normalize_for_router", forbidden)
    assert app.has_private_path_reference(path)
    assert app.is_private_search_target("Read " + path)


@pytest.mark.parametrize("message", [
    "I uploaded the PDF here", "I attached the screenshot to this chat",
    "We uploaded a spreadsheet", "My attached image",
])
def test_review_actual_current_chat_file_objects_are_not_other_services(message):
    assert app.file_attachment_claim_scope(message) == "current"


@pytest.mark.parametrize("message", [
    "We attached a debugger", "We attached a listener",
    "I uploaded my site to a hosting service",
    "How do I attach a file in an email service?",
    "Write code that implements file upload",
    "Write Python code that prints 'I uploaded the PDF'",
])
def test_review_non_file_claims_do_not_establish_current_chat_uploads(message):
    assert app.file_attachment_claim_scope(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "Search the web for this exact private note and tell me whether it appears online: internal unpublished planning note",
        "Search online for my confidential draft.",
        "Look up our unpublished internal memo.",
    ],
)
def test_r2q3_explicit_private_material_never_reaches_search(message):
    assert app.is_explicit_private_material_search(message)
    assert app.is_private_search_target(message)
    assert not app.is_web_search_request(message, [])


@pytest.mark.parametrize(
    "message",
    [
        "Search the web for current privacy laws.",
        "Search the web for internal combustion engines.",
        "Search the web for a private company called OpenAI.",
        "Find public reporting about a leaked internal memo.",
    ],
)
def test_r2q3_public_topics_are_not_private_material(message):
    assert not app.is_explicit_private_material_search(message)
    assert not app.is_private_search_target(message)
