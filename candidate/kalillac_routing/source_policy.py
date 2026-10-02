"""Deterministic authoritative-source policy for native web search."""

OPENAI_OFFICIAL_DOMAINS = (
    "developers.openai.com",
    "openai.com",
)


def get_authoritative_search_domains(
    user_message: str,
    model_query: str,
) -> list[str]:
    """Return narrow first-party domain constraints when justified.

    This function never decides whether web search should run. Luna already
    made that decision. It only constrains source provenance for a search
    whose query explicitly requests official first-party information.

    The user message and model query must both identify OpenAI. Requiring both
    prevents a model-generated query from silently changing an unrelated
    request into an OpenAI-specific search.
    """

    user_text = str(user_message or "").lower()
    query_text = str(model_query or "").lower()

    if "openai" not in user_text:
        return []

    if "openai" not in query_text:
        return []

    if "official" not in query_text:
        return []

    return list(OPENAI_OFFICIAL_DOMAINS)
