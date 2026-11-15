# Financial Safeguards: proposed budget values, with the arithmetic

Status: **proposal for the owner's decision.** No value here is approved, configured or active.
Financial Safeguards stay off in production, migration 0016 is not applied there, and enforcement is
not enabled. The owner chooses the numbers; this document shows what each choice buys.

## 1. Prices used

| Provider | Price | Status |
|---|---|---|
| OpenAI GPT-6 Luna | input $0.10, cached input $0.01, cache write $0.125, output $0.50 per million tokens; above 272,000 input tokens the whole request at 2x input and 1.5x output | Read from OpenAI's model page on 2026-11-14; confirm on the day of activation |
| Tavily | $ per credit, and maximum credits per search and extract | **Not verified here.** The owner confirms the current plan's price; the budgets below are expressed in dollars, so they hold whatever the price is |
| Brave | $ per request | **Not verified here**, as for Tavily |

## 2. What one request costs

Kalillac reserves the worst case before every model call and returns the unused part when OpenAI
reports usage. Bounds from the code: a message is at most 4,000 characters, history at most 400,000
characters, a reply at most 3,200 output tokens; the fixed instructions are about 12,000 characters.

| Request | Reserved per model call (worst case) | Typically charged |
|---|---|---|
| Ordinary chat message, short history (about 30 KB sent) | about 30,000 input tokens x $0.10/M + 3,200 x $0.50/M = **$0.0046** | about 8,000 in and 800 out: **$0.0012** |
| Chat message with the longest allowed history (about 410 KB) | 410,000 x $0.20/M (long-context rate) + 3,200 x $0.75/M = **$0.084** | Rare |
| A search adds | one Tavily or Brave operation at its configured maximum | owner's price |
| File assistant request (up to 7 calls, 32,000 output tokens each) | the largest single call, about 200,000 in and 32,000 out: **$0.036**; up to 7 calls | about 100,000 in and 20,000 out in total: **$0.02** |

## 3. Proposed values (cautious launch)

A starting point sized for a small launch with a hard monthly worst case the owner can accept. Every
value scales together: doubling the monthly GLOBAL ceiling and keeping the proportions keeps every rule
satisfied.

| Variable | Day | Month | Why |
|---|---|---|---|
| `KALILLAC_FINANCE_*_GLOBAL_LIMIT_USD` | 10 | 150 | The absolute ceiling of spend on all providers |
| `KALILLAC_FINANCE_*_FREE_LIMIT_USD` | 4 | 60 | Free use; about 3,300 typical free messages a day, so about 330 visitors at the 10-message daily allowance |
| `KALILLAC_FINANCE_*_PAID_LIMIT_USD` | 5 | 75 | Premium; about 4,000 typical messages or 250 file requests a day |
| `KALILLAC_FINANCE_MIN_HEADROOM_PERCENT` | 10 | | FREE + PAID (9 a day, 135 a month) sit exactly 10% below GLOBAL, which absorbs an overrun |
| `KALILLAC_FINANCE_*_OPENAI_LIMIT_USD` | 8 | 120 | Most of the spend is the model |
| `KALILLAC_FINANCE_*_TAVILY_LIMIT_USD` | 2 | 30 | Searches; `0` closes Tavily if it is not in use |
| `KALILLAC_FINANCE_*_BRAVE_LIMIT_USD` | 2 | 30 | As for Tavily; `0` if Brave is not configured |
| `KALILLAC_FINANCE_MAX_REQUEST_FREE_USD` | 0.10 | | Covers every ordinary request with searches; a free request with an extreme history is refused (`processing_limit_reached`) rather than costing $0.30 |
| `KALILLAC_FINANCE_MAX_REQUEST_PAID_USD` | 0.50 | | Covers the longest-history requests and file work in flight |

Rules check: FREE + PAID = 9 <= 10 x 0.9 (day) and 135 <= 150 x 0.9 (month); every day value is at
most its month value; each provider ceiling is at most GLOBAL; each request ceiling is at most its
scope's day ceiling.

**Worst case:** spend can never exceed $10 in a UTC day or $150 in a UTC month across all providers,
even under concurrent requests. When a ceiling is reached, new requests get "busy" until the next UTC
day or month.

## 4. Before activation

1. The owner chooses the values (these, scaled, or others) and confirms the three prices.
2. The file assistant's calls go through the same financial wrapper, but they do not yet open a
   request scope of their own. With provider ceilings on, the wrapper therefore refuses them
   (`CeilingUnavailable`): the file path fails closed, never spending outside a ceiling, but is
   unusable until it opens its own request scope with the PAID request ceiling (engineering item,
   recorded in the roadmap).
3. Until billing issues a paid entitlement on the chat request, every request is charged to FREE, so
   the PAID budget is unused at first.
4. The activation steps in `candidate/FINANCIAL_SAFEGUARDS.md` ("Activation proposal") still apply,
   including approval of migration 0016 in production and the release-tooling update.
