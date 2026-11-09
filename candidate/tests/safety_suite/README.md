# Safety and Helpfulness Regression Suite

A permanent, offline, data-driven suite that measures both **unsafe compliance**
(false negatives) and **excessive refusal** (false positives) for Kalillac's
chat safety path. It runs with no network and no provider call.

## Layout

- `corpus_v1.json` — versioned synthetic corpus (regenerate with
  `python -m tests.safety_suite.build_corpus` from `candidate/`).
- `build_corpus.py` — deterministic generator. Harmful-intent cases are neutral
  descriptors, never operational prompts; the offline suite never calls a model.
- `loader.py` — loads the corpus and resolves the two crisis-reply placeholders.
- `test_corpus_integrity.py` — schema, tiers, unique ids, synthetic-hosts-only,
  no operational payloads, no real credentials.
- `test_offline_behavior.py` — crisis detection, routing, pasted-content search
  privacy, structural injection framing, history sanitization, route parity and
  the provider-refusal contract.
- `test_engagement_pytest.py` — permanent pytest form of the engagement /
  false-positive-refusal checks that previously lived only in production code.
- `test_refusal_end_to_end.py` — provider refusal becomes the fixed reply
  through `chat()` on both generation paths, with no second provider request.
- `live_runner.py` — **gated, design-only** live-evaluation runner. Disabled by
  default; makes no provider call without `SAFETY_LIVE_EVAL=enabled` plus a
  request cap and dollar ceiling. `python -m tests.safety_suite.live_runner`
  prints a dry-run token and cost estimate without calling anything.
- `test_live_runner_gating.py` — proves the runner refuses to go live without
  the owner gate and bounds.

## Tiers

`ANSWER`, `NARROW_BOUNDARY`, `FULL_BOUNDARY`, `CLARIFY`, `CRISIS_FIXED`.

## What is offline vs. live

Offline (here, deterministic): crisis routing and game/fiction controls,
benign high-risk vocabulary routing, pasted-content search privacy, prompt
assembly and route parity, retrieved-content structural injection, system-role
rejection, provider-refusal contract, and source/link sanitization wiring.

Live (gated, design only): model wording for ambiguous distress, harmful-intent
boundaries, authority claims, extraction, fabricated-history escalation, and
multi-turn persistence. These cases are marked `human_review` and are scored by
two independent reviewers against each case `rubric`, never by a model grader
alone.
