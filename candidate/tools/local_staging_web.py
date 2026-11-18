"""Local staging only: the front server (what Nginx does on a real host) and the local panel.

Serves the built web root (the site at /, the chat at /app/, every /account path from account.html, as in
production), proxies /api/ to the application on loopback, and adds a clearly labelled panel at
/__local__/ that a real deployment does not have:

* the email outbox (registration, recovery and notice emails the application sent, with their links);
* when Stripe is simulated, the stand-in for Stripe's hosted pages: start a Checkout for the signed-in
  account through the application's own /api/account/billing/checkout, "pay" it, and the simulated
  portal's cancel, resume and end-now. Each sends Stripe's events, signed with the local test webhook
  secret, to the application's real webhook route.

Binds to loopback only; the panel's actions accept same-origin requests only. Run by tools/local_staging.py.
"""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_staging_sim as sim  # noqa: E402

HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
       "proxy-authenticate", "host", "content-length"}


def build(webroot: Path, upstream: str, origin: str, webhook_secret_file: str | None, simulated: list[str]):
    client = httpx.AsyncClient(base_url=upstream, timeout=httpx.Timeout(700.0), follow_redirects=False)
    webroot = webroot.resolve()

    async def proxy(request: Request):
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP]
        headers.append(("host", urlsplit(origin).netloc))
        upstream_request = client.build_request(request.method, request.url.path, params=request.url.query or None,
                                                headers=headers, content=await request.body())
        reply = await client.send(upstream_request)
        out = Response(content=reply.content, status_code=reply.status_code)
        for key, value in reply.headers.multi_items():
            if key.lower() not in HOP and key.lower() != "content-encoding":
                out.headers.append(key, value)
        return out

    def static(request: Request):
        path = request.url.path
        candidates = []
        if path == "/":
            candidates = [webroot / "index.html"]
        elif path == "/app" :
            return RedirectResponse("/app/", status_code=308)
        elif path.endswith("/"):
            candidates = [webroot / path.strip("/") / "index.html"]
        else:
            candidates = [webroot / path.lstrip("/"), webroot / (path.lstrip("/") + ".html")]
        if path == "/account" or path.startswith("/account/"):
            candidates.append(webroot / "account.html")
        for candidate in candidates:
            resolved = candidate.resolve()
            if (resolved == webroot or webroot in resolved.parents) and resolved.is_file():
                return FileResponse(resolved, headers={"Cache-Control": "no-store"})
        return HTMLResponse("<h1>Not found</h1>", status_code=404)

    async def account_call(request: Request, method: str, path: str):
        # Exactly what the account page sends: the session cookie, this origin, and no body.
        headers = {"cookie": request.headers.get("cookie", ""), "origin": origin, "accept": "application/json"}
        return await client.request(method, path, headers=headers)

    def same_origin(request: Request) -> bool:
        return request.headers.get("origin") == origin

    async def deliver(events):
        secret = sim.read_secret(webhook_secret_file)
        results = []
        for event in events:
            body, signature = sim.signed(event, secret)
            reply = await client.post("/api/billing/stripe/webhook", content=body,
                                      headers={"stripe-signature": signature, "content-type": "application/json"})
            results.append((event["type"], reply.status_code, reply.text[:200]))
        return results

    async def panel(request: Request):
        me = await account_call(request, "GET", "/api/account/me")
        user = me.json().get("user") if me.status_code == 200 else None
        billing = None
        if user:
            reply = await account_call(request, "GET", "/api/account/billing")
            billing = reply.json().get("billing") if reply.status_code == 200 else None
        mails = sim.outbox()
        rows = "".join(
            f"<li><div class='meta'>{html.escape(m.get('at', ''))} · to {html.escape(m.get('to', ''))}</div>"
            f"<div class='subject'>{html.escape(m.get('subject', ''))}</div>"
            f"<pre>{_linkify(m.get('text', ''))}</pre></li>" for m in mails) or "<li class='empty'>No emails yet.</li>"
        stripe = ""
        if "stripe" in simulated:
            if not user:
                stripe = "<p>Sign in on <a href='/account/sign-in'>the account page</a> first; this panel acts as that account.</p>"
            else:
                status = html.escape(json.dumps(billing)) if billing is not None else "unavailable"
                stripe = (f"<p>Signed in as <b>{html.escape(user.get('email', ''))}</b>. Billing as the application "
                          f"reports it: <code>{status}</code></p>"
                          "<form method='post' action='/__local__/stripe/checkout'><button>Start a simulated Checkout</button></form>"
                          "<form method='post' action='/__local__/stripe/portal/cancel_at_period_end'><button>Simulated portal: cancel at period end</button></form>"
                          "<form method='post' action='/__local__/stripe/portal/resume'><button>Simulated portal: resume</button></form>"
                          "<form method='post' action='/__local__/stripe/portal/end_now'><button>Simulated portal: end the subscription now</button></form>")
        else:
            stripe = "<p>Stripe is in real test mode: use the account page's Upgrade and Manage billing buttons.</p>"
        body = f"""
<h1>Kalillac local staging</h1>
<p class='banner'>Local evaluation environment. Simulated providers: <b>{html.escape(', '.join(simulated) or 'none')}</b>.
Simulated replies say so in their text. Commercial values (allowances, budgets, prices) are synthetic test
inputs, not owner decisions.</p>
<p><a href='/'>Kalillac</a> · <a href='/app/'>Chat</a> · <a href='/account'>Account</a></p>
<h2>Stripe{' (simulated)' if 'stripe' in simulated else ''}</h2>{stripe}
<h2>Email outbox{' (simulated)' if 'email' in simulated else ''}</h2><ul class='mail'>{rows}</ul>"""
        return _page("Kalillac local staging", body)

    async def checkout(request: Request):
        if not same_origin(request):
            return Response("cross-origin request refused", status_code=403)
        reply = await account_call(request, "POST", "/api/account/billing/checkout")
        data = reply.json() if reply.headers.get("content-type", "").startswith("application/json") else {}
        url = data.get("checkout_url", "")
        if reply.status_code != 200 or "/c/pay/" not in url:
            return _page("Checkout refused", f"<h1>The application refused Checkout</h1><p>HTTP {reply.status_code}: "
                                             f"<code>{html.escape(json.dumps(data))}</code></p><p><a href='/__local__/'>Back</a></p>")
        session = url.rsplit("/", 1)[-1]
        return RedirectResponse(f"/__local__/stripe/pay/{session}", status_code=303)

    async def pay_page(request: Request):
        session = request.path_params["session"]
        record = sim.stripe_record().load()["sessions"].get(session)
        if record is None:
            return HTMLResponse("unknown session", status_code=404)
        return _page("Simulated Stripe Checkout", f"""
<h1>Simulated Stripe Checkout</h1><p class='banner'>A local stand-in for Stripe's hosted page. No card is charged
and nothing reaches Stripe.</p><p>Kalillac Premium, Price <code>{html.escape(record['price'])}</code> (synthetic).
Status: <b>{html.escape(record['status'])}</b>.</p>
<form method='post' action='/__local__/stripe/pay/{html.escape(session)}'><button>Pay with test card 4242 4242 4242 4242</button></form>
<p><a href='{html.escape(record['cancel_url'])}'>Cancel and return to Kalillac</a></p>""")

    async def pay(request: Request):
        if not same_origin(request):
            return Response("cross-origin request refused", status_code=403)
        try:
            success, events = sim.complete_checkout(request.path_params["session"])
        except LookupError as missing:
            return _page("Not payable", f"<p>{html.escape(str(missing))}</p><p><a href='/__local__/'>Back</a></p>")
        results = await deliver(events)
        if any(status != 200 for _, status, _ in results):
            return _page("Webhook refused", "<h1>The application did not accept a simulated event</h1><pre>"
                         + html.escape(json.dumps(results, indent=1)) + "</pre>")
        return RedirectResponse(success, status_code=303)

    async def portal(request: Request):
        if not same_origin(request):
            return Response("cross-origin request refused", status_code=403)
        me = await account_call(request, "GET", "/api/account/me")
        if me.status_code != 200:
            return RedirectResponse("/account/sign-in", status_code=303)
        try:
            event = sim.change_subscription(me.json()["user"]["id"], request.path_params["action"])
        except (LookupError, ValueError) as refused:
            return _page("Nothing to change", f"<p>{html.escape(str(refused))}</p><p><a href='/__local__/'>Back</a></p>")
        results = await deliver([event])
        return _page("Simulated portal", "<h1>Simulated portal</h1><pre>" + html.escape(json.dumps(results, indent=1))
                     + "</pre><p><a href='/account'>Your account</a> · <a href='/__local__/'>Panel</a></p>")

    routes = [Route("/__local__/", panel), Route("/__local__", lambda r: RedirectResponse("/__local__/", 308)),
              Route("/__local__/stripe/checkout", checkout, methods=["POST"]),
              Route("/__local__/stripe/pay/{session}", pay_page, methods=["GET"]),
              Route("/__local__/stripe/pay/{session}", pay, methods=["POST"]),
              Route("/__local__/stripe/portal/{action}", portal, methods=["POST"]),
              Route("/api/{rest:path}", proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]),
              Route("/{rest:path}", static, methods=["GET", "HEAD"])]
    return Starlette(routes=routes, on_shutdown=[client.aclose])


