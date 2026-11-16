# Milestone 3: from a verified subscription to the PAID scope and Premium reasoning (design)

Written from the code at `fix/codex-reverification-2` before any change. Codex finding G2: the chat
passes no paid evidence to Financial Safeguards and calls `reasoning_modes.begin(..., paid=False)`.

## What already exists

* **The verified source.** Stripe webhooks are reconciled by `kalillac_billing.service` into the
  account's single `account_entitlements` row (`tier`, `expires_at` from the subscription period);
  `kalillac_accounts.entitlements.effective_entitlement(row, now)` turns it into `FREE` or `PAID`, failing
  closed for a missing, expired or unknown row. A success redirect never grants paid (tested).
* **The evidence type.** `kalillac_finance.traffic.issue_paid_entitlement(request, effective)` issues an
  opaque `PaidEntitlement` bound to one request, only from an authoritative paid `EffectiveEntitlement`;
  `resolve_traffic_class` returns `PAID_ACCOUNT` only for evidence bound to the same request.
* **The read that already happens.** The usage quota (`kalillac_accounts.quota.UsageQuota._limit`) reads
  the effective entitlement inside the transaction that reserves a signed-in account's chat.
* **Two structural tests** (`test_finance_ledger.py`, `test_reasoning_modes.py`) assert the application
  issues no paid evidence. They mark exactly the lines this milestone changes.

## Design

1. **One read, one decision.** The quota reservation records the `EffectiveEntitlement` it used. That
   value, from the transaction that counts the chat, is the request's paid status; no second query, no
   cookie, no client field.
2. **Financial scope.** In the chat dispatch, `provider_controls.account_status(authenticated, effective)`
   issues the `PaidEntitlement` itself, bound to the current request state, so the binding never leaves
   provider controls. A file request does the same from its own account lookup.
3. **Reasoning modes.** A mode reserved for paid requests must be refused before any spending. The mode
   is therefore resolved after the quota reservation (which is the first and cheapest step) and a refusal
   releases that reservation, using the quota's existing release path. Free requests keep exactly today's
   behaviour.
4. **Default off.** A new setting, `KALILLAC_PAID_SCOPE_ENABLED`, off by default: off, every line behaves
   as today (tested byte for byte), so nothing changes in production until the owner enables it with
   billing.
5. **Fail closed.** Any error reading the entitlement, a missing row, an expired period, a mismatch
   between the account resolved for the quota and for the request, or the setting off: FREE.

## Tests to write

Off: unchanged classification and modes (existing tests). On: a paid account's chat is classified
PAID and charged to the PAID buckets; an expired or canceled subscription is FREE; another request's
evidence is ignored; a paid-only reasoning mode is refused for FREE before any provider call and its
quota released; the file assistant's PAID scope; PostgreSQL concurrency of a cancellation webhook
against an in-flight paid request (the request keeps the status read in its own transaction).

## Open owner decisions it depends on

Which reasoning modes Free and Premium may use (decision 9) and the Premium allowances (decision 10).
The implementation takes both from configuration; nothing is assumed.
