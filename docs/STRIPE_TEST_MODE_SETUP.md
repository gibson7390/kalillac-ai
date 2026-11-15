# Stripe test-mode setup (staging only)

For the owner and an operator preparing the isolated staging environment. It sets up Stripe **test
mode** so the subscription, cancellation, refund and dispute flows can be exercised end to end with
Stripe's test cards. Nothing here uses live keys, creates a real charge or touches production.
Kalillac refuses to start billing if the key's mode and `KALILLAC_STRIPE_MODE` disagree, so a live key
cannot be used by mistake in a test configuration.

Decisions this needs from the owner first: the subscription price and interval (no price has been
assumed), and the product name customers will see. Test mode accepts any value; use the intended one
so the Checkout page looks as customers will see it.

## 1. In the Stripe Dashboard, test mode

1. Switch the Dashboard to **Test mode**.
2. **Product catalog > Add product**: the Premium product, one recurring Price (monthly, the owner's
   amount). Copy the Price id (`price_...`). Add an annual Price later the same way if wanted.
3. **Settings > Billing > Customer portal**: allow cancelling subscriptions and updating payment
   methods; set the return link to the staging site.
4. **Developers > API keys**: copy the test secret key (`sk_test_...`). Never copy a `sk_live_` key for
   staging.
5. **Developers > Webhooks > Add endpoint**: `https://<staging host>/api/billing/stripe/webhook`, with
   exactly these events:
   `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`, `charge.refunded`, `charge.dispute.created`,
   `charge.dispute.updated`, `charge.dispute.closed`, `charge.dispute.funds_withdrawn`,
   `charge.dispute.funds_reinstated`. Copy its signing secret (`whsec_...`).

## 2. On the staging server

Secrets go in systemd credentials, never in an environment file, a commit or a log:

```
/etc/credstore/kalillac-staging/stripe_secret_key       (the sk_test_ key, mode 0400, owner root)
/etc/credstore/kalillac-staging/stripe_webhook_secret   (the whsec_ secret, mode 0400, owner root)
```

loaded by the staging unit with `LoadCredential=` so the application reads them from
`CREDENTIALS_DIRECTORY`. Ordinary settings in the staging environment:

```
KALILLAC_BILLING_ENABLED=true
KALILLAC_STRIPE_MODE=test
KALILLAC_STRIPE_PRICE_ID=price_...                 (the Checkout Price)
KALILLAC_STRIPE_ALLOWED_PRICE_IDS=price_...        (every Price that grants Premium, comma-separated)
KALILLAC_BILLING_SUCCESS_URL=https://<staging host>/account/billing?checkout=success
KALILLAC_BILLING_CANCEL_URL=https://<staging host>/account/billing?checkout=cancelled
KALILLAC_BILLING_PORTAL_RETURN_URL=https://<staging host>/account/billing
```

Billing also requires accounts and the database (`KALILLAC_ACCOUNTS_ENABLED`, `KALILLAC_DB_ENABLED`)
and staging's own database with migrations through `0017_billing_cases`. Start-up names any missing or
invalid setting and refuses.

## 3. Exercising the flows (test cards only)

| Flow | How |
|---|---|
| Subscribe | Sign in on staging, start Checkout, pay with `4242 4242 4242 4242`, any future date, any CVC. Premium appears after the `checkout.session.completed` and subscription webhooks. |
| Payment needing authentication | Card `4000 0025 0000 3155`. |
| Failed renewal | Card `4000 0000 0000 0341` (attaches, then fails), or advance a test clock in the Dashboard. The account stays Premium while Stripe reports `past_due`, then follows Stripe's final status. |
| Cancel | Customer portal, "Cancel plan". Access lasts to the period end plus the 7-day grace, then returns to Free. |
| Refund | Dashboard > the payment > Refund. A billing case appears (`python tools/billing_cases.py list`). Access is unchanged; the operator decides. |
| Dispute | Pay with `4000 0000 0000 0259`; Stripe opens a dispute. A billing case appears and follows the dispute to its outcome. |
| Missed webhook | Disable the endpoint, change a subscription, re-enable, then `python tools/reconcile_billing_account.py <account-uuid> --expect-stripe-mode test --apply`. |

The Stripe CLI (`stripe listen --forward-to https://<staging host>/api/billing/stripe/webhook` and
`stripe trigger <event>`) can drive the same events without the Dashboard.

## 4. What this does not do

No live keys, no real customers, no real money. Moving to live mode is a separate, explicit owner
decision with its own checklist (live Price, live webhook endpoint and secret, refund and dispute
policy, legal review), and is not part of staging.
