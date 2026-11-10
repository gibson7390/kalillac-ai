# Account recovery foundation

This is backend-only and default-off, and production remains off. No reset page,
automatic sign-in, account deletion, password-change endpoint, MFA or production
activation is included. Email goes through the separately reviewed, default-off Resend
adapter described in EMAIL_DELIVERY.md. Chat, temporary conversation memory, drafts, uploads, billing,
entitlements and usage are outside recovery.

## Configuration and email boundary

KALILLAC_ACCOUNT_RECOVERY_ENABLED defaults off. No recovery route is mounted while off.
When enabled, accounts and the database must also be enabled, and these settings must
validate before routes are installed:

- KALILLAC_ACCOUNT_RECOVERY_URL: absolute HTTPS; HTTP only for literal loopback or
  localhost development. No credentials, query string or fragment.
- KALILLAC_EMAIL_FROM: the existing normalized account-email format.
- An EmailGateway implementing send_password_reset(to, link) and
  send_password_changed(to).

The application builds the gateway only through email_provider.build_email_gateway,
which requires KALILLAC_EMAIL_PROVIDER=resend and a valid KALILLAC_RESEND_API_KEY (see
EMAIL_DELIVERY.md); anything missing or invalid fails startup before routes are installed.
The recovery service stays provider-neutral: it only calls the gateway protocol. Startup
contacts no provider. Production activation still requires a separate review.

## HTTP contracts

Both POST endpoints require application/json and bounded streamed bodies up to 4096
bytes, with Cache-Control: no-store on all handled responses. Unknown fields and
non-object/missing-field bodies return 400 invalid_body; malformed JSON returns
400 invalid_json; wrong content type returns 415 json_required; oversized bodies return
413 body_too_large.

POST /api/account/recovery/request accepts exactly {"email":"..."}.
Email is normalized with the existing account rules; invalid email is 422 invalid_email.
Client throttling happens before scheduling work. Known, unknown and inactive accounts,
and email-throttled requests, return exactly 202 {"ok":true}. Lookup, issuance and delivery
run in a contained after-response background operation. Ordinary background failures are
contained without Uvicorn tracebacks.

POST /api/account/recovery/confirm accepts exactly {"token":"...","password":"..."}.
Canonical 32-byte URL-safe token encoding is validated before database access.
Existing password limits remain 12 through 256 characters. Invalid password type is
422 invalid_password; out-of-range length is 422 password_length. All malformed, unknown,
expired, consumed, replayed, wrong-purpose and inactive-user tokens share
400 {"error":"invalid_or_expired_token"}. Operational failures, including database timeouts,
return only 503 {"error":"recovery_unavailable"} after rollback.

Success is exactly 200 {"ok":true}, with the current account cookie cleared using the
existing Secure/HttpOnly/SameSite/Path settings and no Domain attribute. It creates no
account session and never signs in. Normal registration, logout and restoring paid
access through the same user_id are unchanged.

Login gains one step: after Argon2 verification it takes the same users-row lock as a
reset and rechecks the password hash and active flag. A reset that commits while a
login with the old password is verifying therefore either revokes that login's session
or makes the login fail with the usual 401 invalid_credentials; no session created
with a replaced password survives the reset.

## Persistence and transactions

Migration 0008 adds account_recovery_tokens with only UUID id, user_id, password_reset
purpose, unique 64-character lowercase SHA-256 token_hash, created_at, expires_at and
nullable consumed_at. Its user foreign key cascades, and user_id is indexed. The runtime
role receives SELECT, INSERT and UPDATE on this table; token DELETE and DDL are not granted.
No raw token, link, body, email, IP, user agent, password, provider result or conversation
content is stored in a recovery row.

Tokens use 32 cryptographically random bytes, canonical unpadded URL-safe encoding, and
a fixed 30-minute lifetime. Only the SHA-256 digest of the encoded token is stored.
Issuance locks the account row and invalidates its earlier unused reset tokens atomically.
Delivery receives the configured URL plus #token=<encoded-token>; credentials never
appear in query strings, route paths or public API responses.

Confirmation hashes the new password with existing Argon2 outside the database
transaction. Inside one transaction it locks the users row, checks the clock after any
lock wait, conditionally consumes an unexpired unused password_reset token, updates only
that user's password hash, invalidates sibling tokens and revokes every unrevoked
AccountSession for that user. Other users, identity fields, billing, entitlement and usage
rows are untouched. One-token and two-token races for the same account have one winner.

Every recovery transaction uses fixed PostgreSQL SET LOCAL lock_timeout=5s and
statement_timeout=30s before any work. These settings expire with the transaction and do
not change pooled session defaults. SQLite is the explicit test-only exception. Unknown
database dialects fail closed. Errors/conflicts roll back the complete mutation.

Reset-email delivery is inside the issuance transaction, after the public response.
A delivery failure rolls the entire issuance back, leaving no usable failed-delivery
credential even when a separate cleanup query could not run. A commit failure also leaves
the delivered credential unusable. Because the account lock is held until delivery and
commit complete, the gateway must bound its delivery call; the Resend adapter caps each
attempt at about 5 seconds with no retries.
Password-changed notification is outside the transaction after commit, and failure never
undoes the reset.

Recovery emits fixed event codes only, never exception classes/messages or request,
identity, token, password, database or provider details. KeyboardInterrupt, SystemExit
and other BaseException values are not treated as ordinary recovery failures.

## Throttling

Recovery has independent, thread-safe sliding windows and an injectable monotonic clock.
Only hashed identifiers are retained in its in-memory store.

- Request client: 5 attempts per 15 minutes, then 429 with Retry-After.
- Normalized-email hash: 3 requests per hour; excess is a silent 202 with no scheduled work.
- Confirmation client: 10 invalid attempts per 15 minutes, then 429 with Retry-After.
  In-flight attempts reserve capacity. Valid/server-failed attempts release their slot;
  unrelated clients cannot exhaust another client's recovery budget.

The limiter is single-process and loses state on restart. Replace it with a shared
atomic implementation before scaling beyond one worker; it is not distributed enforcement.
Existing login and anonymous-allowance limiters and all production defaults are unchanged.

## Verification and activation boundary

SQLite/API tests cover persistence, paid-identity preservation, session revocation,
rate limits, failure redaction, rollback, races, and migration upgrade/downgrade/offline DDL.
Existing schema contracts advance to 0008 without weakening their approved-table checks.
Subscription CI runs the focused suites with an exact collection-count guard and includes
the previously omitted login-rate-limit suite.

tools/verify_recovery_postgres.py accepts only the disposable kalillac_staging database
as kalillac_staging_app, with a literal loopback peer. It refuses alternate database URLs,
destinations, libpq overrides and optimized Python before connecting. It performs no DDL
or real email/Stripe call, uses bounded connections/workers, and deletes only its own
per-run tagged users (which cascade). CI performs migration 0008 downgrade/upgrade using
the separate migration role. The verifier has 14 fixed checks and must end with exactly:
RESULT: 14/14 checks passed.

This slice has no retention/pruning job or production rollout. The Resend activation
prerequisites in EMAIL_DELIVERY.md, a reset UI, an activation/retention plan and shared
throttling before multi-worker scaling remain prerequisites for future production
recovery.
