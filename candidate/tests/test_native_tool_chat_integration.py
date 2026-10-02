import json
import os

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

import app_fastapi_candidate as app


def _message_response(text):
    return {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                    }
                ],
            }
        ]
    }


def _tool_response(name, arguments, call_id):
    return {
        "output": [
            {
                "type": "function_call",
                "name": name,
                "arguments": json.dumps(arguments),
                "call_id": call_id,
            }
        ]
    }


def _last_user_text(input_items):
    for item in reversed(input_items):
        if (
            isinstance(item, dict)
            and item.get("role") == "user"
        ):
            return str(item.get("content", ""))

    return ""


def _last_function_call(input_items):
    for item in reversed(input_items):
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call"
        ):
            return item

    return None


def test_v31_native_tool_chat_behavior(monkeypatch):
    counters = {
        "search": 0,
        "runtime": 0,
    }

    def fake_model(input_items, instructions):
        user_text = _last_user_text(input_items)
        low = user_text.lower()

        previous_call = _last_function_call(input_items)

        if previous_call is not None:
            name = previous_call.get("name")

            if name == "search_web":
                return _message_response(
                    "Here is today's AI news from the search results."
                    "\n\n**Sources**\n\n"
                    "- [Wrong source](https://wrong.example)"
                )

            if name == "get_kalillac_runtime_facts":
                return _message_response(
                    "Kalillac is configured to use GPT-5.6 Luna through "
                    "OpenAI as its primary model, with configured fallbacks. "
                    "The current response contract does not prove which "
                    "provider handled this particular completed response."
                )

        if low.strip() == "web search for me":
            return _message_response(
                "What would you like me to search for?"
            )

        if low.startswith("rewrite this:"):
            return _message_response(
                "The Library update is currently independent of meals "
                "that were logged before the update."
            )

        if "what model is being used right now" in low:
            return _tool_response(
                "get_kalillac_runtime_facts",
                {"topic": "current model configuration"},
                "runtime-1",
            )

        if low.strip() == "ai news today":
            return _tool_response(
                "search_web",
                {"query": "AI news today"},
                "search-1",
            )

        raise AssertionError(
            f"Unexpected model input: {user_text!r}"
        )

    def fake_search(query, include_domains=None):
        counters["search"] += 1

        assert query == "AI news today"

        return (
            "ok",
            [
                {
                    "title": "Example AI News",
                    "url": "https://example.com/ai-news",
                    "published": "2026-10-02",
                    "content": "Networkless integration-test result.",
                }
            ],
        )

    real_runtime_facts = app._v31_runtime_facts

    def counted_runtime_facts():
        counters["runtime"] += 1
        return real_runtime_facts()

    monkeypatch.setattr(
        app,
        "V31_NATIVE_TOOL_ROUTING",
        True,
    )

    monkeypatch.setattr(
        app,
        "_invoke_openai_native_tools",
        fake_model,
    )

    monkeypatch.setattr(
        app,
        "run_web_search",
        fake_search,
    )

    monkeypatch.setattr(
        app,
        "_v31_runtime_facts",
        counted_runtime_facts,
    )

    vague = app.chat(
        "web search for me",
        [],
        session_id="v31-test-vague",
    )

    rewrite = app.chat(
        (
            "Rewrite this: The Library update right now is "
            "independent from meals logged before the update."
        ),
        [],
        session_id="v31-test-rewrite",
    )

    news = app.chat(
        "AI news today",
        [],
        session_id="v31-test-news",
    )

    runtime = app.chat(
        "what model is being used right now",
        [],
        session_id="v31-test-runtime",
    )

    assert vague == "What would you like me to search for?"

    assert "Library update" in rewrite
    assert "**Sources**" not in rewrite

    assert counters["search"] == 1
    assert news.count("**Sources**") == 1
    assert "https://example.com/ai-news" in news
    assert "https://wrong.example" not in news

    assert counters["runtime"] == 1
    assert "GPT-5.6 Luna" in runtime
    assert "particular completed response" in runtime
    assert "**Sources**" not in runtime
