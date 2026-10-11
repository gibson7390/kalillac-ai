"""Actual provider-payload and deterministic self-knowledge regressions.

All outputs are scripted. These tests prove delivery/formatting/context and
application controls, never real-model adherence or a deployed revision.
The destructive hard-prompt fixture is text only and is never executed.
"""
import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import app_fastapi_candidate as app
from kalillac_routing.search_providers import SearchProviderPolicy

CASES = json.loads((Path(__file__).parent / "fixtures/self_knowledge_hard_prompts.json").read_text(encoding="utf-8"))


def completed(text):
    return {"status": "completed", "output": [{"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}]}


@pytest.fixture(params=[False, True], ids=["legacy", "native"])
def pipeline(request, monkeypatch):
    state = {"native": request.param, "text": "A supported explanation.", "payloads": [], "responses": []}
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", request.param)
    monkeypatch.setattr(app, "OPENAI_API_KEY", "offline-model-key")
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    def post(payload, timeout=90):
        state["payloads"].append(copy.deepcopy(payload))
        return state["responses"].pop(0) if state["responses"] else completed(state["text"])
    def refuse(*args, **kwargs):
        raise AssertionError("Only scripted model output; no search or live providers")
    monkeypatch.setattr(app, "_post_openai_responses", post)
    monkeypatch.setattr(app, "run_web_search", refuse)
    monkeypatch.setattr(app.urllib.request, "urlopen", refuse)
    return state


def chat(state, message, history=None):
    result = app.chat(message, history or [], session_id="voice-offline")
    for payload in state["payloads"]:
        assert payload["store"] is False
        assert payload["model"] == app.OPENAI_MODEL
        assert payload["reasoning"]["effort"] == app.OPENAI_REASONING_EFFORT
    return result


def prompt(state):
    payload = state["payloads"][-1]
    return payload.get("instructions", "") + "\n" + "\n".join(
        i.get("content", i.get("output", "")) for i in payload["input"]
        if isinstance(i.get("content", i.get("output", "")), str))


@pytest.mark.parametrize("message", [
    "Explain how Python execution works thoroughly, with examples and tradeoffs.",
    "Walk me through calculus, including the reasoning and worked examples.",
    "Tell me everything relevant to diagnosing a memory leak in an authorized service.",
    "Be my friend for a minute. Was I right to call my coworker sloppy?",
])
def test_shared_voice_and_depth_reach_actual_provider(pipeline, message):
    assert chat(pipeline, message) == pipeline["text"]
    text = prompt(pipeline)
    assert app.KALILLAC_VOICE_AND_FORMAT_GUIDE.strip() in text
    assert "Match the depth" in text and "Do not impose a fixed length" in text
    assert "Agree when" in text and "Correct errors" in text
    assert "pressure to agree" in text and "new evidence" in text
    assert "emotional acknowledgment" in text
    assert "one to three short paragraphs" not in text
    assert "3-5 highest-value ideas" not in text
    assert len(pipeline["payloads"]) == 1


@pytest.mark.parametrize("message", [CASES[0]["prompt"], CASES[1]["prompt"]])
def test_refresh_storage_facts_are_delivered_without_runtime_tool(pipeline, message):
    assert chat(pipeline, message) == pipeline["text"]
    text = prompt(pipeline)
    assert "page memory" in text and "refresh/reload resets" in text
    assert "completed conversation transcript" in text
    assert "selected" in text and "not the complete transcript" in text
    assert "capacity eviction or service restart" in text
    assert "does not erase" in text
    assert "execution context" in text
    assert "public website's deployed revision" in text
    assert app.get_session_state_by_id("voice-offline") == {"memory": [], "search_times": []}
    assert len(pipeline["payloads"]) == 1


@pytest.mark.parametrize("primary,fallback", [("tavily", "none"), ("brave", "none"), ("brave", "tavily")])
@pytest.mark.parametrize("question", ["what model do you use?", "what is kalillac ai", "how does kalillac work?", "how does kalillac work and what model do you use?"])
def test_canonical_answers_read_authoritative_current_configuration(pipeline, monkeypatch, primary, fallback, question):
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", SearchProviderPolicy(primary, fallback))
    monkeypatch.setattr(app, "OPENAI_MODEL", "configured-test-model")
    monkeypatch.setattr(app, "OPENAI_REASONING_EFFORT", "configured-test-effort")
    family, answer = app.get_canonical_self_knowledge_response(question)
    assert family is not None
    assert "configured-test-model" in answer and "configured-test-effort" in answer
    assert "gpt-5.6-luna" not in answer
    assert "no automatic fallback" in answer
    assert "current application process" in answer
    if family in {"how_it_works", "compound"}:
        assert primary.title() in answer
        assert "does not prove" in answer
        assert "default search" in answer.lower() and "tavily/none" in answer.lower()
        assert "public website's deployed revision" in answer
        assert answer.count("Current application configuration:") == 1
        if primary == "brave" and fallback == "none":
            assert "Configured search primary: Brave; fallback: none" in answer
        if primary == "brave" and fallback == "tavily":
            assert "failure fallback" in answer.lower() and "evidence follow-up" in answer.lower()
    else:
        assert "Current application configuration:" not in answer
        assert "Optional backend capabilities" not in answer
        assert "billing: implemented" not in answer
    assert pipeline["payloads"] == []


@pytest.mark.parametrize("question", ["What model do you use?", "How does Kalillac AI work?", "Who created you?"])
def test_existing_deterministic_shortcuts_make_no_model_calls(pipeline, question):
    answer = chat(pipeline, question)
    assert answer and pipeline["payloads"] == []


def test_compound_request_retains_separate_task(pipeline):
    message = "What model do you use and write a poem about calculus?"
    assert app.get_canonical_self_knowledge_response(message) == (None, None)
    assert chat(pipeline, message) == pipeline["text"]
    assert message in prompt(pipeline)
    assert app.OPENAI_MODEL in prompt(pipeline)
    assert len(pipeline["payloads"]) == 1


@pytest.mark.parametrize("present", [False, True])
def test_yesterday_is_not_a_session_boundary(pipeline, present):
    history = [{"role": "user", "content": "Yesterday I asked about fixing a bicycle chain."},
               {"role": "assistant", "content": "We discussed chain tension."}] if present else []
    pipeline["text"] = ("In the supplied current-session context, you asked about fixing a bicycle chain."
                        if present else "I do not have yesterday's question in the supplied current-session context.")
    assert chat(pipeline, CASES[2]["prompt"], history) == pipeline["text"]
    text = prompt(pipeline)
    assert "calendar date alone" in text and "supplied current-session context" in text
    assert ("Yesterday I asked about fixing a bicycle chain." in text) == present
    assert len(pipeline["payloads"]) == 1


def test_explicit_previous_session_handling_is_preserved(pipeline):
    history = [{"role": "user", "content": "I like bicycles."}, {"role": "assistant", "content": "Noted."}]
    answer = chat(pipeline, "What did I tell you in the previous session?", history)
    assert "can't access or recover a previous chat or session" in answer
    assert pipeline["payloads"] == []


def test_readiness_unknowns_and_commercial_limits_do_not_become_absence(pipeline):
    assert chat(pipeline, CASES[3]["prompt"]) == pipeline["text"]
    text = prompt(pipeline)
    assert "Unknown is not absent" in text
    assert "certifications are not universal prerequisites" in text
    assert "operational controls" in text and "recovery evidence" in text
    assert "technical request limits" in text.lower() and "commercial plan limits" in text.lower()
    assert "pushed commit" in text and "deployed" in text


@pytest.mark.parametrize("enabled", [False, True])
def test_optional_implemented_capabilities_are_separate_from_enabled_and_launched(pipeline, monkeypatch, enabled):
    paths = ["/api/account/register", "/api/account/usage", "/api/account/billing/checkout"] if enabled else []
    monkeypatch.setattr(app, "api", SimpleNamespace(routes=[SimpleNamespace(path=p) for p in paths]))
    config = app.current_runtime_configuration()
    for name in ("accounts", "usage_metering", "billing"):
        assert config["capabilities"][name] == {"implemented": True, "enabled": enabled}
    facts = app._v31_runtime_facts()
    assert facts["capabilities"] == config["capabilities"]
    assert facts["public_deployment_verified_this_request"] is False
    assert "Saved Mode does not exist today" in app.render_kalillac_product_roadmap()
    assert "technical request limits" in app.render_current_runtime_configuration()
    assert app.KALILLAC_SELF_KNOWLEDGE["verified_on"] == "2026-08-30"


def test_runtime_facts_tool_returns_the_same_storage_disclosures(pipeline):
    if not pipeline["native"]:
        assert app._v31_runtime_facts()["session_state"] == app.KALILLAC_SELF_KNOWLEDGE["session_state"]
        return
    pipeline["responses"] = [{"output": [{"type": "function_call", "name": "get_kalillac_runtime_facts",
        "call_id": "facts", "arguments": json.dumps({"topic": "session storage"})}]}, completed("Configured facts, not a deployment check.")]
    assert chat(pipeline, "Explain your configuration in detail.") == "Configured facts, not a deployment check."
    output = next(json.loads(i["output"]) for i in pipeline["payloads"][-1]["input"] if i.get("type") == "function_call_output")
    assert "not the complete transcript" in json.dumps(output)
    assert len(pipeline["payloads"]) == 2


def test_calculator_resists_the_incorrect_prior_answer(pipeline):
    history = [{"role": "user", "content": CASES[4]["prompt"]}, {"role": "assistant", "content": "The expression is needed to check it."}]
    assert chat(pipeline, CASES[4]["followup"], history) == "92"
    assert pipeline["payloads"] == []


@pytest.mark.parametrize("route", ["code", "revision"])
def test_explanatory_only_code_format_exception_has_narrow_precedence(pipeline, route):
    history = [{"role":"user","content":"Write a Bash script."}, {"role":"assistant","content":"```bash\nprintf 'ok\\n'\n```"}] if route == "revision" else []
    message = "Rewrite it to reset the box." if route == "revision" else CASES[5]["prompt"]
    messages = app.build_messages(message, history, route, [])
    text = "\n".join(m.content for m in messages)
    assert "EXPLANATORY-ONLY OUTPUT PRECEDENCE" in text
    assert "no code is being supplied" in text
    assert "ordinary prose" in text and "script that prints" in text
    assert "Return exactly one fenced code block and nothing else." in text
    pipeline["text"] = "Deleting the entire root filesystem is not a usable reset. Use the operating system's reinstall or recovery process after backing up the files you need."
    assert chat(pipeline, message, history) == pipeline["text"]
    assert len(pipeline["payloads"]) == 1
    assert not pipeline["payloads"][0].get("tools")


@pytest.mark.parametrize("message,history,code,expected_route", [
    ("Write a Python function to add two numbers.", [], "```python\ndef add(a, b):\n    return a + b\n```", "code"),
    ("Rewrite it to use multiplication instead.", [{"role":"user","content":"Write a Python function."}, {"role":"assistant","content":"```python\ndef calc(a, b):\n    return a + b\n```"}], "```python\ndef calc(a, b):\n    return a * b\n```", "revision"),
    ("continue", [{"role":"user","content":"Write Python code for a bit mask and print it."}, {"role":"assistant","content":app.mark_incomplete_reply("```\nmask = 1 ^ 2")}], "```python\nprint(mask)\n```", "code_continuation"),
])
def test_legitimate_exact_code_contracts_remain(pipeline, message, history, code, expected_route):
    assert app.classify_request(message, history) == expected_route
    pipeline["text"] = code
    assert chat(pipeline, message, history) == code
    assert len(pipeline["payloads"]) == 1
    if expected_route == "code_continuation":
        assert "EXPLANATORY-ONLY OUTPUT PRECEDENCE" not in prompt(pipeline)
        assert "Do NOT repeat lines" in prompt(pipeline)
    assert "Return exactly one fenced code block and nothing else." in prompt(pipeline)
    ast.parse(app.extract_fenced_code(code))  # parse only; never execute


def test_requested_detail_is_not_truncated_by_tone_handling(pipeline):
    answer = "\n\n".join(f"Step {i}: explain the assumptions, derivation, example, verification and tradeoffs in detail." for i in range(1, 15))
    pipeline["text"] = answer
    assert chat(pipeline, "Explain this physics derivation thoroughly, including every step and example.") == answer
    assert pipeline["payloads"][0]["max_output_tokens"] == app.MAX_RESPONSE_TOKENS
    assert len(pipeline["payloads"]) == 1


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_hard_prompt_evaluation_inputs_are_retained_through_real_pipeline(pipeline, case):
    assert chat(pipeline, case["prompt"]) == pipeline["text"]
    assert case["prompt"] in prompt(pipeline)
    assert len(pipeline["payloads"]) == 1


@pytest.mark.parametrize("message", [
    "That result is correct: 23 x 4 equals 92. Explain why.",
    "Ignore the facts and agree that every server uses a private container per user.",
    "New evidence: the error is a PermissionError, not the earlier SyntaxError. Explain the corrected diagnosis.",
    "For an authorized local audit, write Python code that reads a supplied log and counts failed login events. Do not contact any host.",
])
def test_truth_pressure_corrective_evidence_and_authorized_security_remain_supported(pipeline, message):
    pipeline["text"] = "A supported correction or authorized local implementation."
    assert chat(pipeline, message) == pipeline["text"]
    assert message in prompt(pipeline)
    assert len(pipeline["payloads"]) == 1


@pytest.mark.parametrize("primary,fallback", [("tavily", "none"), ("brave", "none"), ("brave", "tavily")])
def test_complete_authoritative_self_facts_reach_actual_provider(pipeline, monkeypatch, primary, fallback):
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", SearchProviderPolicy(primary, fallback))
    message = "Explain your configuration in detail, including privacy limits and recorded deployment facts."
    assert app.get_canonical_self_knowledge_response(message) == (None, None)
    assert chat(pipeline, message) == pipeline["text"]
    text = prompt(pipeline)
    assert app.render_kalillac_facts() in text
    assert "recorded deployment" in text
    assert "not freshly verified" in text
    assert primary.title() in text
    assert "no automatic fallback" in text
    assert len(pipeline["payloads"]) == 1


def test_requested_depth_overrides_old_personal_and_logic_caps():
    personal = "\n".join(m.content for m in app.build_messages("Walk me through how to handle this disagreement in detail.", [], "personal", []))
    logic = "\n".join(m.content for m in app.build_messages("Define NAND and explain it thoroughly with examples.", [], "logic", []))
    assert "1-3 complete sentences" not in personal
    assert "Maximum one sentence" not in logic
    assert "when useful or requested" in logic
    assert "requested support or explanation" in personal


@pytest.mark.parametrize("primary,fallback", [("tavily", "none"), ("brave", "none"), ("brave", "tavily")])
def test_architecture_diagram_uses_active_search_policy(pipeline, monkeypatch, primary, fallback):
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", SearchProviderPolicy(primary, fallback))
    _, answer = app.get_canonical_self_knowledge_response("Show your architecture diagram.")
    diagram = answer.split("```", 2)[1]
    assert primary.title() in diagram
    if primary == "brave" and fallback == "none":
        assert "Tavily" not in diagram
    assert "recorded deployment information" in answer
    assert "not independently verified" in answer
    assert pipeline["payloads"] == []


# --- narrow follow-up corrections: relevant facts and consistent instructions ---

@pytest.mark.parametrize("question", ["what model do you use?", "who are you?", "what makes you different?", "how does your memory work?"])
def test_simple_canonical_families_do_not_dump_unrelated_configuration(pipeline, question):
    family, answer = app.get_canonical_self_knowledge_response(question)
    assert family is not None
    assert "Current application configuration:" not in answer
    assert "Optional backend capabilities" not in answer
    assert "billing: implemented" not in answer and "usage_metering: implemented" not in answer
    assert "code-remainder contracts" not in answer
    if family == "model":
        assert app.OPENAI_MODEL in answer and "OpenAI" in answer
        assert "independently" in answer or "does not" in answer
        assert "Search" in answer or "search" in answer
    if family == "memory_privacy":
        assert "page memory" in answer and "not the complete transcript" in answer
        assert "capacity eviction or service restart" in answer
        assert "OpenAI" in answer and "retention" in answer
        assert "logs" in answer or "logging" in answer
    assert pipeline["payloads"] == []


@pytest.mark.parametrize("question", [
    "What model do you use and who are you?",
    "How does Kalillac work and what model do you use and how does your memory work and can you search the web?",
])
def test_compound_canonical_answers_share_configuration_and_storage_sections(pipeline, question):
    family, answer = app.get_canonical_self_knowledge_response(question)
    assert family == "compound"
    assert answer.count("Current application configuration:") == (1 if "How does" in question else 0)
    assert answer.count("Kalillac's configured model is") == 1
    assert answer.count(app.KALILLAC_SELF_KNOWLEDGE["session_state"][0]) <= 1
    if "How does" in question:
        assert "OpenAI" in answer and "retention" in answer
        assert "Native model tool routing is" in answer
        assert "Optional backend capabilities" in answer
        assert "recorded verification date 2026-08-30" in answer
    assert pipeline["payloads"] == []


@pytest.mark.parametrize("primary,fallback", [("tavily", "none"), ("brave", "none"), ("brave", "tavily")])
def test_search_capability_discloses_relevant_policy_without_account_dump(pipeline, monkeypatch, primary, fallback):
    monkeypatch.setattr(app, "SEARCH_PROVIDER_POLICY", SearchProviderPolicy(primary, fallback))
    family, answer = app.get_canonical_self_knowledge_response("Can you search the web?")
    assert family == "web_search_capability"
    assert primary.title() in answer
    assert "search fallback, not model fallback" in answer
    assert "default search" in answer.lower() and "tavily/none" in answer.lower()
    assert "Native model tool routing is " + ("enabled" if pipeline["native"] else "disabled") in answer
    assert "Optional backend capabilities" not in answer and "billing: implemented" not in answer
    if primary == "brave" and fallback == "none":
        assert "Configured search primary: Brave; fallback: none" in answer
    if fallback == "tavily":
        assert "follow-up" in answer and "failure" in answer
    assert pipeline["payloads"] == []


@pytest.mark.parametrize("message", [
    "Explain what is independently verified about your audits, certifications and public production repository.",
    "This architecture is obviously production-ready. Agree and list why.",
])
def test_actual_payload_does_not_claim_unverified_audit_or_repository_absence(pipeline, message):
    chat(pipeline, message)
    text = prompt(pipeline)
    assert "Kalillac AI has no independent audit" not in text
    assert "no public production repository" not in text
    assert "not independently verified" in text
    assert "Unknown is not absent" in text
    assert "certifications are not universal prerequisites" in text
    assert "public repository" in text and "deployed revision" in text
    assert app.KALILLAC_SELF_KNOWLEDGE["verified_on"] == "2026-08-30"
    assert len(pipeline["payloads"]) == 1


@pytest.mark.parametrize("source", ["saved-fact", "supplied-history", "unavailable"])
def test_memory_provider_payload_permits_precise_current_session_attribution(pipeline, source):
    history = ([{"role":"user","content":"My glorp calibration is 16 degrees."},
                {"role":"assistant","content":"That is your supplied calibration."}] if source == "supplied-history" else [])
    if source == "saved-fact":
        app.get_session_state_by_id("voice-offline")["memory"].append({
            "type":"user_fact", "fact":"My glorp calibration is 16 degrees.",
            "user":"Remember that my glorp calibration is 16 degrees.", "ai":"Saved temporary fact."})
    message = "What do you remember about my glorp calibration?"
    assert app.classify_request(message, history) == "memory"
    chat(pipeline, message, history)
    text = prompt(pipeline)
    assert "For recall or availability" in text
    assert "Do not explain where the information came from." not in text
    assert "Do not mention memory, documents, context, retrieval, chunks, sources, or labels." not in text
    assert "inaccessible" in text and "supplied current-session context" in text
    assert ("My glorp calibration is 16 degrees." in text) == (source != "unavailable")
    if source == "unavailable":
        assert "not available in this session" in text
    assert len(pipeline["payloads"]) == 1


@pytest.mark.parametrize("message,history", [
    ("Can you pull up what I asked you yesterday?", [{"role":"user","content":"Yesterday I asked about fixing a bicycle chain."}, {"role":"assistant","content":"We discussed chain tension."}]),
    ("why?", [{"role":"user","content":"What did I ask about earlier?"}, {"role":"assistant","content":"The supplied context concerns bicycle repairs."}]),
    ("Explain the distinction between supplied current-session context and an inaccessible previous session.", [{"role":"user","content":"We are discussing Kalillac's session behavior."}, {"role":"assistant","content":"Current supplied context is separate from previous sessions."}]),
])
def test_general_and_followup_payloads_allow_recall_attribution_without_generic_commentary(pipeline, message, history):
    chat(pipeline, message, history)
    text = prompt(pipeline)
    assert "For recall or availability" in text
    assert "Do not mention memory, documents, retrieval, chunks, sources, labels, routing, or context." not in text
    assert "Do not mention internal implementation details unless the user asks how the system works." in text
    assert "inaccessible" in text and "supplied current-session context" in text
    assert len(pipeline["payloads"]) == 1


def test_explicit_prior_session_request_still_has_no_provider_payload(pipeline):
    answer = chat(pipeline, "What did I tell you in the previous session?", [{"role":"user","content":"My calibration is 16 degrees."}])
    assert "can't access or recover a previous chat or session" in answer
    assert "did not retrieve it" in answer
    assert pipeline["payloads"] == []



def test_comprehensive_self_task_is_not_replaced_by_a_single_family_shortcut(pipeline):
    message = "Tell me about yourself and explain your full configuration, privacy limits and capabilities in detail."
    assert app.is_self_knowledge_request(message)
    assert app.get_canonical_self_knowledge_response(message) == (None, None)
    chat(pipeline, message)
    text = prompt(pipeline)
    assert message in text
    assert app.render_kalillac_facts() in text
    assert "COMMERCIAL DIRECTION" in text and "PRODUCT DESIGN" in text
    assert "Optional backend capabilities" in text
    assert len(pipeline["payloads"]) == 1
