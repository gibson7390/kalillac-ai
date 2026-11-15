# Reasoning modes

Lets a customer choose how much reasoning Kalillac applies to a message: **QUICK**, **BALANCED**
(the default experience) or **DEEP**. Kalillac remains one assistant on one model, GPT-6 Luna. A
mode never selects another model.

Status: **backend implemented and development verified, off in production.** The interface
control, saved preferences and the product mapping are not done (see "Not done").

## What a mode changes

A mode maps to the Responses API parameter `reasoning.effort` and, optionally, to extra output
tokens so the additional reasoning does not crowd out the visible answer. Nothing else in the
request changes.

Supported efforts are the ones OpenAI's model page lists for `gpt-6-luna` (read 2026-11-14):
`none`, `low`, `medium` (the provider's default), `high`, `xhigh`, `max`. No other value can be
configured, and modes cannot be enabled while another model is configured, because that model's
supported values have not been verified.

## Request

`POST /api/chat` accepts an optional `"reasoning_mode": "quick" | "balanced" | "deep"`.

| Situation | Result |
|---|---|
| Modes off (production today) | The field is ignored, whatever it holds, exactly as any unknown field is |
| Field absent, or the default mode | The request is byte-for-byte what it is without modes |
| An allowed non-default mode | That mode's effort and output allowance are sent |
| A mode the request's tier may not use | `403 reasoning_mode_not_allowed`. Never silently downgraded |
| Any other value | `422 invalid_request` |

A refusal happens at the boundary, before any quota, work or provider attempt, and does not use a
free daily response.

The permission check is in the backend. No request is treated as paid yet: nothing issues a paid
entitlement until billing is integrated, so a mode reserved for Premium is refused for everyone.

## Spending

The mode is applied at the single OpenAI seam, outside the financial reservation. With Financial
Safeguards on, the reservation therefore prices the payload actually sent, including a mode's
extra output tokens, and that worst case counts toward the scope, provider and per-request dollar
ceilings like any other attempt. A mode cannot be used to pass a ceiling
(`test_a_mode_cannot_pass_the_request_or_scope_ceilings`).

Kalillac's description of its own configuration reports the effort the current request uses.

## Configuration

Off unless `KALILLAC_REASONING_MODES_ENABLED=true`. When on, all nine settings are required, any
unrecognised `KALILLAC_REASONING_MODE_*` / `KALILLAC_REASONING_MODES_*` variable is refused, and an
inconsistent mapping stops start-up. There is no mapping in source.

| Variable | Meaning |
|---|---|
| `KALILLAC_REASONING_MODE_{QUICK,BALANCED,DEEP}_EFFORT` | The effort each mode sends. Must not decrease from QUICK to DEEP |
| `KALILLAC_REASONING_MODE_{QUICK,BALANCED,DEEP}_EXTRA_OUTPUT_TOKENS` | Added to the request's `max_output_tokens`, 0 to 120,000; the total is capped at the model's 128,000 |
| `KALILLAC_REASONING_MODE_DEFAULT` | The mode used when a request selects none |
| `KALILLAC_REASONING_MODES_FREE`, `KALILLAC_REASONING_MODES_PAID` | Comma-separated modes each tier may use. FREE must be within PAID and include the default |

The default mode must reproduce today's request: its effort must equal `OPENAI_REASONING_EFFORT`
and it must add no output tokens. Enabling modes therefore changes nothing for a request that does
not select one, including Spaces sends, which do not carry a mode yet.

## Things to decide or check before enabling

* **Owner:** which effort each mode uses, the output allowance for DEEP, and which modes Free and
  Premium may use.
* **Operations:** higher effort takes longer. The request deadline and the OpenAI call timeout
  must allow a DEEP response, or DEEP requests will end as `request_timeout`.
* **Financial Safeguards:** the request ceiling must admit a DEEP attempt's reservation, which is
  larger by its extra output tokens at the output rate.
* **Provider:** confirm in OpenAI's reasoning guide that `max_output_tokens` bounds reasoning and
  visible tokens together. The output allowance exists on that understanding; the model page does
  not state it.
* **Release tooling:** `deploy/release` release facts do not list this flag. A release that
  enables it needs the flag added to its reviewed flag policy.

## Not done

* The control in the chat interface.
* Saving a signed-in customer's preferred mode (needs accounts).
* Paid permissions taking effect (needs verified billing entitlements on the chat request).
* A mode on Spaces sends.

## Tests

`tests/test_reasoning_modes.py` (52): configuration refusals, tier permissions, the exact payload
sent, isolation between requests, the `/api/chat` boundary, start-up, and the interaction with
Financial Safeguards.
