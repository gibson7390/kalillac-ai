# I2: image generation, design only

For the owner and for Codex. Nothing in this document is built or activated. No image-generation model is
selected, no price is assumed and no allowance is set: those are owner decision 15 (model, cost,
allowance) and decision 16 (whether an image counts as one response against a tier's allowance). The
recommendation for the V1 launch stays **off**. This design exists so that turning it on later is a
reviewed configuration change plus one bounded implementation step, not a new architecture.

## What it must preserve

* **One assistant.** Image generation is a capability of Kalillac, used by GPT-6 Luna as a tool inside the
  conversation, never a model picker or a second assistant. The customer never chooses a model.
* **No new paid infrastructure without the owner.** The intended provider is the one Kalillac already
  uses, under the same account and key handling; any other provider is an owner decision.
* **Every billable call passes Financial Safeguards**, before dispatch, with a worst-case reservation, and
  settles against what the provider reports.
* **Capability first.** Kalillac fixes the size and quality per tier so cost is bounded, and spends no
  call on retries the customer did not ask for.

## How a request would flow

1. The customer asks in the chat ("make an image of …"), optionally attaching an image to edit or use as
   a reference (I1 already produces Kalillac's clean copy of any attachment).
2. On a Premium account with the feature on, the file assistant offers GPT-6 Luna one more tool,
   `create_image {prompt, size?}`. The tool is absent otherwise, so the model cannot call it.
3. Kalillac validates the request: prompt length; size from a fixed per-tier list (the model cannot ask
   for more); at most `MAX_IMAGES_PER_REQUEST` (proposed 1 for V1); the attached references limited to
   the customer's own clean copies.
4. **Before dispatch**, inside the request's provider-control scope:
   * the PAID evidence for this request must exist (the B2 seam: V1-verified, paid, the metered
     account); anonymous and Free requests never reach the tool;
   * the tier's daily image allowance is reserved like a chat slot (decision 15/16 sets the number);
   * Financial Safeguards reserves the worst case for the fixed size and quality (below) within the PAID
     request ceiling, the image line of the provider budget, and the global day and month ceilings.
5. One call to the provider through the bounded transport (timeout, size-capped response, no retry
   after the provider accepted the request).
6. The returned image goes through the same worker sanitizer as an upload (`kalillac_files/images.py`):
   decoded under limits, re-encoded by Kalillac, at most 2048 px. Whether the provider's provenance
   metadata (C2PA) is kept is an owner choice, recommended **kept**: the sanitizer would copy exactly
   that manifest and nothing else, and the image is labelled AI-generated in the product either way.
7. The image is stored as a `generated` file of the account and shown in the reply with a preview, like
   any file Kalillac makes; the reservation settles from the provider's reported usage.

## Cost control

| Control | Mechanism | Value |
|---|---|---|
| Who | PAID evidence only (B2); feature flag default off | owner activates |
| How big | per-tier fixed size and quality list; the model cannot exceed it | owner (decision 15) |
| How many per request | `MAX_IMAGES_PER_REQUEST` | proposed 1 |
| How many per day | the tier's image allowance, reserved and settled like chat slots | owner (decisions 15, 16) |
| Worst-case reservation | per-image price for the fixed size and quality, or output-token bound × output rate for a token-billed model, plus the prompt's input bound (the existing body-bytes rule; reference images at the I1 per-image bound) | prices confirmed by the owner (decision 2) |
| Spend ceilings | the PAID request ceiling; an image line in the provider's day and month budgets (0016); the global ceilings | owner (decision 1) |
| Settlement | from the provider's reported usage; a response above the reservation is flagged, never silently undercharged | existing settlement |

New settings (all required when the feature is on; startup refuses otherwise):
`KALILLAC_IMAGE_GENERATION_ENABLED`, `KALILLAC_PRICE_IMAGE_MODEL`, the price settings for the chosen
model's billing unit, `KALILLAC_IMAGE_SIZES_PAID`, `KALILLAC_USAGE_LIMIT_PAID_DAILY_IMAGES`. No default for
any of them.

## Safety

* The shared safety policy applies to the prompt; the provider's own moderation applies to the call.
  A provider refusal is shown as a fixed refusal, never retried with a reworded prompt.
* Refused by Kalillac before any call: images of a real, identifiable person; sexual content; editing an
  attached photo of a person; prompts asking to imitate a living artist's style by name or a trademark.
* Every generated image is labelled as AI-generated in the product.
* Nothing about the prompt or image is logged; failures log a class name and a stable code only.

## What is built already that this reuses

The worker sanitizer and storage (I1), the per-image input bound for reference images (I1), the paid
seam (B2), provider controls and Financial Safeguards (F1), the file store and reply chips (D2f), and the
chat's preview of a stored image (I1).

## Activation checklist (none of it done)

1. Owner decision 15: model, sizes per tier, daily image allowance; decision 16 settled for images.
2. Prices confirmed against the provider's current list (decision 2); the image budget line set
   (decision 1).
3. Implement the tool, the reservation rule and the allowance slot behind the flag, with tests on the
   real worker and PostgreSQL; Codex review.
4. Staging: measure real usage per size; confirm the reservation is never below the settlement.
5. Owner acceptance in staging; production activation only on the owner's instruction.
