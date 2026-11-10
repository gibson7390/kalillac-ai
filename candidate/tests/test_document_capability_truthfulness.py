"""Document capability truthfulness.

Kalillac can draft and format document content in the chat. It cannot create,
attach, send or export a file, give a download link, or receive uploads.
Downloadable documents and document uploads are planned, not current.

Every model call carries one short universal sentence forbidding fabricated
files or links. The detailed facts and guidance live in one authoritative
block, DOCUMENT_CAPABILITY_CONTEXT, which reaches the legacy and native paths
only for requests about documents, files, or Kalillac's plans, and for short
follow-ups to such a request. Upload claims in this chat keep their
deterministic answer.

The model output is scripted: these tests prove what reaches the model and
which routes are taken, not how GPT-6 Luna words its reply.
"""

from __future__ import annotations

import pytest

import app_fastapi_candidate as app
from test_self_knowledge_voice import chat, completed, pipeline, prompt  # noqa: F401  (fixture)


UNIVERSAL = ("Kalillac cannot create, attach, or export files or give download links; never claim that one "
             "was created.")
BLOCK = app.DOCUMENT_CAPABILITY_CONTEXT
BLOCK_HEADER = "KALILLAC DOCUMENT CAPABILITIES - AUTHORITATIVE:"
NO_RESTATE = "When the task is already clear, do not ask them to restate it."
UPLOAD_REPLY = ("This chat doesn't support file uploads or attachments, so I can't receive or open files here. "
                "Paste the relevant text instead. If you already said what you want done, you don't need to "
                "repeat it.")

DOCUMENT_REQUESTS = [
    "Can you produce documents?",
    "Can you make Word documents?",
    "Can you generate a PDF?",
    "Can you create spreadsheets?",
    "Do you support documents?",
    "Send me a document of your choice.",
    "Create a report I can download.",
    "Attach a résumé as a Word document.",
    "Export this as a PDF.",
    "Give me the download link.",
    "Are document uploads planned?",
]
UNRELATED_REQUESTS = [
    "Explain recursion.",
    "Help me learn cybersecurity.",
    "What is the latest Python release?",
]
WRITING_REQUESTS = [
    "Write a professional email asking for an extension.",
    "Draft a resignation letter.",
    "Write a business proposal.",
    "Format these meeting notes: met with Sam, budget ok, ship Friday",
    "Rewrite this without losing my voice.",
    "Write API documentation for this function.",
]
DRAFTED_REPORT = [
    {"role": "user", "content": "Draft a professional report in the chat."},
    {"role": "assistant", "content": "# Quarterly Report\n\n## Summary\nRevenue grew 8% while costs held flat."},
]
CAPABILITY_ANSWERED = [
    {"role": "user", "content": "Can you produce documents?"},
    {"role": "assistant", "content": "I can write the document content here; I can't create a downloadable file yet."},
]


def normalized(state):
    return " ".join(prompt(state).split())


# --- the authoritative block --------------------------------------------------------------------


def test_the_block_states_current_limits_plans_and_response_guidance():
    block = " ".join(BLOCK.split())
    assert "Current: Kalillac drafts, structures, and formats document content in this chat" in block
    assert ("Not current: it cannot create, attach, send, or export a file (PDF, Word/DOCX, spreadsheet/XLSX), "
            "cannot give a download link, and cannot receive uploaded files.") in block
    assert ("Never claim or imply that a file, attachment, export, download, link, upload, or saved path exists "
            "or was created, or that an uploaded file was read.") in block
    assert "Planned, not launched: downloadable PDF, DOCX, and XLSX creation" in block
    assert "No launch date, price, tier, limit, or storage term is established" in block
    assert "an account or subscription will not by itself save conversations or documents" in block
    assert ("say plainly that you can write the content here and cannot yet create a downloadable file. "
            "A sample is optional.") in block
    assert ("state the limitation in one sentence, then deliver the complete document in the chat with the "
            "structure its type calls for, not a placeholder or a description of what you could write.") in block
    assert "Ask only for details that materially change the document." in block
    assert "Writing requests that do not ask for a file need no file remark." in block


