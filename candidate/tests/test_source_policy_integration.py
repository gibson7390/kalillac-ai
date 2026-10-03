import json
import os

os.environ.setdefault("GROQ_API_KEY", "test-not-real")

import app_fastapi_candidate as app


def _tool_response(query):
    return {
        "output": [
            {
                "type": "function_call",
                "name": "search_web",
                "arguments": json.dumps(
                    {"query": query}
                ),
                "call_id": "search-1",
            }
        ]
    }


def _message_response(text="done"):
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


def _fake_model_for(query):
    def fake_model(input_items, instructions):
        for item in reversed(input_items):
            if (
                isinstance(item, dict)
                and item.get("type") == "function_call"
            ):
                return _message_response()

        return _tool_response(query)

    return fake_model


def test_openai_official_query_restricts_tavily(monkeypatch):
    captured = {}

    def fake_search(query, include_domains=None):
        captured["query"] = query
        captured["domains"] = include_domains

        return (
            "ok",
            [
                {
                    "title": "Pricing | OpenAI API",
                    "url": "https://developers.openai.com/api/docs/pricing",
                    "published": None,
                    "content": "Official OpenAI API pricing.",
                }
            ],
        )

    monkeypatch.setattr(
        app,
        "_invoke_openai_native_tools",
        _fake_model_for(
            "current OpenAI API pricing official"
        ),
    )

    monkeypatch.setattr(
        app,
        "run_web_search",
        fake_search,
    )

    app._run_v31_native_tool_chat(
        "Search the web for current OpenAI API pricing",
        [],
        {
            "memory": [],
            "search_times": [],
        },
    )

    assert captured["query"] == (
        "current OpenAI API pricing official"
    )

    assert captured["domains"] == [
        "developers.openai.com",
        "openai.com",
    ]


def test_user_domain_overrides_authoritative_policy(monkeypatch):
    captured = {}

    def fake_search(query, include_domains=None):
        captured["domains"] = include_domains

        return (
            "ok",
            [
                {
                    "title": "Example",
                    "url": "https://example.com/pricing",
                    "published": None,
                    "content": "Example result.",
                }
            ],
        )

    monkeypatch.setattr(
        app,
        "_invoke_openai_native_tools",
        _fake_model_for(
            "current OpenAI API pricing official"
        ),
    )

    monkeypatch.setattr(
        app,
        "run_web_search",
        fake_search,
    )

    app._run_v31_native_tool_chat(
        "Search example.com for current OpenAI API pricing",
        [],
        {
            "memory": [],
            "search_times": [],
        },
    )

    assert captured["domains"] == [
        "example.com",
    ]
