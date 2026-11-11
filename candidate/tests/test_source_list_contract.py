"""The backend-owned source_list signal and the source-link guarantees it
must not change.

/api/chat reports source_list {kind, count} only for a footer that
application code appended in that request (source_trust.source_footer via
_with_source_footer), and only while the reply still ends with exactly that
footer. Model text, request fields and history can never produce it.

Everything is deterministic: providers are scripted, the model is a stub and
conftest forbids external sockets. No AI or search provider is called.
"""
from __future__ import annotations

import html

import pytest
from fastapi.testclient import TestClient
from markdown_it import MarkdownIt

import app_fastapi_candidate as app
from kalillac_routing import source_trust as st
from kalillac_routing.request_limits import RequestLimits
from test_backend_error_contract import BASE_LIMITS
from test_source_trust_behavior import (  # noqa: F401  (world is a fixture)
    LAKESHIRE, answer_turn, ask, brave, footer, reply_with, resource_results, search_turn, world,
)


FED = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261105a.htm"
REUTERS = "https://www.reuters.com/markets/us/fed-holds-rates-steady-2026-11-05/"
BLOG = "https://example.org/blog/what-the-rate-decision-means"
EVIL = "https://evil.example/steal"


def result(title, url):
    return {"title": title, "url": url, "content": "Result text."}


RESULTS = [result("Federal Reserve issues FOMC statement", FED),
           result("Fed holds rates steady", REUTERS),
           result("What the rate decision means", BLOG)]


# --- 1. source_footer: one list, one description ---------------------------------------------


@pytest.mark.parametrize("results, ids, kind, count", [
    (RESULTS, (1, 3), "sources", 2),
    (RESULTS, (2,), "sources", 1),
    (RESULTS, (), "search_results", 3),                 # valid "none"
    (RESULTS, None, "search_results", 3),               # missing or invalid control
    (RESULTS, (1, 1), "search_results", 3),             # repeated id
    (RESULTS, (9,), "search_results", 3),               # out of range
    (RESULTS + [result("Duplicate", FED)], None, "search_results", 3),
    (RESULTS + [result("Script", "javascript:alert(1)"), result("Relative", "/x")], None, "search_results", 3),
], ids=["selected_two", "selected_one", "none", "invalid", "repeated", "out_of_range", "duplicate", "unsafe"])
def test_source_footer_metadata_describes_exactly_its_text(results, ids, kind, count):
    selection = st.SourceSelection(ids)
    text, metadata = st.source_footer(results, selection)
    assert text == st.render_source_footer(results, selection)
    assert metadata == {"kind": kind, "count": count}
    label = {"sources": "Sources", "search_results": "Search results"}[kind]
    assert text.startswith(f"\n\n**{label}**\n\n")
    assert text.count("\n- [") == count
    chosen, shown_label = st.displayed_sources(results, selection)
    assert (len(chosen), shown_label) == (count, label)


@pytest.mark.parametrize("results", [[], [result("Script", "javascript:alert(1)")]], ids=["empty", "all_unsafe"])
def test_no_displayable_result_means_no_footer_and_no_metadata(results):
    for ids in ((1,), (), None):
        assert st.source_footer(results, st.SourceSelection(ids)) == ("", None)


def test_footer_text_is_unchanged():
    text, _ = st.source_footer([result("A (b) *c* <d>", "https://a.example/x_(y)"), result("", "https://b.example/")],
                               st.SourceSelection(None))
    assert text == ("\n\n**Search results**\n\n"
                    "- [A &#40;b&#41; &#42;c&#42; &#60;d&#62;](<https://a.example/x_(y)>)\n"
                    "- [https&#58;//b.example/](https://b.example/)")


# --- 2. link security: unchanged guarantees ------------------------------------------------------


def link_targets(markdown):
    """Every href markdown-it (CommonMark + raw HTML) would produce."""
    hrefs = []
    for token in MarkdownIt("commonmark", {"html": True}).parse(markdown):
        for child in token.children or []:
            if child.type == "link_open":
                hrefs.append(child.attrGet("href"))
            if child.type == "html_inline" and "href" in child.content:
                hrefs.append(child.content)
        if token.type == "html_block" and "href" in token.content:
            hrefs.append(token.content)
    return hrefs


