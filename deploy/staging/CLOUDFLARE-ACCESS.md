# Staging edge: Cloudflare Access, the Stripe webhook exception, and the origin

For the operator configuring `staging.kalillac.ai` (the proposed hostname; it has no DNS record as of
2026-11-18). Nothing here is done by a development session. Goal: only the owner reaches staging, Stripe
reaches exactly one endpoint, and nobody reaches the origin around Cloudflare.

## What the application already enforces on the webhook

`POST /api/billing/stripe/webhook` (`candidate/kalillac_billing/router.py`): POST only (any other method
is 405); a `Stripe-Signature` header is required (400 otherwise); the body is at most 512 KiB (413);
the signature is verified with the endpoint's `whsec_` secret and the Stripe library's 5-minute timestamp
tolerance (400 `invalid_signature`); an event whose `livemode` differs from `KALILLAC_STRIPE_MODE=test` is
refused and never recorded (400 `livemode_mismatch`); each event id is processed once (a replay is a
duplicate); access is always re-read from Stripe's current subscription, never taken from the event body.
It is the only unsafe route exempt from the origin check (`request_guard.EXEMPT`). The edge rules below add
to this; they do not replace it.

## 1. Two Access applications, most specific first

| Application | Domain and path | Policy | Include |
|---|---|---|---|
| Staging | `staging.kalillac.ai`, all paths | **Allow** | the owner's email only (one-time PIN or the owner's identity provider); session 24 hours |
| Stripe webhook | `staging.kalillac.ai/api/billing/stripe/webhook` | **Bypass** | **IP ranges: Stripe's published webhook addresses only** (`https://stripe.com/files/ips/ips_webhooks.json`) |

Never a Bypass for "Everyone", never a bypass on a shorter path (`/api`, `/api/billing`). Access applies
an application to its path and the paths beneath it; the application has no route beneath the webhook,
and Nginx (section 2) refuses any, so the bypass reaches nothing else. When Stripe changes its list (it
announces changes), update the IP ranges; until then a delivery from a new address is refused at the edge
and Stripe retries it, so nothing is lost or wrongly accepted. Cloudflare Zero Trust's free plan covers
this (up to 50 users); enabling it is the owner's account decision.

## 2. Nginx on the staging origin

Start from the reviewed site file (runbook section 9), then for this hostname:

```
location = /api/billing/stripe/webhook {
    limit_except POST { deny all; }
    client_max_body_size 512k;
    proxy_pass http://127.0.0.1:PORT;
}
location ^~ /api/billing/stripe/webhook/ { return 404; }
add_header X-Robots-Tag "noindex, nofollow" always;
```

## 3. The origin accepts only this zone's Cloudflare traffic

* Firewall: 443 open only to Cloudflare's published ranges (production's origin already drops direct
  traffic: measured 2026-11-18, `docs/STAGING-HOST-ASSESSMENT.md`); 80 closed; SSH only from the
  operator's address.
* **Authenticated Origin Pulls with a certificate for this zone or hostname** (not Cloudflare's shared
  certificate), `ssl_verify_client on` in the staging server block. Without it, a different Cloudflare
  account could proxy its own hostname to this origin and skip this zone's Access policies.

## 4. Proof after configuration (from outside; read-only)

```
H=https://staging.kalillac.ai
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' $H/                 # 302 to the Access login, never 200
curl -s -o /dev/null -w '%{http_code}\n' $H/api/health                       # 302 or 403, never 200
curl -s -o /dev/null -w '%{http_code}\n' $H/api/billing/stripe/webhook       # 403 (Access: not a Stripe address)
curl -s -o /dev/null -w '%{http_code}\n' -X POST $H/api/billing/stripe/webhook   # 403 (same)
curl -s -o /dev/null -w '%{http_code}\n' $H/api/billing/stripe/webhook/x     # 403 at the edge, 404 at the origin
curl -sk --max-time 8 --resolve staging.kalillac.ai:443:<ORIGIN IP> $H/ ; echo "exit $?"   # no connection (28 or 35)
```

Then, in the browser as the owner: the Access login, then the site. In the Stripe Dashboard (test mode):
**Send test webhook** to the endpoint returns 200 `{"received": true, ...}`; the application's journal
shows no `STRIPE_WEBHOOK_MODE_MISMATCH`. A real test-mode Checkout (card 4242 4242 4242 4242) then makes the
account Premium (runbook section 10).
