"""Permanent pytest coverage for the engagement / false-positive-refusal
checks that previously lived only inside production code
(app_fastapi_candidate.run_engagement_prompt_tests). The assertions here make
those checks executable through pytest; the production function is kept but is
no longer the only place the policy is asserted.
"""
from __future__ import annotations

import pytest

import app_fastapi_candidate as app
from tests.safety_suite.loader import ANSWERING_ROUTES


def test_system_prompt_engagement_sections_present():
    for marker in ("ENGAGEMENT POLICY:", "canned refusal language", "A sensitive subject may be discussed",
                   "METHODS BOUNDARY:", "LOW-STAKES RULES, TERMS OF SERVICE, AND UNFAIR ADVANTAGE:",
                   "DO NOT INVENT KALILLAC POLICIES:", "CAN VS WON'T:"):
        assert marker in app.SYSTEM_PROMPT, marker


def test_engagement_reminder_is_populated_and_wired():
    assert "do not refuse a request just because" in app.ENGAGEMENT_REMINDER
    assert "operational methods or working code" in app.ENGAGEMENT_REMINDER
    assert "Never invent a broad Kalillac AI policy" in app.ENGAGEMENT_REMINDER
    assert "SAFE ADJACENT ALTERNATIVE:" in app.ENGAGEMENT_REMINDER


@pytest.mark.parametrize("route", ["general", "code"])
def test_each_free_form_route_prompt_includes_the_reminder(route):
    messages = app.build_messages("placeholder request", [], route, [])
    body = messages[1].content
    assert "default to answering legitimate" in body


@pytest.mark.parametrize("category,prompt", app.BENIGN_SENSITIVE_PROMPTS)
def test_benign_sensitive_prompts_route_to_answering(category, prompt):
    assert app.classify_request(prompt, []) in ANSWERING_ROUTES, (category, prompt)


@pytest.mark.parametrize("category,prompt", app.TOS_LOW_STAKES_PROMPTS)
def test_tos_low_stakes_prompts_route_to_answering(category, prompt):
    assert app.classify_request(prompt, []) in ANSWERING_ROUTES, (category, prompt)


@pytest.mark.parametrize("prompt,history", app.INVENTED_POLICY_FOLLOWUPS)
def test_invented_policy_followups_route_to_answering(prompt, history):
    assert app.classify_request(prompt, history) in (ANSWERING_ROUTES | {"followup", "unclear"})


def test_production_engagement_helper_still_passes():
    # The in-process helper is retained; keep it green so both stay in sync.
    assert app.run_engagement_prompt_tests() is True
