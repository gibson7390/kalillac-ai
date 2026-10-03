from kalillac_routing.source_policy import (
    OPENAI_OFFICIAL_DOMAINS,
    get_authoritative_search_domains,
)


def test_openai_pricing_official_query_uses_first_party_domains():
    domains = get_authoritative_search_domains(
        "Search the web for current OpenAI API pricing",
        "current OpenAI API pricing official",
    )

    assert domains == list(OPENAI_OFFICIAL_DOMAINS)


def test_openai_responses_api_official_query_uses_first_party_domains():
    domains = get_authoritative_search_domains(
        "What is the latest information about the OpenAI Responses API?",
        "OpenAI Responses API latest official documentation updates",
    )

    assert domains == [
        "developers.openai.com",
        "openai.com",
    ]


def test_openai_news_without_official_intent_remains_broad():
    domains = get_authoritative_search_domains(
        "What is the latest OpenAI news?",
        "OpenAI latest news today",
    )

    assert domains == []


def test_unrelated_user_request_cannot_be_redirected_by_model_query():
    domains = get_authoritative_search_domains(
        "AI news today",
        "OpenAI official AI news today",
    )

    assert domains == []


def test_other_vendor_official_query_is_not_inferred():
    domains = get_authoritative_search_domains(
        "Find current Microsoft API pricing",
        "Microsoft API pricing official",
    )

    assert domains == []