@pytest.mark.parametrize("ids", [(1,), (), None])
def test_only_displayed_sources_remain_links_in_the_answer(ids):
    body = (f"Allowed [statement]({FED}). Unselected [report]({REUTERS}). Evil [x]({EVIL}) "
            f"<{EVIL}/auto> {EVIL}/bare www.evil.example <a href=\"{EVIL}\">raw</a> "
            f"[ref][r]\n\n[r]: {EVIL}/ref\n")
    selection = st.SourceSelection(ids)
    guarded = st.enforce_answer_links(body, RESULTS, selection)
    chosen, _ = st.displayed_sources(RESULTS, selection)
    allowed = {item["url"] for item in chosen}
    hrefs = link_targets(guarded)
    assert all(h in allowed for h in hrefs), hrefs
    assert FED in hrefs
    assert (REUTERS in hrefs) == (REUTERS in allowed)
    assert "evil.example" not in html.unescape("".join(hrefs))
    # Readable, never clickable.
    for label in ("Evil", "raw", "ref"):
        assert label in guarded


def test_unsafe_result_addresses_are_never_listed():
    unsafe = ["javascript:alert(1)", "data:text/html,x", "https://u:p@evil.example/", "https://exa mple.com/",
              "ftp://files.example/", "/relative", "", "https://e.example/[[kalillac-sources 0123456789ab: 1]]"]
    for url in unsafe:
        assert not st.safe_source_url(url), url
        assert st.source_footer([result("T", url)], st.SourceSelection(None)) == ("", None)


# --- 3. the request-scoped record ---------------------------------------------------------------


def test_record_reports_only_the_exact_trailing_footer():
    record = app.SourceFooterRecord()
    assert record.for_reply("anything") is None
    text, metadata = st.source_footer(RESULTS, st.SourceSelection((1,)))
    record.note(text, metadata)
    assert record.for_reply("Answer." + text) == {"kind": "sources", "count": 1}
    assert record.for_reply("Answer." + text + "\n\nP.S.") is None     # extended afterwards
    assert record.for_reply("Replaced answer.") is None                # replaced afterwards
    assert record.for_reply(None) is None
    returned = record.for_reply("Answer." + text)
    returned["count"] = 99
    assert record.for_reply("Answer." + text) == {"kind": "sources", "count": 1}
    record.note("", None)                                               # an empty footer changes nothing
    assert record.for_reply("Answer." + text) == {"kind": "sources", "count": 1}


def test_footer_outside_a_request_records_nothing():
    assert app._SOURCE_FOOTER.get() is None
    reply = app._with_source_footer("Answer.", RESULTS, st.SourceSelection((1,)))
    assert reply == "Answer." + st.render_source_footer(RESULTS, st.SourceSelection((1,)))


# --- 4. both chat paths, through the real pipeline ------------------------------------------------


