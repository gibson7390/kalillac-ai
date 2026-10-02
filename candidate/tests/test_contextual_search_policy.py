import os

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

import app_fastapi_candidate as app


def test_native_policy_supports_contextual_search_followups():
    policy = app.V31_NATIVE_TOOL_POLICY

    assert "recent conversation context" in policy
    assert '"do web search"' in policy
    normalized = " ".join(policy.split())
    assert "exactly one clear active topic" in normalized


def test_native_policy_honors_explicit_runtime_web_search():
    policy = app.V31_NATIVE_TOOL_POLICY

    assert (
        "explicitly requests a public-web search after discussing"
        in policy
    )

    assert (
        "public documentation from authoritative local runtime facts"
        in policy
    )

    assert (
        "public search can prove which provider handled"
        in policy
    )
