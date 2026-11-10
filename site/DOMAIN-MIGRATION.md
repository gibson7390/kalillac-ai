# Canonical public domain migration — review notes

Reviewed October 27, 2026 against base 440924a9a5ec4083689be9c72cdc8b9d7119e30e.

The intended canonical product domain is https://kalillac.ai. The standalone chat remains /app/.
The legacy redirect domain is kalillac.com; it remains operational until the final verified redirect cutover.
The www alias is an infrastructure redirect to the apex, not an application redirect.

## Pre-edit audit of tracked files

All 26 former-host occurrences were categorized before editing:
- site/src/App.tsx: five occurrences in Privacy domain/infrastructure/analytics copy and Terms.
- candidate/app_fastapi_candidate.py: two user-facing HTTPS/self-description strings.
- candidate/tests/test_homepage_handoff.py: two exact legal-copy expectations.
- candidate/tests/test_billing.py: eight disabled-billing return URL fixtures, including deliberately invalid HTTP/FTP cases.
- candidate/tests/test_billing_concurrency.py: six return URL fixtures, including versioned URLs.
- candidate/tests/test_stripe_gateway.py: three disabled-billing return URL fixtures.

Absolute URL audit: 7 public external links in App.tsx; 11 provider endpoints/example links
in four candidate implementation/tool files; 215 test/fixture URLs in 36 test files;
21 vendored library/license URLs in six files; 189 registry/metadata URLs in package-lock.json.
Only the former-host identity and fixture URLs change. Third-party destinations, provider
endpoints, test loopback/invalid hosts, dependency metadata and vendored assets remain intact.

No tracked canonical URL, Open Graph URL/image, Twitter image, structured data, sitemap,
robots source or web manifest existed. The chat already has noindex metadata. No explicit
application CORS, trusted-host or CSP host allowlist requires migration. The browser uses
same-origin /api/chat and /app/; the visitor cookie is Secure, HttpOnly, SameSite=Lax,
Path=/, without Domain. No new allowlist, redirect or cookie behavior is introduced.

## Source and build ownership

src/public-identity.ts defines the apex origin and public page metadata.
public-metadata.ts builds canonical/social metadata, flat privacy.html and
terms.html entries, robots.txt and sitemap.xml. Client route changes update metadata.
The sitemap contains only /, /privacy and /terms; /app/ remains separately served and noindex.

Only robots.txt and sitemap.xml move out of deployment-preserve-live-assets.txt.
All established icons, the web manifest and the verification file remain protected.
Those files exist only on the server: their contents cannot be inspected or changed in
this local-only task. The existing logo supplies the social image. No icon/manifest is
invented and no production state is queried.

Flat HTML entries avoid introducing route directories that could trigger trailing-slash
redirects. Client-side route metadata remains in place, but SPA fallback alone does not
satisfy the production deployment contract below.
No deployment, infrastructure operation, commit or push is part of this change.

## Production deployment contract

The production Nginx configuration must serve the generated flat legal pages at the
public routes:

| Public route | Generated entry |
| --- | --- |
| /privacy | /privacy.html |
| /terms | /terms.html |

These mappings must expose the generated canonical and social crawler metadata in the
initial HTML response, without relying on client-side JavaScript.

Account pages are not public routes: they are noindex, absent from the sitemap and not
linked from the homepage. The build emits one flat, noindex /account.html entry for
them. Only when accounts are activated after a separate review, the server should serve
/account.html for /account and for every path under /account/, without redirects or
trailing-slash rewrites. Credentials reach those pages only in the URL fragment, which
browsers never send to the server.

Preserve every server-owned favicon, icon, web manifest and verification asset listed
in deployment-preserve-live-assets.txt. Deploy the build-owned robots.txt and sitemap.xml
from the new build; their formerly preserved server copies must not override the new files.

A cache-busted production check confirming that no Cloudflare browser RUM beacon is
injected is a release gate. As reported for this source review, browser RUM has been
explicitly disabled for all incoming requests on kalillac.ai, and a cache-busted live
verification returned RUM_BEACON_DISABLED. This is supplied verification evidence;
repeat the cache-busted check against the deployed release before completing cutover.

The future kalillac.com redirect must preserve normal GET paths and query strings.
Do not blindly apply a 301 redirect to /api/ endpoints or webhook POST requests: a 301
can change POST behavior. API and webhook compatibility must remain deliberately proxied
or otherwise handled until those old-domain endpoints are formally retired.

The anonymous visitor cookie is host-only and will not transfer between kalillac.com and kalillac.ai; visitors receive a one-time new anonymous identity on the new domain.
The allowance remains 10 successful responses per browser per UTC day.

## Architecture and legal copy

The source default is gpt-6-luna via OpenAI with no model-provider fallback. Search policy
supports the supplied Brave/Tavily production configuration; its development default is
Tavily without fallback. Anonymous allowance is opt-in in source, requires PostgreSQL
and the request budget, and validates exactly 10. Accounts/billing remain opt-in and untouched.
These defaults are configuration contracts, not evidence of live production flag values.

The supplied production configuration and hosting identity inform the public copy;
the implementation supports them and no conflicting architecture was found. No environment
file, credentials, certificates or production settings were read or modified.
Privacy and Terms now identify the current allowance as active, name SHA-256/PostgreSQL
and DigitalOcean, and retain the limits on privacy, retention, availability and provider
commitments. Search prose describes fallback/verification without stale attempt counts
or disclosing routing details. Both material reviews are dated October 27, 2026.
