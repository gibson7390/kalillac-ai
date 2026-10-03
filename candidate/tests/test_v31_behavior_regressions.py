import os

# This test must be runnable by itself. These are inert test-only values;
# no network calls are made by these regression tests.
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("TAVILY_API_KEY", "test-tavily-key")
os.environ.setdefault("CLOUDFLARE_API_TOKEN", "test-cloudflare-token")
os.environ.setdefault("CLOUDFLARE_ACCOUNT_ID", "test-cloudflare-account")

import app_fastapi_candidate as app


BASIC_HTML = "produce very basic html code"

KALILLAC_ROUTER = (
    "if someone were to build their own ai, use gpt 5.6 luna as the model, "
    "and they asked you to build the perfect router that would fit kalillac "
    "ai perfectly. show me how the full router would look"
)


def test_basic_html_is_code():
    assert app.classify_request(BASIC_HTML, []) == "code"


def test_kalillac_router_is_grounded_code():
    assert app.is_kalillac_code_reference_request(KALILLAC_ROUTER)
    assert app.classify_request(KALILLAC_ROUTER, []) == "code"


def test_v31_runtime_facts_include_architecture():
    facts = app._v31_runtime_facts()

    assert facts["routing_mode"] == "transitional_v31"

    handling = facts["request_handling"]

    assert handling["legacy_classifier_gate"] is True
    assert "search_web" in handling["native_tool_path"]["tools"]
    assert "get_kalillac_runtime_facts" in handling["native_tool_path"]["tools"]

    assert (
        handling["native_tool_path"]["model"]
        == app.OPENAI_MODEL
    )


def test_native_policy_guards_behavior():
    policy = app.V31_NATIVE_TOOL_POLICY

    assert "capability question" in policy
    assert "Do not volunteer the creator" in policy
    assert "Do not reconstruct Kalillac's architecture" in policy
