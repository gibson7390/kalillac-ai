"""Local staging's provider simulation (tools/local_staging_sim.py): local only, labelled, faithful to the
shapes the real code reads, and never imported by the application."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import sys

import pytest

CANDIDATE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CANDIDATE / "tools"))
import local_staging_sim as sim  # noqa: E402

LOCAL = {"KALILLAC_DEPLOYMENT_ID": "kalillac-local-staging", "KALILLAC_SITE_ORIGIN": "http://localhost:8010",
         "KALILLAC_STRIPE_MODE": "test"}


@pytest.mark.parametrize("change", [
    {"KALILLAC_DEPLOYMENT_ID": "kalillac-staging-v1"}, {"KALILLAC_DEPLOYMENT_ID": "production"},
    {"KALILLAC_SITE_ORIGIN": "https://kalillac.ai"}, {"KALILLAC_SITE_ORIGIN": "https://staging.kalillac.ai"},
    {"KALILLAC_STRIPE_MODE": "live"}, {"KALILLAC_STRIPE_MODE": ""}])
def test_the_simulation_refuses_anything_but_a_local_deployment(change):
    with pytest.raises(sim.LocalOnly):
        sim.require_local({**LOCAL, **change})
    sim.require_local(LOCAL)


def test_simulated_hosts_cannot_be_resolved(monkeypatch):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: calls.append(host) or [("ok",)])
    sim.guard_network(["openai", "stripe"])
    for host in ("api.openai.com", "API.OPENAI.COM.", "checkout.stripe.com", "api.stripe.com"):
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo(host, 443)
    assert socket.getaddrinfo("api.tavily.com", 443) == [("ok",)] and calls == ["api.tavily.com"]


def test_unknown_providers_are_refused():
    with pytest.raises(sim.LocalOnly):
        sim.simulated_providers({"KALILLAC_LOCAL_SIMULATE": "openai,anthropic"})
    assert sim.simulated_providers({}) == list(sim.PROVIDERS)


def _chat_payload(text, effort="high", image=False):
    content = [{"type": "input_text", "text": text}]
    if image:
        content.append({"type": "input_image", "detail": "high", "image_url": "data:image/png;base64," + "A" * 400_000})
    return {"model": "gpt-6-luna", "reasoning": {"effort": effort}, "max_output_tokens": 4000,
            "input": [{"role": "user", "content": content}]}


def test_a_chat_reply_is_labelled_shaped_like_the_responses_api_and_reports_bounded_usage():
    reply = sim.simulated_openai(_chat_payload("What is a webhook?"))
    (item,) = reply["output"]
    text = item["content"][0]["text"]
    assert item["type"] == "message" and text.startswith("**Simulated GPT-6 Luna.**") and "`high`" in text
    assert "webhook" not in text                                   # The customer's words are never quoted back.
    usage = reply["usage"]
    assert 0 < usage["input_tokens"] and 0 < usage["output_tokens"] <= 4000
    assert set(usage) >= {"input_tokens", "output_tokens", "input_tokens_details", "output_tokens_details"}


def test_an_image_counts_as_a_small_fixed_number_of_tokens_not_its_bytes():
    with_image = sim.simulated_openai({**_chat_payload("x", image=True), "tools": [{"name": "create_file"}]})
    assert with_image["usage"]["input_tokens"] < 2000
    assert "1 image(s) were checked" in with_image["output"][0]["content"][0]["text"]


FILE_TOOLS = [{"type": "function", "name": name} for name in ("create_file", "read_file", "edit_file")]


@pytest.mark.parametrize("words,fmt", [("a PDF report", "pdf"), ("a Word document", "docx"), ("an Excel model", "xlsx"),
                                       ("a PowerPoint deck", "pptx"), ("a summary", "pdf")])
def test_a_file_request_calls_create_file_with_a_complete_specification(words, fmt):
    reply = sim.simulated_openai({**_chat_payload(f"Make {words} about revenue"), "tools": FILE_TOOLS})
    (call,) = reply["output"]
    arguments = json.loads(call["arguments"])
    assert call["type"] == "function_call" and call["name"] == "create_file" and arguments["format"] == fmt
    assert isinstance(arguments["spec"], dict) and "(simulated sample)" in json.dumps(arguments["spec"])


@pytest.mark.parametrize("fmt", ["pdf", "docx", "xlsx", "pptx"])
def test_the_sample_specifications_really_generate(fmt):
    pytest.importorskip("typst")
    pytest.importorskip("docx")
    pytest.importorskip("openpyxl")
    pytest.importorskip("pptx")
    from kalillac_files import generate
    reply = sim.simulated_openai({**_chat_payload(f"Make a {fmt} file"), "tools": FILE_TOOLS})
    spec = json.loads(reply["output"][0]["arguments"])["spec"]
    assert len(generate(spec, fmt).data) > 1000


def test_an_edit_request_reads_the_attached_file_then_edits_it():
    note = ("Change the fee to $12,000 to $13,000.\n\nFiles the customer attached to this message (use their ids "
            "with read_file and edit_file):\n- id Abcdefgh123456: proposal.docx (application/vnd.openxmlformats-"
            "officedocument.wordprocessingml.document, 1234 bytes)")
    payload = {**_chat_payload("Replace $11,000 with $12,000\n\n" + note.split("\n\n", 1)[1]), "tools": FILE_TOOLS}
    first = sim.simulated_openai(payload)["output"][0]
    assert first["name"] == "read_file" and json.loads(first["arguments"]) == {"file_id": "Abcdefgh123456"}
    payload["input"] += [first, {"type": "function_call_output", "call_id": first["call_id"], "output": "{}"}]
    second = sim.simulated_openai(payload)["output"][0]
    assert second["name"] == "edit_file" and json.loads(second["arguments"])["operations"] == [
        {"op": "replace_text", "find": "$11,000", "replace": "$12,000"}]


def test_the_chats_confirmation_repeats_the_earlier_edit_as_exact():
    note = ("\n\nFiles the customer attached to this message (use their ids with read_file and edit_file):\n"
            "- id Abcdefgh123456: proposal.docx (application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document, 1234 bytes)")
    payload = {"model": "gpt-6-luna", "max_output_tokens": 100, "tools": FILE_TOOLS, "input": [
        {"role": "user", "content": "Replace $11,000 with $12,000"},
        {"role": "assistant", "content": "That would leave the total row not adding up."},
        {"role": "user", "content": sim.CONFIRM_TEXT + note}]}
    first = sim.simulated_openai(payload)["output"][0]
    assert first["name"] == "read_file"
    payload["input"] += [first, {"type": "function_call_output", "call_id": first["call_id"], "output": "{}"}]
    second = json.loads(sim.simulated_openai(payload)["output"][0]["arguments"])
    assert second == {"file_id": "Abcdefgh123456", "exact": True,
                      "operations": [{"op": "replace_text", "find": "$11,000", "replace": "$12,000"}]}


def test_after_the_tools_ran_the_reply_names_the_real_result():
    payload = {**_chat_payload("Make a PDF"), "tools": FILE_TOOLS,
               "input": [{"role": "user", "content": "Make a PDF"},
                         {"type": "function_call", "name": "create_file", "call_id": "c1", "arguments": "{}"},
                         {"type": "function_call_output", "call_id": "c1", "output": json.dumps(
                             {"status": "created", "filename": "report.pdf", "format": "pdf", "size": 103423})}]}
    text = sim.simulated_openai(payload)["output"][0]["content"][0]["text"]
    assert "**report.pdf** (PDF, 101 KB)" in text and "sample benchmark" in text


def test_simulated_stripe_events_verify_with_stripes_own_code_and_parse_into_the_right_state(tmp_path, monkeypatch):
    stripe = pytest.importorskip("stripe")
    from kalillac_billing.stripe_gateway import subscription_state_from_stripe, webhook_event_from_stripe
    monkeypatch.setenv("KALILLAC_LOCAL_STATE", str(tmp_path))
    record = sim.stripe_record()
    record.save({"sessions": {"cs_test_sim_1": {
        "id": "cs_test_sim_1", "status": "open", "idempotency_key": "k", "url": "https://checkout.stripe.com/c/pay/cs_test_sim_1",
        "price": "price_local", "customer": None, "user": "00000000-0000-4000-8000-00000000000a",
        "success_url": "http://localhost:8010/account?checkout=success", "cancel_url": "x", "expires_at": 0,
        "subscription": None}}, "subscriptions": {}, "customers": {}})
    success, events = sim.complete_checkout("cs_test_sim_1")
    assert success.endswith("?checkout=success") and [e["type"] for e in events] == [
        "checkout.session.completed", "customer.subscription.created"]
    secret = "whsec_" + "a" * 32
    for event in events:
        body, header = sim.signed(event, secret)
        stripe.WebhookSignature.verify_header(body.decode(), header, secret, tolerance=300)
        parsed = webhook_event_from_stripe(json.loads(body))
        assert parsed.livemode is False and parsed.kalillac_user_id == "00000000-0000-4000-8000-00000000000a"
        assert parsed.subscription_id.startswith("sub_sim_")
    state = subscription_state_from_stripe(events[1]["data"]["object"])
    assert (state.status, state.price_id, state.cancel_at_period_end) == ("active", "price_local", False)
    assert state.current_period_end is not None
    ended = sim.change_subscription("00000000-0000-4000-8000-00000000000a", "end_now")
    assert ended["type"] == "customer.subscription.deleted" and ended["data"]["object"]["status"] == "canceled"
    with pytest.raises(LookupError):
        sim.complete_checkout("cs_test_sim_1")                     # Paid once only.


def test_the_application_never_references_local_staging():
    for path in CANDIDATE.rglob("*.py"):
        parts = path.relative_to(CANDIDATE).parts
        if parts[0] in ("tests", "tools") or "__pycache__" in parts:
            continue
        assert "local_staging" not in path.read_text(encoding="utf-8", errors="replace"), path