def test_the_block_is_the_single_source_of_document_plans():
    assert "document" not in app.render_kalillac_product_roadmap().lower()
    assert "Planned, not launched" not in app.render_kalillac_facts()
    assert "downloadable file" not in app.CURRENT_INTERFACE_CAPABILITIES


def test_no_instruction_authorizes_a_fabricated_file():
    for text in (app.CURRENT_INTERFACE_CAPABILITIES, BLOCK):
        lowered = " ".join(text.split()).lower()
        for invented in ["you can download", "here is your file", "i attached", "saved to", "download it here"]:
            assert invented not in lowered
    assert UNIVERSAL in " ".join(app.CURRENT_INTERFACE_CAPABILITIES.split())


# --- conditional delivery -----------------------------------------------------------------------


@pytest.mark.parametrize("message", DOCUMENT_REQUESTS)
def test_document_requests_receive_the_block_once_in_both_paths(pipeline, message):
    assert app.classify_request(message, []) == "general"
    assert app.document_capability_context_active(message, [])
    pipeline["text"] = "Scripted reply."
    assert chat(pipeline, message) == "Scripted reply."
    sent = prompt(pipeline)
    assert sent.count(BLOCK) == 1
    assert " ".join(sent.split()).count(UNIVERSAL) == 1
    assert "ABSOLUTE OUTPUT RULE" not in sent
    assert len(pipeline["payloads"]) == 1


@pytest.mark.parametrize("message", UNRELATED_REQUESTS + WRITING_REQUESTS)
def test_unrelated_and_writing_requests_do_not_receive_the_block(pipeline, message):
    assert not app.document_capability_context_active(message, [])
    chat(pipeline, message)
    sent = prompt(pipeline)
    assert BLOCK_HEADER not in sent
    assert "Planned, not launched" not in sent
    normalized_sent = " ".join(sent.split())
    assert normalized_sent.count(UNIVERSAL) == 1
    assert NO_RESTATE in normalized_sent
    assert "ask the user to paste the relevant content and state what they want done" not in normalized_sent


def test_the_block_text_is_identical_in_both_paths(pipeline):
    chat(pipeline, "Send me a document of your choice.")
    payload = pipeline["payloads"][-1]
    if pipeline["native"]:
        assert BLOCK in payload["instructions"]
        assert payload["instructions"].startswith(app.CURRENT_INTERFACE_CAPABILITIES)
    else:
        assert payload["input"][0]["content"] == app.CURRENT_INTERFACE_CAPABILITIES
        assert BLOCK in "\n".join(item["content"] for item in payload["input"][1:])


def test_a_broad_kalillac_plans_question_reports_document_plans(pipeline):
    message = "What is planned for Kalillac?"
    assert app.classify_request(message, []) == "self_knowledge"
    chat(pipeline, message)
    assert prompt(pipeline).count(BLOCK) == 1


# --- conversation follow-ups --------------------------------------------------------------------


@pytest.mark.parametrize("message,history,route,retained", [
    ("Send me one.", CAPABILITY_ANSWERED, "general", "I can't create a downloadable file yet."),
    ("Now make it downloadable.", DRAFTED_REPORT, "followup", "Revenue grew 8% while costs held flat."),
    ("Export that as a PDF.", DRAFTED_REPORT, "followup", "Revenue grew 8% while costs held flat."),
])
def test_a_document_follow_up_keeps_its_context_and_the_block(pipeline, message, history, route, retained):
    assert app.classify_request(message, history) == route
    assert app.document_capability_context_active(message, history)
    chat(pipeline, message, history)
    sent = prompt(pipeline)
    assert retained in " ".join(sent.split())
    assert sent.count(BLOCK) == 1


def test_a_short_follow_up_to_an_unrelated_topic_does_not_receive_the_block():
    history = [{"role": "user", "content": "Tell me a joke."}, {"role": "assistant", "content": "Why did..."}]
    assert not app.document_capability_context_active("Send me one.", history)