def _linkify(text: str) -> str:
    import re
    escaped = html.escape(text)
    return re.sub(r"(https?://[^\s<]+)", r"<a href='\1'>\1</a>", escaped)


def _page(title, body):
    return HTMLResponse(f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><title>{html.escape(title)}</title>
<meta name='robots' content='noindex'><style>
:root {{ color-scheme: light dark; --ink:#0b1737; --muted:#5a6883; --line:#d9e2f1; --accent:#0069ff; --bg:#f6f9fe; --card:#fff; }}
@media (prefers-color-scheme: dark) {{ :root {{ --ink:#e8eefc; --muted:#a3b0c8; --line:#2a3550; --bg:#0d1424; --card:#141d33; }} }}
body {{ margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--ink); font:15px/1.55 system-ui, sans-serif; }}
main {{ max-width:820px; margin:0 auto; }}
h1 {{ font-size:24px; margin:0 0 8px; }} h2 {{ font-size:17px; margin:28px 0 8px; }}
.banner {{ padding:10px 14px; border-left:3px solid #c27c0e; background:rgba(194,124,14,.10); border-radius:0 8px 8px 0; }}
a {{ color:var(--accent); }} code {{ font-size:13px; overflow-wrap:anywhere; }}
button {{ margin:4px 0; min-height:44px; padding:8px 16px; border-radius:10px; border:1px solid var(--line);
          background:var(--card); color:var(--ink); font:600 14px system-ui, sans-serif; cursor:pointer; }}
ul.mail {{ list-style:none; padding:0; }} ul.mail li {{ background:var(--card); border:1px solid var(--line);
          border-radius:10px; padding:12px 14px; margin:0 0 10px; }}
.meta {{ color:var(--muted); font-size:13px; }} .subject {{ font-weight:700; margin:2px 0 6px; }}
pre {{ white-space:pre-wrap; overflow-wrap:anywhere; margin:0; font:13px/1.5 ui-monospace, monospace; }}
</style></head><body><main>{body}</main></body></html>""")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="local_staging_web.py")
    parser.add_argument("--webroot", required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    sim.require_local()
    origin = os.environ["KALILLAC_SITE_ORIGIN"]
    app = build(Path(args.webroot), args.upstream, origin, os.environ.get("KALILLAC_LOCAL_WEBHOOK_SECRET_FILE"),
                sim.simulated_providers())
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
