"""Local staging only: provider simulation for the real application (tools/local_staging.py).

Replaces exactly the outermost network exchange of each provider the owner has not given real test
credentials for, and nothing else, so every product control still runs for real: reasoning modes, the
Financial Safeguards reservation and settlement, provider ceilings, request budgets, the daily allowance,
V1 authorization, the file worker, Stripe's webhook signature verification and event parsing, the
reconciliation of subscriptions and entitlements.

    OpenAI   the bounded transport's single POST (post_json_within_budget, for api.openai.com) and the
             unbudgeted _post_openai_responses: a scripted GPT-6 Luna stand-in that says it is simulated,
             reports token usage within the reservation's own bounds, and drives the file tools
             (create_file, read_file, edit_file) so generation and editing run in the real worker.
    Brave    the same POST for api.search.brave.com, and the unbudgeted search POST: fixed, labelled results.
    Tavily   the same for api.tavily.com.
    Stripe   a StripeGateway whose five network calls (Checkout create and retrieve, portal, subscription,
             charge and dispute retrieval) are answered from a local record; parse_webhook is Stripe's own
             code, so simulated events are signed with the local test webhook secret and verified for real.
    Email    Resend's single delivery call: the rendered message goes to a local outbox instead.

It refuses to install unless the deployment is a local one (KALILLAC_DEPLOYMENT_ID starting with
"kalillac-local-", KALILLAC_SITE_ORIGIN on localhost or 127.0.0.1, KALILLAC_STRIPE_MODE=test), and it
blocks name resolution for every simulated provider's host, so a simulated provider can never be reached
on the network by any path. Nothing here is imported by the application.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

PROVIDERS = ("openai", "brave", "tavily", "stripe", "email")
HOSTS = {"openai": "api.openai.com", "brave": "api.search.brave.com", "tavily": "api.tavily.com",
         "stripe": "api.stripe.com", "email": "api.resend.com"}
LABEL = "Simulated GPT-6 Luna"
SIM_DAYS = 30


class LocalOnly(RuntimeError):
    pass


def require_local(environ=os.environ):
    deployment = environ.get("KALILLAC_DEPLOYMENT_ID", "")
    origin = urlsplit(environ.get("KALILLAC_SITE_ORIGIN", ""))
    if (not deployment.startswith("kalillac-local-") or origin.hostname not in ("localhost", "127.0.0.1")
            or environ.get("KALILLAC_STRIPE_MODE") != "test"):
        raise LocalOnly("provider simulation is for a local deployment only")


def state_dir(environ=os.environ) -> Path:
    path = Path(environ["KALILLAC_LOCAL_STATE"])
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- the network guard --------------------------------------------------------------------------------

def guard_network(simulated):
    blocked = {HOSTS[name] for name in simulated}
    if "stripe" in simulated:
        blocked |= {"checkout.stripe.com", "billing.stripe.com", "files.stripe.com"}
    original = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else str(host or "")
        if name.lower().rstrip(".") in blocked:
            raise socket.gaierror(socket.EAI_NONAME, f"{name} is simulated in local staging")
        return original(host, *args, **kwargs)
    socket.getaddrinfo = getaddrinfo


# --- OpenAI: a scripted stand-in ---------------------------------------------------------------------

def _approx_tokens(payload):
    """Input tokens as the Responses API would roughly count them, never above the reservation's bound:
    text at ~4 characters per token, each image at a small fixed count (images are reserved at the
    configured per-image bound, not their bytes)."""
    images = 0

    def strip(value):
        nonlocal images
        if isinstance(value, dict):
            if value.get("type") == "input_image":
                images += 1
                return {}
            return {k: strip(v) for k, v in value.items()}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    text = json.dumps(strip(payload), ensure_ascii=False)
    return max(1, len(text) // 6) + images * 500


def _usage(payload, text_out):
    out = max(1, len(text_out) // 4)
    bound = payload.get("max_output_tokens")
    if isinstance(bound, int):
        out = min(out, bound)
    return {"input_tokens": _approx_tokens(payload), "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": out, "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": _approx_tokens(payload) + out}


def _message(payload, text):
    return {"id": "resp_sim_" + secrets.token_hex(8), "object": "response", "status": "completed",
            "model": payload.get("model", "gpt-6-luna"),
            "output": [{"type": "message", "role": "assistant", "id": "msg_sim_" + secrets.token_hex(6),
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}],
            "usage": _usage(payload, text)}


def _call(payload, name, arguments):
    text = json.dumps(arguments)
    return {"id": "resp_sim_" + secrets.token_hex(8), "object": "response", "status": "completed",
            "model": payload.get("model", "gpt-6-luna"),
            "output": [{"type": "function_call", "id": "fc_sim_" + secrets.token_hex(6),
                        "call_id": "call_sim_" + secrets.token_hex(6), "name": name, "arguments": text}],
            "usage": _usage(payload, text)}


def _user_text(payload):
    for item in reversed(payload.get("input") or []):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _images(payload):
    count = 0
    for item in payload.get("input") or []:
        if isinstance(item, dict) and isinstance(item.get("content"), list):
            count += sum(1 for part in item["content"] if isinstance(part, dict) and part.get("type") == "input_image")
    return count


def _tool_outputs(payload):
    return [item for item in payload.get("input") or [] if isinstance(item, dict)
            and item.get("type") == "function_call_output"]


def _calls(payload):
    return [item for item in payload.get("input") or [] if isinstance(item, dict) and item.get("type") == "function_call"]


def _effort(payload):
    reasoning = payload.get("reasoning")
    return reasoning.get("effort", "default") if isinstance(reasoning, dict) else "default"


def _attached(text):
    return re.findall(r"- id ([A-Za-z0-9_-]{8,}): ([^\n(]+) \(([^,]+),", text)


FORMAT_WORDS = (("pptx", ("powerpoint", "pptx", "deck", "slides", "presentation")),
                ("xlsx", ("excel", "xlsx", "spreadsheet", "workbook")),
                ("csv", ("csv",)), ("docx", ("word", "docx")), ("md", ("markdown",)), ("pdf", ("pdf",)))


def _format(text):
    lowered = text.lower()
    for fmt, words in FORMAT_WORDS:
        if any(re.search(r"\b" + word + r"\b", lowered) for word in words):
            return fmt
    return "pdf"


def _sample_spec(fmt, request):
    """A complete specification in the product's grammar: the repository's quality benchmark content,
    retitled from the request. Synthetic sample content, labelled as such."""
    tests = Path(__file__).resolve().parents[1] / "tests"
    if str(tests) not in sys.path:
        sys.path.insert(0, str(tests))
    import copy
    import quality_benchmark as qb
    title = " ".join(request.split()[:10])[:80] or "Local staging sample"
    if fmt in ("xlsx", "csv"):
        spec = copy.deepcopy(qb.FORECAST_WORKBOOK)
    elif fmt == "pptx":
        spec = copy.deepcopy(qb.EXECUTIVE_DECK)
    else:
        spec = copy.deepcopy(qb.RESEARCH_DOCUMENT)
    if isinstance(spec.get("title"), str):
        spec["title"] = f"{title} (simulated sample)"
    return spec


def _replacement(text):
    """("old", "new") from the customer's own words ("replace A with B", "change A to B"), never from the
    attachment note the product appends to the message."""
    message = text.split("\n\nFiles the customer attached", 1)[0]
    match = re.search(r"(?:replace|change)\s+[\"“']?([^\n]+?)[\"”']?\s+(?:with|to)\s+[\"“']?([^\n]+?)[\"”']?"
                      r"\s*[.!?]?\s*(?:\n|$)", message, re.I)
    return (match.group(1).strip(), match.group(2).strip()) if match else None


CONFIRM_TEXT = "Apply that edit exactly as I asked."


def _earlier_change(payload):
    """The most recent edit the customer described earlier in the conversation (the history turns)."""
    turns = [item for item in payload.get("input") or [] if isinstance(item, dict) and item.get("role") == "user"]
    for item in reversed(turns[:-1]):
        content = item.get("content")
        found = _replacement(content) if isinstance(content, str) else None
        if found:
            return found
    return None


def _tool_summary(output):
    try:
        result = json.loads(output) if isinstance(output, str) else dict(output)
    except (TypeError, ValueError):
        result = {}
    status, name = result.get("status"), result.get("filename")
    if status in ("created", "edited") and name:
        size = result.get("size")
        shown = f", {size / 1024:.0f} KB" if isinstance(size, int) else ""
        what = "created" if status == "created" else "edited a copy of your file as"
        return (f"**{LABEL}.** No model was called, but the real file tools and the isolated worker ran: they "
                f"{what} **{name}** ({str(result.get('format', '')).upper()}{shown}). "
                + ("Its content is the repository's sample benchmark document, retitled from your request; with "
                   "a real OpenAI key, GPT-6 Luna writes the content from what you ask."
                   if status == "created" else "The edit you described was applied by the real editor."))
    code = result.get("error") or result.get("code") or result.get("status") or "no result"
    return (f"**{LABEL}.** No model was called. The real file tools refused or could not complete the "
            f"simulated request (`{code}`): this is the product's own answer to that case.")


def simulated_openai(payload):
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    tools = {tool.get("name") for tool in payload.get("tools") or [] if isinstance(tool, dict)}
    text = _user_text(payload)
    effort = _effort(payload)
    outputs, calls = _tool_outputs(payload), _calls(payload)
    if "create_file" in tools:
        # The chat's confirmation of a held-back edit is one fixed message (frontend CONFIRM_TEXT): the
        # model is expected to repeat the customer's earlier edit, now with exact=true.
        confirming = text.split("\n\nFiles the customer attached", 1)[0].strip() == CONFIRM_TEXT
        change = _replacement(text) or (_earlier_change(payload) if confirming else None)
        if outputs:
            last = outputs[-1].get("output", "")
            last_call = calls[-1].get("name") if calls else ""
            if last_call == "read_file":
                attached = _attached(text)
                if attached and change:
                    arguments = {"file_id": attached[0][0], "operations": [
                        {"op": "replace_text", "find": change[0], "replace": change[1]}]}
                    if confirming:
                        arguments["exact"] = True
                    return _call(payload, "edit_file", arguments)
            return _message(payload, _tool_summary(last))
        images = _images(payload)
        if images:
            return _message(payload, f"**{LABEL}.** No model was called, so nothing was looked at. Your "
                                     f"{images} image(s) were checked, re-encoded and stored by the real "
                                     "worker, reserved under Financial Safeguards at the per-image bound, and "
                                     "sent in the request this stand-in received.")
        attached = _attached(text)
        office = [a for a in attached if not a[2].startswith("image/")]
        if office and change:
            return _call(payload, "read_file", {"file_id": office[0][0]})
        fmt = _format(text)
        return _call(payload, "create_file", {"format": fmt, "spec": _sample_spec(fmt, text)})
    # The chat sends its own guidance as input items too, so the customer's words are not quoted back:
    # a stand-in that misquotes is worse than one that does not quote.
    reply = (f"**{LABEL}.** No model was called: this is local staging with simulated providers. "
             f"Your message went through the real chat pipeline (allowance, request budget, Financial "
             f"Safeguards reservation and settlement, reasoning effort `{effort}`).\n\n"
             "With a real OpenAI key in local staging's provider file, GPT-6 Luna answers here instead.")
    return _message(payload, reply)


# --- Brave and Tavily -------------------------------------------------------------------------------

def simulated_brave(payload):
    query = str((payload or {}).get("q", ""))[:200]
    return {"grounding": {"generic": [
        {"url": "https://example.com/local-staging-simulated-result", "title": "Simulated search result",
         "snippets": [f"Simulated Brave result for: {query}. No search was made in local staging."]}]},
        "sources": {}}


def simulated_tavily(payload):
    query = str((payload or {}).get("query", ""))[:200]
    return {"query": query, "results": [
        {"url": "https://example.org/local-staging-simulated-result", "title": "Simulated search result",
         "content": f"Simulated Tavily result for: {query}. No search was made in local staging.", "score": 0.5}]}


def _dispatch(url, payload):
    host = urlsplit(url).hostname or ""
    if host == HOSTS["openai"]:
        return simulated_openai(payload)
    if host == HOSTS["brave"]:
        return simulated_brave(payload)
    if host == HOSTS["tavily"]:
        return simulated_tavily(payload)
    raise RuntimeError("no simulation for this host")


# --- Stripe -------------------------------------------------------------------------------------------

class StripeRecord:
    """Simulated Stripe objects, kept in local staging's state directory (survives restarts)."""

    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()

    def load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"sessions": {}, "subscriptions": {}, "customers": {}}

    def save(self, data):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def stripe_record(environ=os.environ):
    return StripeRecord(state_dir(environ) / "stripe-simulation.json")


def sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Stripe's webhook signature header (t=...,v1=HMAC-SHA256 of "t.payload")."""
    t = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def subscription_object(sub):
    return {"id": sub["id"], "object": "subscription", "customer": sub["customer"], "status": sub["status"],
            "cancel_at_period_end": sub["cancel_at_period_end"], "cancel_at": None,
            "metadata": {"kalillac_user_id": sub["user"]},
            "items": {"object": "list", "data": [{"id": "si_" + sub["id"][4:], "price": {"id": sub["price"]},
                                                  "current_period_end": sub["period_end"]}]}}


def make_gateway_class():
    from kalillac_billing.gateway import BillingProviderError, CheckoutSessionState
    from kalillac_billing.stripe_gateway import StripeGateway, subscription_state_from_stripe

    def _ts(value):
        return datetime.fromtimestamp(int(value), tz=timezone.utc) if value else None

    class SimulatedStripeGateway(StripeGateway):
        """Stripe's network calls answered locally; parse_webhook is Stripe's own."""

        def __init__(self, config):
            super().__init__(config)
            self.record = stripe_record()

        def _session_state(self, session):
            return CheckoutSessionState(session_id=session["id"], status=session["status"], url=session["url"],
                                        expires_at=_ts(session["expires_at"]), subscription_id=session.get("subscription"))

        def create_checkout_session(self, *, price_id, customer_id, kalillac_user_id, success_url, cancel_url,
                                    idempotency_key):
            with self.record.lock:
                data = self.record.load()
                for session in data["sessions"].values():
                    if session["idempotency_key"] == idempotency_key:
                        return self._session_state(session)
                session_id = "cs_test_sim_" + secrets.token_hex(12)
                session = {"id": session_id, "status": "open", "idempotency_key": idempotency_key,
                           "url": "https://checkout.stripe.com/c/pay/" + session_id, "price": price_id,
                           "customer": customer_id, "user": kalillac_user_id, "success_url": success_url,
                           "cancel_url": cancel_url, "expires_at": int(time.time()) + 23 * 3600, "subscription": None}
                data["sessions"][session_id] = session
                self.record.save(data)
                return self._session_state(session)

        def retrieve_checkout_session(self, session_id):
            session = self.record.load()["sessions"].get(session_id)
            if session is None:
                raise BillingProviderError()
            return self._session_state(session)

        def create_portal_session(self, *, customer_id, return_url):
            return "https://billing.stripe.com/p/session/test_sim_" + secrets.token_hex(8)

        def retrieve_subscription(self, subscription_id):
            sub = self.record.load()["subscriptions"].get(subscription_id)
            if sub is None:
                raise BillingProviderError()
            return subscription_state_from_stripe(subscription_object(sub))

        def retrieve_charge(self, charge_id):
            raise BillingProviderError()

        def retrieve_dispute(self, dispute_id):
            raise BillingProviderError()

    return SimulatedStripeGateway