@pytest.mark.parametrize("message", [
    "Send me one.", "Make it downloadable.", "Export that as a PDF.", "Give me the download link.",
    "Put it in Word format.", "Can you attach it?", "When will that launch?", "What will it cost?",
    "Which plan will include it?", "Will uploads be private?",
])
def test_a_document_continuation_inherits_the_block(message):
    assert app.document_capability_context_active(message, CAPABILITY_ANSWERED)


@pytest.mark.parametrize("message", [
    "Thanks, that helps.", "Okay.", "Understood.", "That looks good.", "Now explain recursion.",
    "Tell me a joke.", "What is 2 + 2?",
])
def test_an_acknowledgment_or_new_topic_does_not_inherit_the_block(message):
    assert not app.document_capability_context_active(message, CAPABILITY_ANSWERED)


def test_an_acknowledgment_reaches_the_model_without_the_block(pipeline):
    chat(pipeline, "Thanks, that helps.", CAPABILITY_ANSWERED)
    assert BLOCK_HEADER not in prompt(pipeline)


@pytest.mark.parametrize("message", ["When will that launch?", "What will it cost?"])
def test_a_planned_feature_follow_up_receives_the_authoritative_facts(pipeline, message):
    chat(pipeline, message, CAPABILITY_ANSWERED)
    sent = prompt(pipeline)
    assert sent.count(BLOCK) == 1
    assert "No launch date, price, tier, limit, or storage term is established" in " ".join(sent.split())


# --- Kalillac capability questions versus the user's own software ----------------------------------


@pytest.mark.parametrize("message", [
    "Can your app export PDFs?",
    "Can Kalillac generate DOCX files?",
    "Does your application support Word exports?",
    "Can this chat create an Excel file?",
    "Can you make downloadable reports?",
    "Does Kalillac support document uploads?",
])
def test_a_capability_question_about_kalillac_activates_the_block(pipeline, message):
    assert app.document_capability_context_active(message, [])
    chat(pipeline, message)
    assert prompt(pipeline).count(BLOCK) == 1


@pytest.mark.parametrize("message", [
    "How do I add PDF export to my app?",
    "Help my users download reports from a Django application.",
    "Write code that generates a DOCX file.",
    "Which Python library should I use to create PDFs?",
    "How should my backend store uploaded documents?",
])
def test_a_question_about_the_users_own_software_does_not_activate_the_block(message):
    assert not app.document_capability_context_active(message, [])


# --- natural file-output phrasings ---------------------------------------------------------------


@pytest.mark.parametrize("message", [
    "Put this in Word format.",
    "Make this a Word file.",
    "Save this as a PDF.",
    "Turn this into a downloadable document.",
    "Give me something I can download.",
    "Export the answer to Excel.",
    "Make a downloadable version of this.",
])
def test_a_natural_file_output_request_receives_the_block(pipeline, message):
    assert app.classify_request(message, []) == "general"
    assert app.document_capability_context_active(message, [])
    chat(pipeline, message)
    assert prompt(pipeline).count(BLOCK) == 1


def test_saving_as_a_file_is_not_a_memory_write():
    assert not app.is_memory_save_request("Save this as a PDF.")
    for message in ["Save this: my dog's name is Comet.", "Save this to memory: I prefer metric units.",
                    "Store this: my favorite color is blue."]:
        assert app.is_memory_save_request(message)
        assert app.classify_request(message, []) == "memory_save"


@pytest.mark.parametrize("message", [
    "Format this letter professionally.",
    "Make this report clearer.",
    "Write API documentation.",
    "Create a printable checklist in the chat.",
])
def test_formatting_and_printable_content_do_not_activate_the_block(message):
    assert not app.document_capability_context_active(message, [])


# --- web search -----------------------------------------------------------------------------------


SEARCH_RESULTS = ("ok", [{"title": "Guidance", "url": "https://example.org/guidance",
                          "content": "Use long passphrases and a password manager.", "published": None}])


