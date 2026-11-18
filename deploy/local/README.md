# Kalillac local staging (this Windows machine, no server)

The complete Kalillac application from `release/staging-v1`, running in WSL on this computer, for the
owner to use and evaluate before launch. Nothing here touches production, needs a server, or costs
anything while providers are simulated.

## Start and stop

* **Start:** double-click `deploy\local\Kalillac-Local.cmd`. The browser opens **http://localhost:8010**.
* **Stop:** `deploy\local\Kalillac-Local.cmd stop` (also when WSL shuts down by itself, just start again).
* **What runs, demo accounts:** `deploy\local\Kalillac-Local.cmd status`.
* **Local panel:** **http://localhost:8010/__local__/** (emails Kalillac sent, simulated Stripe).
* **After a site change** (`npm run build` in `site\`): `deploy\local\Kalillac-Local.cmd refresh-web`.

First time only (about three minutes): build the site once (`npm run build` in `site\`), then
`deploy\local\Kalillac-Local.cmd setup`. It needs WSL with the repository's Python environment
(`~/kf15`, or set `KALILLAC_LOCAL_PYTHON`) and PostgreSQL 16 binaries (`~/pg16tools`, or
`KALILLAC_LOCAL_PG_BIN`); both already exist on this machine.

## What is real and what is simulated

| Part | Locally |
|---|---|
| Application, account pages, chat, Spaces, files, images, reasoning modes | **Real** (the release's code, its own controlled launcher) |
| Database | **Real** PostgreSQL 16, its own cluster (port 55433, data in `/var/lib/kalillac-local-staging`) |
| Accounts and security | **Real**: email-first registration, V1 account security after the real operator transition, sessions, password change, deletion request |
| Daily allowances, request budgets, Financial Safeguards (ledger reservation and settlement), provider ceilings | **Real** mechanisms, with **synthetic test values** (below) |
| File generation and editing | **Real** isolated worker and editor (PDF, Word, Excel, PowerPoint), the total check and the exact-edit confirmation |
| Image upload | **Real** checks and clean re-encoding by the worker |
| GPT-6 Luna (OpenAI) | **Simulated** unless a real key is added: replies say "Simulated GPT-6 Luna"; for files it drives the real tools with the repository's sample documents, retitled from your request; it does not look at images |
| Brave and Tavily search | **Simulated** unless real keys are added: labelled sample results |
| Stripe | **Simulated** unless test keys are added: the panel stands in for Stripe's hosted Checkout and portal, and sends Stripe's events, signed with a local test secret, to the application's real webhook route (Stripe's own signature check, reconciliation and entitlement run for real) |
| Email (Resend) | **Simulated** unless a key is added: every email lands in the panel's outbox, with its links |

A simulated provider's address cannot be reached from local staging at all: its host name is blocked
inside the application's process.

**Synthetic values, not decisions:** Free 20 and Premium 200 responses a day; Financial Safeguards
ceilings of $100 a day ($20 Free, $70 Premium), $1,000 a month, at most $1 per Free and $5 per Premium
request; Free gets Quick and Balanced, Premium also Deep; an image is reserved as 3,000 input tokens;
provider prices are placeholders. They are the CI boot verifier's test inputs, labelled at the top of
`/var/lib/kalillac-local-staging/settings.env`. The commercial values remain the owner's decisions.

## What to try

Two demo accounts are created at setup (passwords: `status`): **demo.free@kalillac-local.test** and
**demo.premium@kalillac-local.test**. Or create your own on the site; its email arrives in the panel's
outbox, and `Kalillac-Local.cmd spaces-allow you@example.com` gives it Spaces.

* **Chat** at http://localhost:8010/app/ with Quick, Balanced or Deep (Deep is refused on Free, with the
  plan's message).
* **Files** (Premium): "Make a PDF report about …", "… a Word document", "… an Excel model", "… a
  PowerPoint deck". The real worker renders them; download from the chat.
* **Editing** (Premium): attach a Word, PowerPoint or Excel file and write "Replace A with B". If the change
  would leave a total row wrong, Kalillac holds it back and offers **Apply exactly as asked**.
* **Images** (Premium): attach a PNG or JPEG; it is checked, re-encoded and stored (understanding needs
  a real OpenAI key).
* **Spaces** (the Spaces button in the chat): create one, open a conversation, send; it is saved to the
  account. Rename, instructions, delete, earlier messages.
* **Account** at http://localhost:8010/account: plan, billing, sessions, password change, the deletion
  request (this ends the demo account).
* **Subscription:** signed in as the Free account, open the panel, **Start a simulated Checkout**, **Pay**:
  you return to the account page as Premium. The panel's portal buttons cancel at period end, resume, or
  end the subscription.

## Using real providers (optional)

Real providers make local staging send real requests, at their prices. Put keys in WSL, as root:

* `/var/lib/kalillac-local-staging/secrets/provider-keys.env`, any of: `OPENAI_API_KEY=…`,
  `BRAVE_API_KEY=…`, `TAVILY_API_KEY=…`, `KALILLAC_RESEND_API_KEY=…` (each provider with a key stops
  being simulated at the next start; mode 0600).
* **Stripe test mode** (free): in `secrets/stripe.env`, `STRIPE_SECRET_KEY=sk_test_…`,
  `STRIPE_PRICE_ID=price_…` (a test recurring Price) and `STRIPE_WEBHOOK_SECRET=whsec_…` from
  `stripe listen --forward-to localhost:8010/api/billing/stripe/webhook` (the free Stripe CLI, left running).
  Stripe values are written at setup, so after adding them: `reset --yes`, then `setup`. The account page's
  Upgrade and Manage billing buttons then use Stripe's real hosted test pages (card 4242 4242 4242 4242).

## What simulation proves, and what it does not

**Verified locally with simulated providers** (2026-11-18, browser runs against http://localhost:8010):
sign-in through V1 with the Secure cookie on localhost; the account page; a chat reply through the real
pipeline; a PDF generated by the worker and downloaded; an image checked, re-encoded and sent; Deep refused
on Free; a simulated Checkout reaching Premium through signed webhooks and the account page's return
notice; a Space with a saved conversation; cancel at period end and end now through the simulated portal;
a Word edit held back by the total check, then applied after the customer's confirmation.

**Not verified without real providers:** the quality of GPT-6 Luna's answers, documents and image
understanding; real search results; Stripe's hosted pages and real webhook delivery; email delivery by
Resend. Those need the real keys above (or the server staging run).

## Reset

`Kalillac-Local.cmd reset --yes` deletes local staging's database and files (only
`/var/lib/kalillac-local-staging`); `setup` builds it again. Chinese, Japanese and Korean PDFs need the large
CJK fonts once: in WSL, `cd candidate && ~/kf15/bin/python tools/fetch_document_fonts.py`.