# --- Email --------------------------------------------------------------------------------------------

def outbox_dir(environ=os.environ) -> Path:
    path = state_dir(environ) / "outbox"
    path.mkdir(exist_ok=True)
    return path


def install_email():
    from kalillac_accounts import resend_gateway

    def deliver(self, to, message, idempotency_key):
        if not isinstance(to, str) or not resend_gateway.ADDRESS.fullmatch(to):
            raise resend_gateway.EmailDeliveryError()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        record = {"to": to, "at": stamp, "simulated": True,
                  "subject": str(getattr(message, "subject", "")), "text": str(getattr(message, "text", message))}
        (outbox_dir() / f"{stamp}-{secrets.token_hex(4)}.json").write_text(json.dumps(record, indent=1),
                                                                           encoding="utf-8")
    resend_gateway.ResendGateway._deliver = deliver


# --- installation -------------------------------------------------------------------------------------

def simulated_providers(environ=os.environ):
    raw = environ.get("KALILLAC_LOCAL_SIMULATE", ",".join(PROVIDERS))
    chosen = [name.strip() for name in raw.split(",") if name.strip()]
    unknown = [name for name in chosen if name not in PROVIDERS]
    if unknown:
        raise LocalOnly("unknown simulated provider: " + ", ".join(unknown))
    return chosen