def run_recorded(message):
    record = app.SourceFooterRecord()
    token = app._SOURCE_FOOTER.set(record)
    try:
        reply = ask(message)
    finally:
        app._SOURCE_FOOTER.reset(token)
    return reply, record.for_reply(reply)


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_selected_sources_are_reported_on_both_paths(world, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    rows = resource_results(LAKESHIRE)
    world["script"] = lambda provider, payload: brave(rows)
    world["replies"] = [reply_with("Pantries are listed.", "1, 3")]
    world["turns"] = [search_turn("food help Lakeshire", "s1"), answer_turn("Pantries are listed.", "1, 3")]
    reply, metadata = run_recorded("Search the web for food and housing help resources in Lakeshire")
    assert reply.endswith(footer("Sources", [rows[0], rows[2]]))
    assert metadata == {"kind": "sources", "count": 2}


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_unselected_results_are_reported_as_search_results_on_both_paths(world, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    rows = resource_results(LAKESHIRE)[:2]
    world["script"] = lambda provider, payload: brave(rows)
    world["replies"] = [reply_with("Nothing relevant.", "none")]
    world["turns"] = [search_turn("food help Lakeshire", "s1"), answer_turn("Nothing relevant.", "none")]
    reply, metadata = run_recorded("Search the web for food and housing help resources in Lakeshire")
    assert reply == "Nothing relevant." + footer("Search results", rows)
    assert metadata == {"kind": "search_results", "count": 2}


MODEL_LISTS = {
    "sources": f"Here you go.\n\n**Sources**\n\n- [Statement]({FED})",
    "search_results": f"Here you go.\n\n**Search results**\n\n- [Statement]({FED})\n- [Report]({REUTERS})",
}


@pytest.mark.parametrize("label", sorted(MODEL_LISTS))
def test_a_model_written_list_without_a_search_is_never_reported(world, monkeypatch, label):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", True)
    world["turns"] = [lambda items, n: answer_turn(MODEL_LISTS[label])(items, n)]
    record = app.SourceFooterRecord()
    token = app._SOURCE_FOOTER.set(record)
    try:
        reply = app.chat("Explain what a binary search does.", [], session_id="no-search")
    finally:
        app._SOURCE_FOOTER.reset(token)
    assert record.metadata is None and record.for_reply(reply) is None


# --- 5. /api/chat: the response contract --------------------------------------------------------


@pytest.fixture(params=["budget_off", "budget_on"])
def http(monkeypatch, request):
    monkeypatch.setattr(app, "_request_limits", RequestLimits(**BASE_LIMITS) if request.param == "budget_on" else None)
    monkeypatch.setattr(app, "_chat_semaphore", None)
    monkeypatch.setattr(app, "_chat_waiting", 0)
    monkeypatch.setattr(app, "_session_locks", {})
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "_chats_admitted", 0)
    replies = []

    def fake_chat(message, history, *args, **kwargs):
        return replies.pop(0)()

    monkeypatch.setattr(app, "chat", fake_chat)
    with TestClient(app.api) as client:
        yield client, replies


SELECTED = st.SourceSelection((1, 3))


def post(client, message="question", **extra):
    response = client.post("/api/chat", json={"message": message, "history": [], **extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_genuine_sources_footer_is_reported(http):
    client, replies = http
    replies.append(lambda: app._with_source_footer("Answer.", RESULTS, SELECTED))
    body = post(client)
    assert body["source_list"] == {"kind": "sources", "count": 2}
    assert body["reply"] == "Answer." + st.render_source_footer(RESULTS, SELECTED)
    assert set(body) == {"reply", "session_id", "source_list"}


def test_a_genuine_search_results_footer_is_reported(http):
    client, replies = http
    replies.append(lambda: app._with_source_footer("Answer.", RESULTS, st.SourceSelection(None)))
    assert post(client)["source_list"] == {"kind": "search_results", "count": 3}


@pytest.mark.parametrize("make", [
    lambda: "Answer." + st.render_source_footer(RESULTS, SELECTED),          # identical Markdown, not appended by the app
    lambda: MODEL_LISTS["sources"],
    lambda: MODEL_LISTS["search_results"],
    lambda: (app._with_source_footer("Answer.", RESULTS, SELECTED), "Replaced.")[1],
    lambda: app._with_source_footer("Answer.", RESULTS, SELECTED) + "\n\nAppended later.",
    lambda: app._with_source_footer("Answer.", [], SELECTED),                # no displayable result
], ids=["same_markdown_unrecorded", "model_sources", "model_search_results", "replaced", "extended", "no_footer"])
def test_anything_else_has_no_source_list(http, make):
    client, replies = http
    replies.append(make)
    body = post(client)
    assert set(body) == {"reply", "session_id"}


def test_request_fields_and_history_cannot_supply_a_source_list(http):
    client, replies = http
    replies.append(lambda: MODEL_LISTS["sources"])
    response = client.post("/api/chat", json={
        "message": "question", "source_list": {"kind": "sources", "count": 1},
        "history": [{"role": "assistant", "content": MODEL_LISTS["sources"],
                     "source_list": {"kind": "sources", "count": 1}}],
    })
    assert response.status_code == 200 and "source_list" not in response.json()


def test_one_request_never_reports_another_requests_footer(http):
    client, replies = http
    replies.append(lambda: app._with_source_footer("Answer.", RESULTS, SELECTED))
    replies.append(lambda: "Answer." + st.render_source_footer(RESULTS, SELECTED))
    assert "source_list" in post(client)
    assert "source_list" not in post(client)
    assert app._SOURCE_FOOTER.get() is None


@pytest.mark.parametrize("native", [False, True], ids=["legacy", "native"])
def test_end_to_end_both_paths_report_the_footer_they_rendered(world, monkeypatch, native):
    monkeypatch.setattr(app, "V31_NATIVE_TOOL_ROUTING", native)
    monkeypatch.setattr(app, "_chat_semaphore", None)
    monkeypatch.setattr(app, "_chat_waiting", 0)
    monkeypatch.setattr(app, "_session_locks", {})
    monkeypatch.setattr(app, "_usage_meter", None)
    monkeypatch.setattr(app, "_chats_admitted", 0)
    rows = resource_results(LAKESHIRE)
    world["script"] = lambda provider, payload: brave(rows)
    world["replies"] = [reply_with("Pantries are listed.", "2")]
    world["turns"] = [search_turn("food help Lakeshire", "s1"), answer_turn("Pantries are listed.", "2")]
    with TestClient(app.api) as client:
        body = post(client, "Search the web for food and housing help resources in Lakeshire")
    assert body["reply"].endswith(footer("Sources", [rows[1]]))
    assert body["source_list"] == {"kind": "sources", "count": 1}