@pytest.fixture
def scripted_search(monkeypatch):
    calls = []

    def search(*args, **kwargs):
        calls.append(args)
        return SEARCH_RESULTS

    monkeypatch.setattr(app, "run_web_search", search)
    return calls


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
@pytest.mark.parametrize("message,blocks", [
    ("Search for current cybersecurity statistics and export the report as a Word document.", 1),
    ("Search for the latest PDF specification.", 0),
    ("Search for examples of PDF export in Django.", 0),
])
def test_a_search_payload_carries_the_block_only_when_a_file_is_asked_for(
        monkeypatch, scripted_search, native, message, blocks):
    payloads = []
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    monkeypatch.setattr(app, "OPENAI_API_KEY", "offline-model-key")
    monkeypatch.setattr(app, "_request_limits", None)
    monkeypatch.setattr(app, "SESSION_STATE", type(app.SESSION_STATE)())
    monkeypatch.setattr(app, "_post_openai_responses",
                        lambda payload, timeout=90: payloads.append(payload) or completed("Scripted."))
    monkeypatch.setattr(app.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    assert app.classify_request(message, []) == "web_search"
    app.chat(message, [], session_id="doc-search")
    payload = payloads[-1]
    sent = payload.get("instructions", "") + "\n" + "\n".join(
        item["content"] for item in payload["input"] if isinstance(item.get("content"), str))
    assert sent.count(BLOCK) == blocks
    if not native:                                                      # the legacy search still runs
        assert len(scripted_search) == 1
        assert "https://example.org/guidance" in sent


@pytest.mark.parametrize("message,route,private", [
    ("Research current password-security guidance and create a PDF report I can download.", "general", True),
    ("Search for current cybersecurity statistics and export the report as a Word document.", "web_search", True),
    ("Research this topic and give me a downloadable report.", "general", False),
    ("Find official Python documentation.", "general", False),
    ("Research libraries that generate DOCX files.", "general", True),
    ("Search for examples of PDF export in Django.", "web_search", True),
])
def test_search_requests_keep_their_head_routes_and_privacy_flags(message, route, private):
    assert app.classify_request(message, []) == route
    assert app.is_file_reference_request(message, [], resolve_supplied=False) is private


# --- routes, privacy and code are unchanged -----------------------------------------------------


@pytest.mark.parametrize("message,route,private", [
    ("Document the function.", "general", False),
    ("Add documentation to this code.", "general", False),
    ("Create an HTML document.", "code", False),
    ("How do I generate a PDF in Python?", "code", False),
    ("Write Python code that creates a DOCX file.", "code", False),
    ("Search for the latest PDF specification.", "web_search", True),
    ("Find the official documentation for FastAPI.", "general", False),
    ("The spreadsheet I uploaded to Google Drive has errors.", "web_search", False),
    ("How do I let users download files in my Django application?", "general", False),
    ("I attached the file to the email.", "general", True),
    ("Explain the documents I attached to my application.", "general", True),
    ("Search the web for the report I attached to my email.", "web_search", False),
    ("What do the files I uploaded to S3 cost per month?", "general", True),
    ("How do I let users download the files I attached in my Django app?", "general", True),
    ("Write a cover letter for the job I applied to.", "general", False),
])
def test_code_search_and_other_destination_requests_keep_their_head_behavior(message, route, private):
    assert app.classify_request(message, []) == route
    assert app.is_file_reference_request(message, [], resolve_supplied=False) is private
    assert not app.document_capability_context_active(message, [])
    assert app.file_attachment_claim_scope(message) != "current"


def test_code_requests_never_receive_the_block():
    messages = app.build_messages("How do I generate a PDF in Python?", [], "code", [])
    assert BLOCK_HEADER not in "\n".join(m.content for m in messages)


# --- upload claims keep the deterministic answer ------------------------------------------------


@pytest.mark.parametrize("message", [
    "I uploaded a PDF. Summarize it.",
    "Compare the two documents I attached.",
    "Summarize the file I uploaded.",
    "Read the PDF we attached.",
])
def test_an_upload_claim_gets_the_deterministic_no_upload_answer(pipeline, message):
    assert app.classify_request(message, []) == "file_unavailable"
    reply = chat(pipeline, message)
    assert reply == UPLOAD_REPLY
    assert "that file" not in reply                                     # reads naturally for one or several files
    assert "http" not in reply and "download" not in reply.lower()
    assert pipeline["payloads"] == []                                   # no model call, nothing fabricated
