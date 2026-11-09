"""Load the versioned safety/helpfulness corpus and resolve its placeholders.

The corpus is data. Two assistant-turn placeholders are resolved against the
live application constants so a conversation in history matches what Kalillac
actually produced: ``{CRISIS_SELF_RESPONSE}`` and ``{CRISIS_CONCERN_RESPONSE}``.
Nothing else is interpolated.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import app_fastapi_candidate as app

CORPUS_PATH = Path(__file__).resolve().parent / "corpus_v1.json"

_PLACEHOLDERS = {
    "{CRISIS_SELF_RESPONSE}": app.CRISIS_SELF_RESPONSE,
    "{CRISIS_CONCERN_RESPONSE}": app.CRISIS_CONCERN_RESPONSE,
}

ANSWERING_ROUTES = frozenset({
    "general", "code", "web_search", "logic", "calculator", "self_knowledge",
    "followup", "revision", "debug", "personal", "unclear", "memory",
    "code_continuation", "code_history", "memory_save",
})


def _resolve(text: str) -> str:
    for key, value in _PLACEHOLDERS.items():
        text = text.replace(key, value)
    return text


@lru_cache(maxsize=1)
def load_corpus() -> dict[str, Any]:
    raw = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    for case in raw["cases"]:
        for turn in case["turns"]:
            turn["content"] = _resolve(str(turn["content"]))
    return raw


def cases(category: str | None = None):
    rows = load_corpus()["cases"]
    if category is not None:
        rows = [c for c in rows if c["category"] == category]
    return rows


def last_user_message(case: dict) -> str:
    for turn in reversed(case["turns"]):
        if turn["role"] == "user":
            return turn["content"]
    return ""


def prior_history(case: dict) -> list[dict]:
    """The conversation history before the final user turn, in the
    {'role','content'} shape the application accepts."""
    turns = case["turns"]
    for i in range(len(turns) - 1, -1, -1):
        if turns[i]["role"] == "user":
            return [dict(t) for t in turns[:i]]
    return [dict(t) for t in turns[:-1]]
