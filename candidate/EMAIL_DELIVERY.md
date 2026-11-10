# Transactional email delivery

Account recovery and account email verification send email through a provider-neutral
gateway (kalillac_accounts/email_gateway.py). Resend is the first and only adapter
(kalillac_accounts/resend_gateway.py). It is backend-only and default-off, and production
remains off: this slice includes no production activation, live provider call, webhook
processing, DNS change, UI, account rollout or deployment.

## Activation boundary

The application builds a gateway only while KALILLAC_ACCOUNT_RECOVERY_ENABLED or
KALILLAC_ACCOUNT_EMAIL_VERIFICATION_ENABLED is on. While both are off, nothing
email-related is imported and provider settings are ignored. While either is on, these
must validate before any route is installed, or startup fails naming the setting (never
its value):

- KALILLAC_EMAIL_PROVIDER: exactly `resend`. Blank and unknown values are refused.
- KALILLAC_RESEND_API_KEY: `re_` followed by a nonempty RFC 6750 `b64token` (letters,
  digits and `- . _ ~ + /`, then optional trailing `=`), at most 256 characters. This
  checks only that the value is safe as an HTTP Bearer credential (no whitespace, CR/LF,
  control or other header-unsafe characters); it does not assume Resend's key format or
  estimate entropy.
- KALILLAC_EMAIL_FROM: the existing account-email rules, and one plain address (no
  display name, comma, bracket, quote or space). Messages are sent as
  `Kalillac AI <address>`.

Each feature's own settings (accounts, database, recovery or verification URL) are still
validated by its router. Building the gateway constructs no HTTP client and contacts no
provider. The recovery and verification services know only the gateway protocol.

## HTTP contract

One request per gateway call, no retries:

- POST https://api.resend.com/emails (fixed in code; not configurable by environment, so
  there is no arbitrary outbound-request surface; proxy and certificate environment
  variables are ignored);
- headers: Authorization: Bearer <key>, Content-Type: application/json,
  Idempotency-Key, Accept: application/json, Accept-Encoding: identity;
- JSON body with exactly `from`, `to` (one address), `subject`, `text` and `html`. No
  attachments, CC, BCC, tags, scheduling, templates, audiences or tracking options.

Success is only HTTP 200 with an uncompressed `application/json` body of at most
4096 bytes that is exactly `{"id": "<1-128 letters, digits or hyphens>"}`. The id is
validated and discarded. Any other status (including every redirect, which is never
followed), content type, encoding, size or shape is a failure. If Resend adds response
fields, deliveries fail closed until the adapter is reviewed and updated.

## Bounds

- timeouts: connect 1.5 s, write 1.0 s, read 2.0 s, pool 0.5 s;
- the whole attempt, including DNS, which socket timeouts do not cover, runs in a worker
  thread that the caller waits on for at most 5 seconds;
- at most 4 attempts in flight per process; beyond that, a send fails at once;
- the response body is streamed and reading stops at 4096 bytes or the deadline.

An attempt abandoned at the deadline may still deliver. That is safe: the caller has
already treated it as failed and rolled issuance back, so the link is unusable.

## Idempotency

Keys are `kalillac-email-v1/<purpose>/<hex>`, at most 101 characters:

- password-reset and email-verification: SHA-256 of the version, purpose, recipient and
  link. An explicitly repeated identical message reuses the key; every new link gets a
  new one. The key reveals no address, link, token or key.
- password-changed: a random UUID per notification, because there is no credential or
  stable event id.

No automatic retry exists; the key prepares for a separately reviewed retry policy.

## Messages

Three short transactional messages, each plain text plus self-contained HTML
(kalillac_accounts/email_messages.py): reset password (30-minute single-use link),
password changed (no link; directs the recipient to start account recovery if they
didn't make the change), and verify email (24-hour single-use link). All dynamic values
are HTML-escaped; the credential URL is the only link and is used exactly as given. No
images, fonts, scripts, stylesheets, tracking, link rewriting, marketing copy or
unsubscribe link. Rendered messages are never logged or persisted.

## Errors and privacy

Every ordinary failure (timeout, transport error, refusal, status, malformed or oversized
response, full in-flight slots, invalid recipient or link) raises EmailDeliveryError with
the fixed message EMAIL_DELIVERY_FAILED and no cause or context. The adapter logs
nothing. API keys, addresses, links, tokens, idempotency inputs, rendered content,
provider responses and message ids never appear in exceptions, logs or events, and
nothing from a response is stored. KeyboardInterrupt, SystemExit and other
BaseException values propagate unchanged.

## Transaction semantics (unchanged)

- Password-reset and email-verification delivery stay inside the issuance transaction,
  while the account row is locked; a delivery failure rolls issuance back.
- The password-changed notification is sent after the reset commits; its failure never
  undoes the password change or session revocation.
- Locks, PostgreSQL transaction timeouts, token hashing and lifetimes, throttling and
  rollback behaviour are unchanged.

## Testing

Tests use only injected fake transports (httpx.MockTransport): no API key, DNS lookup,
Resend account or internet request. The test API key value is an obvious fake.
tests/conftest.py also blocks every non-loopback connection.

## Prerequisites for any future activation

1. Verify the sending domain in Resend (SPF, DKIM and DMARC records).
2. Create a sending-only API key restricted to that domain. Never use a full-access key.
3. Install the key as a staging secret outside the repository.
4. Run real delivery tests in staging, including failure and timeout behaviour.
5. Decide a bounce and complaint handling policy (no provider webhooks are processed).
6. Separately review production activation, together with the reset and verification
   pages and shared multi-worker throttling.
