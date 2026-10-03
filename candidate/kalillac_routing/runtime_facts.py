"""Safe authoritative runtime facts for Kalillac V31.

This module performs no network calls and contains no credentials.

It exposes only application facts that are safe for the model to use when
answering questions about Kalillac's own configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeConfig:
    primary_provider: str
    primary_model: str
    reasoning_effort: str

    first_fallback_provider: str
    first_fallback_model: str

    second_fallback_provider: str
    second_fallback_model: str

    final_fallback_provider: str
    final_fallback_model: str

    web_search_provider: str = "Tavily"


def build_runtime_facts(
    config: RuntimeConfig,
) -> dict[str, Any]:
    """Return safe facts about Kalillac's configured runtime.

    Important distinction:
    configuration tells us the order Kalillac is designed to attempt.
    It does not prove which provider handled a particular completed response.
    """

    return {
        "configured_primary": {
            "provider": config.primary_provider,
            "model": config.primary_model,
            "reasoning_effort": config.reasoning_effort,
        },
        "configured_fallback_chain": [
            {
                "provider": config.first_fallback_provider,
                "model": config.first_fallback_model,
            },
            {
                "provider": config.second_fallback_provider,
                "model": config.second_fallback_model,
            },
            {
                "provider": config.final_fallback_provider,
                "model": config.final_fallback_model,
            },
        ],
        "web_search_provider": config.web_search_provider,
        "per_message_provider_identity_available": False,
        "per_message_provider_note": (
            "Kalillac currently knows its configured primary and fallback "
            "order, but its response contract does not yet preserve metadata "
            "that proves which provider handled a particular completed "
            "response."
        ),
    }