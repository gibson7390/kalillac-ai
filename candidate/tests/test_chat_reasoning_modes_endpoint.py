"""GET /api/chat/reasoning-modes tells the chat's picker which modes this browser's next request may use.

It must apply exactly the rule /api/chat applies (paid modes only with the V1-verified paid entitlement), so the
picker never shows a mode as selectable that the chat would refuse. The real application runs in a child
process; only the verified-account lookup is substituted.
"""
from __future__ import annotations

import json

from test_unsafe_request_guard import ORIGIN, _run_app

MODES = {
    "KALILLAC_REASONING_MODES_ENABLED": "true",
    "OPENAI_API_KEY": "sk-test-not-real",
    "KALILLAC_REASONING_MODE_QUICK_EFFORT": "none",
    "KALILLAC_REASONING_MODE_BALANCED_EFFORT": "low",
    "KALILLAC_REASONING_MODE_DEEP_EFFORT": "high",
    "KALILLAC_REASONING_MODE_QUICK_EXTRA_OUTPUT_TOKENS": "0",
    "KALILLAC_REASONING_MODE_BALANCED_EXTRA_OUTPUT_TOKENS": "0",
    "KALILLAC_REASONING_MODE_DEEP_EXTRA_OUTPUT_TOKENS": "8000",
    "KALILLAC_REASONING_MODE_DEFAULT": "balanced",
    "KALILLAC_REASONING_MODES_FREE": "quick,balanced",
    "KALILLAC_REASONING_MODES_PAID": "quick, balanced, deep",
}

SCRIPT = r'''
import asyncio, json, sys, uuid
sys.path.insert(0, "tests")
import httpx
import app_fastapi_candidate as a
import kalillac_accounts.paid_access as paid_access
from kalillac_accounts.entitlements import PAID
from kalillac_accounts.paid_access import VerifiedAccount
VERIFIED = VerifiedAccount(uuid.uuid4(), PAID)
if a.reasoning_modes.CONFIG is not None:
    a._v1_account_security = object()
async def verified_account(request, security, cookie_name):
    return VERIFIED if request.cookies.get("kalillac_session") == "verified" else None
paid_access.verified_account = verified_account
def headers(account=None):
    h = {"origin": "ORIGIN_VALUE"}
    if account:
        h["cookie"] = "kalillac_session=" + account
    return h
async def run():
    out = {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=a.api), base_url="https://testserver") as c:
        for who in (None, "free", "verified"):
            r = await c.get("/api/chat/reasoning-modes", headers=headers(who))
            out[str(who)] = [r.status_code, r.json() if r.content else None, r.headers.get("cache-control"),
                             r.headers.get("vary")]
        if a.reasoning_modes.CONFIG is not None:
            refused = await c.post("/api/chat", headers=headers("free"), json={"message": "Hi there", "reasoning_mode": "deep"})
            out["chat_free_deep"] = [refused.status_code, refused.json().get("error")]
    return out
print(json.dumps(asyncio.run(run())))
'''


def _result(env):
    proc = _run_app({"KALILLAC_SITE_ORIGIN": ORIGIN, **env}, SCRIPT.replace("ORIGIN_VALUE", ORIGIN))
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_modes_a_browser_may_use_follow_the_chats_own_rule():
    out = _result(MODES)
    free = ["quick", "balanced"]
    for who in ("None", "free"):
        status, body, cache, vary = out[who]
        assert status == 200 and body == {"modes": free, "default": "balanced", "premium": ["deep"]}, who
        assert cache == "no-store" and "Cookie" in vary
    status, body, _, _ = out["verified"]
    assert status == 200 and body == {"modes": ["quick", "balanced", "deep"], "default": "balanced", "premium": []}
    # The chat refuses what the answer withholds, before any work.
    assert out["chat_free_deep"] == [403, "reasoning_mode_not_allowed"]


def test_without_reasoning_modes_there_is_nothing_to_choose():
    out = _result({})
    assert all(out[who][:3] == [404, None, "no-store"] for who in ("None", "free", "verified"))
