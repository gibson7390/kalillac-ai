"""User-selectable reasoning modes: QUICK, BALANCED and DEEP. Default off.

Kalillac stays one assistant on one model. A mode changes only how much
reasoning that model is asked for: it maps to the Responses API's
reasoning.effort and, optionally, to extra output tokens so the additional
reasoning does not crowd out the visible answer.

Supported efforts are exactly those OpenAI's model page lists for the one
model this was verified against (SUPPORTED_MODEL, page read 2026-11-14):
none, low, medium, high, xhigh, max. Enabling modes with any other configured
model is refused: its supported values have not been verified, and no
unsupported parameter is ever sent.

With KALILLAC_REASONING_MODES_ENABLED off, nothing here is read and a request
is exactly as before, whatever it carries. With it on:

  * every setting below must be explicit; there is no default mapping in
    source (which effort each mode uses is a product decision);
  * the default mode must reproduce today's request exactly: its effort must
    equal the configured OPENAI_REASONING_EFFORT and it adds no output tokens.
    A request that selects nothing is therefore unchanged by enabling modes;
  * efforts must not decrease from QUICK to BALANCED to DEEP;
  * every mode FREE may use, PAID may use, and the default is open to FREE;
  * a mode a request may not use is refused, never silently downgraded.

The selected mode travels in a context variable set at the chat boundary and
is applied at the single OpenAI seam, before the financial reservation is
priced, so a mode's worst-case cost is reserved like any other attempt and
counts toward the request's dollar ceiling.
"""
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import re

FLAG = "KALILLAC_REASONING_MODES_ENABLED"
MODES = ("quick", "balanced", "deep")
SUPPORTED_MODEL = "gpt-6-luna"
SUPPORTED_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")  # In increasing order.
# The model's maximum output is 128,000 tokens; a mode may add at most this much.
MAX_EXTRA_OUTPUT_TOKENS = 120_000
MAX_OUTPUT_TOKENS = 128_000

EFFORT_NAMES = {mode: f"KALILLAC_REASONING_MODE_{mode.upper()}_EFFORT" for mode in MODES}
EXTRA_NAMES = {mode: f"KALILLAC_REASONING_MODE_{mode.upper()}_EXTRA_OUTPUT_TOKENS" for mode in MODES}
DEFAULT_NAME = "KALILLAC_REASONING_MODE_DEFAULT"
FREE_NAME = "KALILLAC_REASONING_MODES_FREE"
PAID_NAME = "KALILLAC_REASONING_MODES_PAID"
REQUIRED = (*EFFORT_NAMES.values(), *EXTRA_NAMES.values(), DEFAULT_NAME, FREE_NAME, PAID_NAME)
KNOWN = frozenset((FLAG, *REQUIRED))
_FAMILIES = ("KALILLAC_REASONING_MODE_", "KALILLAC_REASONING_MODES_")
TRUE = frozenset({"1", "true", "yes", "on"})
FALSE = frozenset({"", "0", "false", "no", "off"})
_COUNT = re.compile(r"(0|[1-9][0-9]{0,5})", re.ASCII)

CONFIG = None  # ReasoningModes, or None while modes are off.
_SELECTED = ContextVar("reasoning_mode_selection", default=None)


class ReasoningConfigError(RuntimeError):
    """Refused configuration. Messages name a variable, never a value."""


class ReasoningModeRefused(Exception):
    """A request's mode cannot be used. status and code are the HTTP answer."""

    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


@dataclass(frozen=True)
class Selection:
    mode: str
    effort: str
    extra_output_tokens: int


@dataclass(frozen=True)
class ReasoningModes:
    efforts: tuple  # ((mode, effort), ...) for every mode, in MODES order
    extra_output_tokens: tuple  # ((mode, tokens), ...)
    default: str
    free: frozenset
    paid: frozenset

    def __post_init__(self):
        efforts, extra = dict(self.efforts), dict(self.extra_output_tokens)
        if tuple(efforts) != MODES or tuple(extra) != MODES:
            raise ReasoningConfigError("Every reasoning mode must be configured.")
        if any(effort not in SUPPORTED_EFFORTS for effort in efforts.values()):
            raise ReasoningConfigError("A reasoning mode uses an unsupported effort.")
        order = [SUPPORTED_EFFORTS.index(efforts[mode]) for mode in MODES]
        if order != sorted(order):
            raise ReasoningConfigError("Reasoning efforts must not decrease from quick to balanced to deep.")
        if any(type(v) is not int or not 0 <= v <= MAX_EXTRA_OUTPUT_TOKENS for v in extra.values()):
            raise ReasoningConfigError("A reasoning mode's extra output tokens are outside the supported range.")
        if type(self.free) is not frozenset or type(self.paid) is not frozenset \
                or not self.free <= self.paid <= frozenset(MODES):
            raise ReasoningConfigError("Every mode open to free requests must be open to paid requests.")
        if self.default not in self.free:
            raise ReasoningConfigError("The default reasoning mode must be open to free requests.")
        if extra[self.default] != 0:
            raise ReasoningConfigError("The default reasoning mode must add no output tokens.")

    def selection(self, mode):
        return Selection(mode, dict(self.efforts)[mode], dict(self.extra_output_tokens)[mode])

    def allowed(self, paid):
        return self.paid if paid is True else self.free

    def resolve(self, requested, paid):
        """The Selection for a request, or None when it uses the default mode
        (which leaves the request exactly as it is without modes).

        requested: the request's reasoning_mode value, or None when absent.
        paid: True only for a request with a verified paid entitlement.
        """
        if requested is None:
            return None
        if type(requested) is not str or requested not in MODES:
            raise ReasoningModeRefused(422, "invalid_request")
        if requested not in self.allowed(paid):
            raise ReasoningModeRefused(403, "reasoning_mode_not_allowed")
        return None if requested == self.default else self.selection(requested)


