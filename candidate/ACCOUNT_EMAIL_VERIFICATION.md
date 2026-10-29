# Account email-verification foundation

Backend-only and default-off. There is no verification page, real email adapter,
environment-selected provider, production activation, automatic sign-in, email change,
account deletion, MFA, shared throttling or retention/pruning job. Registration, login,
logout, password recovery, paid access, webhooks, reconciliation and the Customer Portal
are unchanged.

## Configuration and activation

KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED defaults off. While off, no verification route
is mounted, no gateway or URL is needed, /api/account/me is unchanged and Checkout behaves
exactly as before.

When on, startup requires KALILLAC_ACCOUNTS_ENABLED, KALILLAC_DB_ENABLED,
KALILLAC_ACCOUNT_EMAIL_VERIFICATION_URL (absolute HTTPS; HTTP only for loopback or
localhost; no credentials, query or fragment), a valid KALILLAC_EMAIL_FROM, and a
code-supplied EmailVerificationGateway with send_email_verification(to, link). The gateway
is a separate protocol from the recovery EmailGateway, whose contract is unchanged. The
main application supplies no gateway, so setting the flag alone fails at startup.

## Account state

Migration 0009 adds nullable users.email_verified_at (no default; every existing account
stays unverified and no row is rewritten) and account_email_verification_tokens: UUID id,
user_id (cascading foreign key, indexed), email_hash and unique token_hash (both lowercase
64-hex SHA-256, enforced by check constraints), created_at, expires_at (after created_at)
and nullable consumed_at. The runtime role gets SELECT, INSERT and UPDATE only on the new
table. Downgrade drops the index, the table and the column, nothing else.

While enabled, /api/account/me adds only "email_verified": true/false. The timestamp is
never exposed. Registration and login stay open to unverified accounts.

## HTTP contracts

Both routes: application/json only, streamed bodies of at most 4096 bytes, exact fields,
Cache-Control: no-store on every handled response; 415 json_required, 400 invalid_json,
413 body_too_large, 400 invalid_body.

POST /api/account/email-verification/request takes exactly {} and a signed-in session.
Order: body, client throttle (429 too_many_attempts with Retry-After), authentication
(401 not_authenticated, the existing contract; inactive accounts never authenticate),
account throttle, then 202 {"ok":true}. Eligible, already-verified, account-throttled and
accounts that become inactive before issuance all get the same 202. An authentication
lookup failure returns 503 verification_unavailable.

Issuance runs after the response: fixed transaction limits, lock the users row, reread
active/email/verified state, stop if inactive or verified, invalidate unused tokens, store
the new token's hashes, deliver through the gateway inside the transaction, commit. A
delivery failure rolls the whole issuance back; a commit failure after delivery leaves the
delivered credential unusable. Because the account row stays locked until delivery and
commit finish, a future real gateway must bound its delivery time.

POST /api/account/email-verification/confirm takes exactly {"token":"..."}. The canonical
32-byte URL-safe encoding is checked before database access. Malformed, unknown, expired,
consumed, replayed, email-mismatched and inactive-account tokens all return
400 invalid_or_expired_token; operational failures, including lock or statement timeouts,
return 503 verification_unavailable; success returns 200 {"ok":true} and sets no cookie.
Order: fixed limits, find the token's account, lock the users row, read the clock, consume
one unused unexpired token whose email_hash matches the current normalized email, set
email_verified_at for that user only, invalidate sibling tokens, commit. It never signs in,
changes a password, revokes sessions or touches billing, entitlement, usage, chat or
recovery state. Same-token and sibling-token races have exactly one winner.

## Tokens

32 cryptographically random bytes, canonical unpadded URL-safe encoding, 24-hour lifetime.
Only SHA-256 digests are stored. Links are the configured URL plus #token=<token>; the
credential never appears in a query string, path, log or API response.

## Transactions

Issuance and confirmation run SET LOCAL lock_timeout = '5s' and SET LOCAL
statement_timeout = '30s' before any account read or lock. SQLite is the explicit
test-only exception; unknown dialects fail closed. Every error rolls back the whole
mutation. Recovery, webhook, Checkout, reconciliation and ordinary account limits are
unchanged.

## Checkout

While enabled, POST /api/account/billing/checkout returns 403 email_verification_required
for a signed-in, unverified account. The check uses the same read-only signed-in lookup and
happens before start_checkout, so before any Stripe call or billing write. The portal,
billing status, webhooks, reconciliation and existing paid access are not restricted.

## Throttling

Independent sliding windows with their own store and an injectable monotonic clock,
keeping only SHA-256 digests:

- request, per client: 5 per 15 minutes, then 429 with Retry-After;
- request, per account and email: 3 per hour, excess is a silent 202 with no work;
- confirmation, per client: 10 invalid attempts per 15 minutes, then 429. In-flight
  attempts reserve capacity; valid and server-failed attempts release it.

The limiter is single-process and loses state on restart. A shared atomic store is
required before multi-worker production scaling.

## Events

Fixed codes only: ACCOUNT_EMAIL_VERIFICATION_REQUEST_FAILED, _DELIVERY_FAILED,
_CONFIRM_FAILED and _LIMITER_FAILED. No email address or hash, token or hash, link, body,
IP, user agent, password, database detail, provider response, exception class or message,
Stripe identifier or conversation content is logged or returned.

## Verification

tools/verify_email_verification_postgres.py follows the recovery verifier's boundary
(only kalillac_staging as kalillac_staging_app on literal loopback; refuses alternate URLs,
destinations, libpq overrides and optimized Python before connecting; no DDL, real email or
Stripe; bounded connections and workers; deletes only its own tagged users). It has 15
fixed checks and must end with exactly: RESULT: 15/15 checks passed.