def install(environ=os.environ):
    require_local(environ)
    simulated = simulated_providers(environ)
    guard_network(simulated)
    exchange = {name for name in simulated if name in ("openai", "brave", "tavily")}
    if exchange:
        from kalillac_routing import provider_transport, search_providers
        real_budgeted, real_unbudgeted = provider_transport.post_json_within_budget, search_providers.post_json_unbudgeted

        def budgeted(holder, settings, url, payload, **kwargs):
            if _host_simulated(url, exchange):
                return _dispatch(url, payload)
            return real_budgeted(holder, settings, url, payload, **kwargs)

        def unbudgeted(url, payload, headers, timeout, *args, **kwargs):
            if _host_simulated(url, exchange):
                return _dispatch(url, payload)
            return real_unbudgeted(url, payload, headers, timeout, *args, **kwargs)
        provider_transport.post_json_within_budget = budgeted
        search_providers.post_json_unbudgeted = unbudgeted
        if "openai" in exchange:
            _after_import("app_fastapi_candidate", lambda module: setattr(
                module, "_post_openai_responses", lambda payload, timeout=90: simulated_openai(payload)))
    if "stripe" in simulated:
        from kalillac_billing import stripe_gateway
        stripe_gateway.StripeGateway = make_gateway_class()
    if "email" in simulated:
        install_email()
    real = [name for name in PROVIDERS if name not in simulated]
    print("LOCAL_STAGING_PROVIDERS simulated=" + (",".join(simulated) or "none") +
          " real=" + (",".join(real) or "none"), flush=True)
    return simulated


