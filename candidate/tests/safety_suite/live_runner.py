"""Gated live-evaluation runner (DESIGN + DRY-RUN ONLY in this phase).

This runner is DISABLED by default and performs NO provider call unless an
explicit owner-supplied flag is set. Running it with no flag, or with the
dry-run flag, calls nothing: it loads the human-review cases, estimates token
usage and a dollar ceiling, and writes a plan. A real evaluation would call
the actual in-process ``chat()`` with the production Luna configuration, never
the public Kalillac route, never anonymous allowances, and with recorded
search fixtures by default.

Safety invariants (asserted before any live call would be made):

* ``SAFETY_LIVE_EVAL=enabled`` must be set explicitly by the owner.
* A per-run request cap (``SAFETY_LIVE_MAX_REQUESTS``) and a dollar ceiling
  (``SAFETY_LIVE_MAX_USD``) must be set; the run aborts before exceeding either.
* Prompts are the synthetic corpus only; no real victim, person or secret.
* Search uses recorded fixtures unless ``SAFETY_LIVE_REAL_SEARCH=enabled``.
* No real API key is embedded here; keys come from the environment at call time.

This module performs no network call at import and defines no pytest test, so
CI never triggers a provider call through it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from tests.safety_suite import loader

# Rough token estimate: a Luna request carries the system prompt plus the
# conversation. The system prompt is ~6k tokens; a case adds a few hundred.
_SYSTEM_PROMPT_TOKENS = 6000
_PER_CASE_OVERHEAD_TOKENS = 400
_OUTPUT_TOKENS = 500
# Placeholder list price; the real figure comes from the owner at run time.
_DEFAULT_INPUT_USD_PER_1K = 0.0
_DEFAULT_OUTPUT_USD_PER_1K = 0.0


@dataclass
class LivePlan:
    total_cases: int
    live_cases: int
    est_input_tokens: int
    est_output_tokens: int
    est_usd: float
    enabled: bool
    max_requests: int | None
    max_usd: float | None
    real_search: bool
    blockers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__ | {"blockers": list(self.blockers)}


def _chars_to_tokens(chars: int) -> int:
    return max(1, chars // 4)


def _live_cases() -> list[dict]:
    """Cases whose verdict needs model judgment (human_review)."""
    return [c for c in loader.cases() if c["human_review"]]


def build_plan() -> LivePlan:
    enabled = os.getenv("SAFETY_LIVE_EVAL", "").strip().lower() == "enabled"
    real_search = os.getenv("SAFETY_LIVE_REAL_SEARCH", "").strip().lower() == "enabled"
    max_requests = os.getenv("SAFETY_LIVE_MAX_REQUESTS")
    max_usd = os.getenv("SAFETY_LIVE_MAX_USD")
    in_price = float(os.getenv("SAFETY_LIVE_INPUT_USD_PER_1K", _DEFAULT_INPUT_USD_PER_1K) or 0.0)
    out_price = float(os.getenv("SAFETY_LIVE_OUTPUT_USD_PER_1K", _DEFAULT_OUTPUT_USD_PER_1K) or 0.0)

    live = _live_cases()
    conv_chars = sum(len(t["content"]) for c in live for t in c["turns"])
    est_in = len(live) * (_SYSTEM_PROMPT_TOKENS + _PER_CASE_OVERHEAD_TOKENS) + _chars_to_tokens(conv_chars)
    est_out = len(live) * _OUTPUT_TOKENS
    est_usd = round(est_in / 1000 * in_price + est_out / 1000 * out_price, 4)

    blockers: list[str] = []
    if not enabled:
        blockers.append("SAFETY_LIVE_EVAL is not set to 'enabled' (owner gate).")
    if max_requests is None:
        blockers.append("SAFETY_LIVE_MAX_REQUESTS is not set (request cap required).")
    if max_usd is None:
        blockers.append("SAFETY_LIVE_MAX_USD is not set (dollar ceiling required).")
    if max_requests is not None and int(max_requests) < len(live):
        blockers.append(f"request cap {max_requests} is below {len(live)} live cases.")
    if max_usd is not None and est_usd > float(max_usd):
        blockers.append(f"estimated ${est_usd} exceeds the ceiling ${max_usd}.")

    return LivePlan(
        total_cases=len(loader.cases()),
        live_cases=len(live),
        est_input_tokens=est_in,
        est_output_tokens=est_out,
        est_usd=est_usd,
        enabled=enabled,
        max_requests=int(max_requests) if max_requests is not None else None,
        max_usd=float(max_usd) if max_usd is not None else None,
        real_search=real_search,
        blockers=blockers,
    )


def dry_run(out_path: str | None = None) -> dict:
    """Produce a token and cost estimate plan WITHOUT any provider call."""
    plan = build_plan().as_dict()
    plan["would_run_live"] = plan["enabled"] and not plan["blockers"]
    plan["mode"] = "dry-run (no provider calls made)"
    payload = json.dumps(plan, indent=2)
    if out_path:
        Path(out_path).write_text(payload + "\n", encoding="utf-8")
    return plan


def run_live():
    """Placeholder for the gated live evaluation. This phase does not implement
    provider calls; it refuses unless the owner gate and bounds are set, and
    even then returns a not-implemented marker so no accidental call is made."""
    plan = build_plan()
    if plan.blockers:
        raise RuntimeError("Live evaluation is gated: " + "; ".join(plan.blockers))
    raise NotImplementedError(
        "The live evaluation body is intentionally not implemented in this phase. "
        "It is approved as design only; implement it under owner supervision with "
        "the production Luna configuration, in-process chat(), recorded search "
        "fixtures, and the request/dollar bounds enforced here."
    )


if __name__ == "__main__":
    print(json.dumps(dry_run(), indent=2))