def _flag(environ):
    value = environ.get(FLAG, "")
    value = value.strip().lower() if type(value) is str else None
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    raise ReasoningConfigError(FLAG + " must be true or false.")


def _text(environ, name):
    value = environ.get(name)
    if type(value) is not str or not value.strip():
        raise ReasoningConfigError(name + " must be set explicitly.")
    return value.strip()


def _modes(environ, name):
    values = [part.strip() for part in _text(environ, name).split(",")]
    if len(set(values)) != len(values) or any(value not in MODES for value in values):
        raise ReasoningConfigError(name + " requires a comma-separated list of quick, balanced, deep.")
    return frozenset(values)


def load_reasoning_modes(environ, model, default_effort):
    """ReasoningModes, or None (reading only the flag) while modes are off.

    model and default_effort are the application's configured OpenAI model
    and reasoning effort; the default mode must reproduce the latter.
    """
    if not _flag(environ):
        return None
    if model != SUPPORTED_MODEL:
        raise ReasoningConfigError(FLAG + " is verified for " + SUPPORTED_MODEL + " only.")
    unknown = sorted(name for name in environ if name.startswith(_FAMILIES) and name not in KNOWN)
    if unknown:
        raise ReasoningConfigError("Unsupported reasoning-mode variable: " + unknown[0] + ".")
    efforts, extra = [], []
    for mode in MODES:
        effort = _text(environ, EFFORT_NAMES[mode])
        if effort not in SUPPORTED_EFFORTS:
            raise ReasoningConfigError(EFFORT_NAMES[mode] + " requires one of: " + ", ".join(SUPPORTED_EFFORTS) + ".")
        tokens = _text(environ, EXTRA_NAMES[mode])
        if _COUNT.fullmatch(tokens) is None or int(tokens) > MAX_EXTRA_OUTPUT_TOKENS:
            raise ReasoningConfigError(EXTRA_NAMES[mode] + " requires a whole number within the supported range.")
        efforts.append((mode, effort))
        extra.append((mode, int(tokens)))
    default = _text(environ, DEFAULT_NAME)
    if default not in MODES:
        raise ReasoningConfigError(DEFAULT_NAME + " requires one of: quick, balanced, deep.")
    config = ReasoningModes(tuple(efforts), tuple(extra), default, _modes(environ, FREE_NAME),
                            _modes(environ, PAID_NAME))
    if dict(efforts)[default] != default_effort:
        raise ReasoningConfigError(EFFORT_NAMES[default] + " must equal the configured OPENAI_REASONING_EFFORT.")
    return config


# --- per-request selection ------------------------------------------------------------------


def begin(requested, paid=False):
    """Resolve and bind this request's mode. Returns a token for end(), or
    None when modes are off (the value is then ignored, as any unknown field
    is). Raises ReasoningModeRefused."""
    config = CONFIG
    if config is None:
        return None
    return _SELECTED.set(config.resolve(requested, paid))


def end(token):
    if token is not None:
        _SELECTED.reset(token)


def effective_effort(configured):
    """The effort this request's model calls use: its mode's, else the configured one."""
    selected = _SELECTED.get()
    return configured if selected is None or CONFIG is None else selected.effort


def apply(function):
    """Wrap the OpenAI seam: a request that selected a non-default mode sends
    that mode's effort and output allowance. Applied outside the financial
    reservation, so the reservation prices the payload actually sent."""
    @wraps(function)
    def moded(payload, *args, **kwargs):
        selected = _SELECTED.get()
        if selected is not None and CONFIG is not None and type(payload) is dict:
            payload = {**payload, "reasoning": {**(payload.get("reasoning") or {}), "effort": selected.effort}}
            limit = payload.get("max_output_tokens")
            if selected.extra_output_tokens and type(limit) is int:
                payload["max_output_tokens"] = min(limit + selected.extra_output_tokens, MAX_OUTPUT_TOKENS)
        return function(payload, *args, **kwargs)
    return moded