def _host_simulated(url, names):
    host = urlsplit(url).hostname or ""
    return any(HOSTS[name] == host for name in names)


def _after_import(name, action):
    """Run action(module) right after the module executes, without importing it early (the launcher
    imports the application only after taking its singleton lock)."""
    import importlib.abc
    import importlib.util

    class Hook(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname != name:
                return None
            sys.meta_path.remove(self)
            spec = importlib.util.find_spec(fullname)
            if spec is None or spec.loader is None:
                return spec
            loader = spec.loader
            exec_module = loader.exec_module

            def patched(module):
                exec_module(module)
                action(module)
            loader.exec_module = patched
            return spec
    sys.meta_path.insert(0, Hook())


# --- simulated Stripe events (the local panel) --------------------------------------------------------

def _event(event_type, obj):
    return {"id": "evt_sim_" + secrets.token_hex(12), "object": "event", "type": event_type, "livemode": False,
            "api_version": "2025-01-27", "created": int(time.time()), "data": {"object": obj}}


def complete_checkout(session_id, environ=os.environ):
    """Pay a simulated Checkout Session: the subscription becomes active for SIM_DAYS, and the two events
    Stripe would send are returned for delivery (checkout.session.completed, customer.subscription.created)."""
    record = stripe_record(environ)
    with record.lock:
        data = record.load()
        session = data["sessions"].get(session_id)
        if session is None or session["status"] != "open":
            raise LookupError("no open simulated Checkout Session")
        customer = session["customer"] or data["customers"].get(session["user"]) or "cus_sim_" + secrets.token_hex(8)
        data["customers"][session["user"]] = customer
        sub_id = "sub_sim_" + secrets.token_hex(10)
        period_end = int((datetime.now(timezone.utc) + timedelta(days=SIM_DAYS)).timestamp())
        sub = {"id": sub_id, "customer": customer, "status": "active", "cancel_at_period_end": False,
               "price": session["price"], "user": session["user"], "period_end": period_end}
        data["subscriptions"][sub_id] = sub
        session.update(status="complete", subscription=sub_id, customer=customer)
        record.save(data)
    completed = {"id": session_id, "object": "checkout.session", "mode": "subscription", "status": "complete",
                 "customer": customer, "subscription": sub_id, "client_reference_id": session["user"],
                 "metadata": {"kalillac_user_id": session["user"]}}
    return session["success_url"], [_event("checkout.session.completed", completed),
                                     _event("customer.subscription.created", subscription_object(sub))]


def change_subscription(user_id, action, environ=os.environ):
    """The simulated portal: cancel at period end, resume, or end now. Returns the event to deliver."""
    record = stripe_record(environ)
    with record.lock:
        data = record.load()
        subs = [s for s in data["subscriptions"].values() if s["user"] == user_id and s["status"] != "canceled"]
        if not subs:
            raise LookupError("no live simulated subscription for this account")
        sub = subs[-1]
        if action == "cancel_at_period_end":
            sub["cancel_at_period_end"] = True
            kind = "customer.subscription.updated"
        elif action == "resume":
            sub["cancel_at_period_end"] = False
            kind = "customer.subscription.updated"
        elif action == "end_now":
            sub["status"] = "canceled"
            kind = "customer.subscription.deleted"
        else:
            raise ValueError("unknown action")
        record.save(data)
    return _event(kind, subscription_object(sub))


def signed(event, secret):
    body = json.dumps(event).encode()
    return body, sign(body, secret)


def read_secret(path):
    return Path(path).read_text(encoding="utf-8").strip()


def outbox(environ=os.environ, limit=30):
    items = []
    for path in sorted(outbox_dir(environ).glob("*.json"), reverse=True)[:limit]:
        try:
            items.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return items


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
