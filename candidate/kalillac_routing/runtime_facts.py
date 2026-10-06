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

    web_search_provider: str = "Tavily"


def build_runtime_facts(
    config: RuntimeConfig,
) -> dict[str, Any]:
    """Return safe facts about Kalillac's configured runtime.

    Kalillac uses one configured model provider and never falls back to
    another model or provider automatically. Configuration still does not
    prove which provider handled a particular completed response.
    """

    return {
        "configured_primary": {
            "provider": config.primary_provider,
            "model": config.primary_model,
            "reasoning_effort": config.reasoning_effort,
        },
        "configured_fallback_chain": [],
        "automatic_model_fallback": False,
        "model_unavailable_behavior": (
            "If the configured model provider cannot produce a usable answer, "
            "the request ends with a temporary model-provider-unavailable "
            "error instead of an answer from a different model."
        ),
        "web_search_provider": config.web_search_provider,
        "web_search_provider_role": (
            "Used only when live web search runs; it does not generate answers."
        ),
        "per_message_provider_identity_available": False,
        "per_message_provider_note": (
            "Kalillac's configured model path is a single provider, but its "
            "response contract does not yet preserve metadata that proves "
            "which provider handled a particular completed response."
        ),
    }
