"""Provider refusal content on the Responses API, shared by both generation paths.

A Responses API message item may carry ``refusal`` content parts instead of
(or alongside) ``output_text`` parts. Kalillac never shows the provider's raw
refusal text and never treats a refusal as an outage: a refusal with no
visible answer becomes one fixed, normal assistant response, delivered as an
ordinary successful reply. A real provider failure, an empty completion
without any refusal part, and an incomplete answer keep their existing
handling; this module changes none of them.

This module performs no network calls and contains no credentials.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


# The fixed reply for a provider refusal that left no visible answer text.
# Wording is a product decision; it names a boundary and offers the safe
# approaches without describing an outage or inviting an automatic retry.
REFUSAL_RESPONSE = (
    "Kalillac can’t help with that request as written. It can still help "
    "with prevention, analysis, defensive guidance, or another safe approach."
)


def output_has_refusal(output: Iterable[Any]) -> bool:
    """True when any Responses API message item carries a ``refusal`` part.

    Only well-formed message items are inspected; anything else (reasoning
    items, function calls, malformed entries) is ignored here and handled by
    the caller's own output contract.
    """
    for item in output or ():
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "refusal":
                return True
    return False


def resolve_visible_text(text: str, refused: bool) -> str:
    """The assistant-visible text for one completed model turn.

    No refusal part: the text is returned unchanged (an empty text stays
    empty, so the existing unusable-output handling still applies).

    Any refusal part: the complete visible model output for that turn is
    DISCARDED and replaced by exactly REFUSAL_RESPONSE. Partial text before
    or after the refusal, and any accumulated continuation text, never
    survive, and the provider's raw refusal text is never copied.
    """
    text = str(text or "")
    if not refused:
        return text
    return REFUSAL_RESPONSE
