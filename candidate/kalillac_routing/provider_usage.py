"""Opt-in, content-free evaluation only. No environment, I/O, threads or sinks.

A trusted evaluator wraps exactly one request in observation_scope. Context
copies retain that request's observer; closing seals it, so late worker/HTTP
completion cannot write into the next request. Export is pulled afterwards.
Admission and HTTP-client dispatch are not proof of a provider charge.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import threading

from .request_budget import current_budget

MAX_REQUESTS = 64
MAX_ATTEMPTS = 32
MAX_TOKENS = 2_000_000
MODEL_IDS = frozenset({"gpt-5.6-luna"})
TIERS = frozenset({"auto", "default", "standard", "flex", "priority", "fast", "scale"})
STATUSES = frozenset({"completed", "incomplete", "failed", "cancelled", "queued", "in_progress"})
_REQUEST = ContextVar("evaluation_request_usage", default=None)
_ATTEMPT = ContextVar("evaluation_provider_attempt", default=None)


def _category(value, allowed):
    # Exact builtin types: do not invoke arbitrary provider objects/methods.
    return value if type(value) is str and len(value) <= 48 and value in allowed else "unknown"


def _counter(data, key):
    value = data.get(key) if type(data) is dict else None
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_TOKENS:
        raise ValueError("Invalid numeric usage.")
    return value


def _numbers(data):
    empty = dict.fromkeys(("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens"))
    usage = data.get("usage") if type(data) is dict else None
    if usage is None:
        return {**empty, "usage_status": "missing"}
    if type(usage) is not dict:
        return {**empty, "usage_status": "invalid"}
    try:
        values = {"input_tokens": _counter(usage, "input_tokens"),
                  "output_tokens": _counter(usage, "output_tokens"),
                  "cached_tokens": _counter(usage.get("input_tokens_details"), "cached_tokens"),
                  "cache_write_tokens": _counter(usage.get("input_tokens_details"), "cache_write_tokens"),
                  "reasoning_tokens": _counter(usage.get("output_tokens_details"), "reasoning_tokens")}
        i, o = values["input_tokens"], values["output_tokens"]
        c, w, r = values["cached_tokens"], values["cache_write_tokens"], values["reasoning_tokens"]
        if i is not None and ((c is not None and c > i) or (w is not None and w > i) or
                              (c is not None and w is not None and c + w > i)):
            raise ValueError("Invalid input partition.")
        if o is not None and r is not None and r > o:
            raise ValueError("Invalid output partition.")
        total = _counter(usage, "total_tokens")
        if total is not None and i is not None and o is not None and total != i + o:
            raise ValueError("Invalid total.")
    except ValueError:
        return {**empty, "usage_status": "invalid"}
    return {**values, "usage_status": "complete" if all(v is not None for v in values.values()) else "partial"}


def _export_attempt(a):
    # Validate again at export; never copy extra keys or arbitrary metadata.
    result = {}
    categories = {"provider": {"openai", "brave", "tavily"}, "operation": {"responses", "search", "extract"},
                  "depth": {"basic", "advanced"}, "configured_model": MODEL_IDS, "returned_model": MODEL_IDS,
                  "service_tier": TIERS, "provider_status": STATUSES,
                  "phase": {"started", "returned", "failed", "rejected", "unobserved"},
                  "usage_status": {"missing", "invalid", "partial", "complete", "not_applicable"},
                  "failure": {"none", "cancelled", "deadline", "allowance", "local_unavailable", "provider_or_unknown", "missing_credential"}}
    for field, allowed in categories.items():
        result[field] = _category(a.get(field), allowed)
    for field in ("admitted", "dispatched"):
        result[field] = a.get(field) if type(a.get(field)) is bool else None
    result["response_returned"] = a.get("response_returned") is True
    for field in ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "credits"):
        try:
            result[field] = _counter(a, field)
        except ValueError:
            result[field] = None
            result["usage_status"] = "invalid"
    return result


class _Request:
    def __init__(self, case_number, max_attempts):
        self.case_number = case_number
        self.max_attempts = max_attempts
        self.attempts = []
        self.closed = False
        self.outcome = "open"
        self.observation_lost = False
        self.unobserved_operations = False
        self._lock = threading.Lock()

    def begin(self, provider, operation, depth="unknown", model="unknown"):
        if self.closed:
            return None
        if not self._lock.acquire(blocking=False):
            self.observation_lost = True
            return None
        try:
            if self.closed:
                return None
            if len(self.attempts) >= self.max_attempts:
                self.observation_lost = True
                return None
            index = len(self.attempts)
            record = {"provider": _category(provider, {"openai", "brave", "tavily"}),
                      "operation": _category(operation, {"responses", "search", "extract"}),
                      "depth": _category(depth, {"basic", "advanced"}),
                      "configured_model": _category(model, MODEL_IDS), "returned_model": "unknown",
                      "service_tier": "unknown", "provider_status": "unknown", "phase": "started",
                      "admitted": None, "dispatched": None, "response_returned": False,
                      "failure": "none", "credits": None, **_numbers(None)}
            if provider != "openai":
                record["usage_status"] = "not_applicable"
            self.attempts.append(record)
            return self, index
        finally:
            self._lock.release()

    def update(self, index, **fields):
        if self.closed:
            return
        if not self._lock.acquire(blocking=False):
            self.observation_lost = True
            return
        try:
            if not self.closed:
                self.attempts[index] = {**self.attempts[index], **fields}
        finally:
            self._lock.release()

    def close(self, outcome):
        # Never wait for an evaluator holding a snapshot lock.
        self.closed = True
        self.outcome = outcome
        if not self._lock.acquire(blocking=False):
            self.observation_lost = True
            return
        try:
            self.attempts = [dict(a, phase="unobserved") if a["phase"] == "started" else a for a in self.attempts]
        finally:
            self._lock.release()

    def snapshot(self):
        base = {"case_number": self.case_number, "outcome": self.outcome,
                "observation_lost": self.observation_lost, "unobserved_operations": self.unobserved_operations}
        if not self._lock.acquire(blocking=False):
            return {**base, "observation_lost": True, "attempts": []}
        try:
            return {**base, "attempts": [_export_attempt(a) for a in self.attempts]}
        finally:
            self._lock.release()


class EvaluationCollector:
    """Fixed-capacity in-memory records. No callbacks, blocking offers or I/O.

    snapshot() is for the evaluator after a response, never an application
    export/logging hook. Saturation/contention flags incomplete observation;
    it cannot refuse/retry a chat or silently imply zero cost.
    """
    def __init__(self, max_requests=32, max_attempts=16):
        for value, limit in ((max_requests, MAX_REQUESTS), (max_attempts, MAX_ATTEMPTS)):
            if type(value) is not int or not 1 <= value <= limit:
                raise ValueError("Invalid evaluation capacity.")
        self.max_requests = max_requests
        self.max_attempts = max_attempts
        self._requests = []
        self.observation_lost = False
        self._lock = threading.Lock()

    def _open(self, case_number):
        if not self._lock.acquire(blocking=False):
            self.observation_lost = True
            return None
        try:
            if len(self._requests) >= self.max_requests:
                self.observation_lost = True
                return None
            request = _Request(case_number, self.max_attempts)
            self._requests.append(request)
            return request
        finally:
            self._lock.release()

    def snapshot(self):
        if not self._lock.acquire(blocking=False):
            return {"observation_lost": True, "requests": []}
        try:
            requests = tuple(self._requests)
        finally:
            self._lock.release()
        return {"observation_lost": self.observation_lost, "requests": [r.snapshot() for r in requests]}


@contextmanager
def observation_scope(collector, *, case_number):
    if type(collector) is not EvaluationCollector:
        raise TypeError("Use the bounded evaluation collector, not a sink.")
    if type(case_number) is not int or not 1 <= case_number <= 20:
        raise ValueError("Use a synthetic case number from 1 to 20.")
    if _REQUEST.get() is not None:
        raise ValueError("Evaluation scopes cannot nest.")
    request = collector._open(case_number)
    token = _REQUEST.set(request)
    outcome = "finished"
    try:
        yield
    except BaseException as error:
        outcome = "cancelled" if type(error).__name__ in {"CancelledError", "RequestCancelled"} else "raised"
        raise
    finally:
        if request is not None:
            request.close(outcome)
        _REQUEST.reset(token)


def _update(**fields):
    attempt = _ATTEMPT.get()
    if attempt is not None:
        try:
            attempt[0].update(attempt[1], **fields)
        except Exception:
            attempt[0].observation_lost = True


def admitted():
    _update(admitted=True)


def dispatched():
    """The HTTP client operation started, not proof of bytes/billing."""
    from . import provider_controls
    provider_controls.dispatch()
    _update(dispatched=True)


def returned(data):
    attempt = _ATTEMPT.get()
    if attempt is None:
        return
    try:
        provider = attempt[0].attempts[attempt[1]]["provider"]
        fields = {"phase": "returned", "response_returned": True, "dispatched": True}
        if provider == "openai":
            fields.update(_numbers(data))
            obj = data if type(data) is dict else {}
            fields.update(returned_model=_category(obj.get("model"), MODEL_IDS),
                          service_tier=_category(obj.get("service_tier"), TIERS),
                          provider_status=_category(obj.get("status"), STATUSES))
        elif type(data) is dict:
            try:
                fields["credits"] = _counter(data.get("usage"), "credits")
            except ValueError:
                pass
        _update(**fields)
    except Exception:
        attempt[0].observation_lost = True


def _failure(error):
    name = type(error).__name__
    if name in {"RequestCancelled", "TransportCancelled", "CancelledError"}:
        return "cancelled"
    if name in {"RequestDeadlineExceeded", "TransportDeadlineExceeded"}:
        return "deadline"
    if name in {"CallBudgetExhausted", "CeilingBusy"}:
        return "allowance"
    if name in {"TransportUnavailable", "ProviderTransportUnavailable", "SearchTransportUnavailable", "OpenAIConfigurationUnavailable", "CeilingUnavailable"}:
        return "local_unavailable"
    return "provider_or_unknown"


def _call(function, args, kwargs, provider, operation, depth="unknown", model="unknown"):
    request = _REQUEST.get()
    if request is None or request.closed:
        return function(*args, **kwargs)
    try:
        attempt = request.begin(provider, operation, depth, model)
    except Exception:
        request.observation_lost = True
        attempt = None
    token = _ATTEMPT.set(attempt)
    budget = current_budget()
    counter = "model_attempts" if provider == "openai" else "search_attempts"
    before = getattr(budget, counter) if budget is not None else None
    if budget is None:
        admitted()
    try:
        value = function(*args, **kwargs)
        returned(value)
        return value
    except BaseException as error:
        failure = _failure(error)
        _update(phase="failed", failure=failure)
        if failure == "local_unavailable":
            # Cleanup can fail AFTER HTTP dispatch/usable returned usage.
            # Never turn that observed operation into an alleged free refusal.
            record = attempt[0].attempts[attempt[1]] if attempt is not None else {}
            if record.get("dispatched") is not True:
                _update(dispatched=False)
        # A real HTTP status proves response dispatch; no error text copied.
        if type(error).__name__ in {"TransportHTTPError", "HTTPError"}:
            _update(dispatched=True)
        raise
    finally:
        if budget is not None:
            observed_admission = getattr(budget, counter) > before
            _update(admitted=observed_admission)
            if not observed_admission:
                _update(dispatched=False, phase="rejected")
        _ATTEMPT.reset(token)


def observe_model(function):
    @wraps(function)
    def observed(payload, *args, **kwargs):
        model = payload.get("model") if type(payload) is dict else None
        return _call(function, (payload, *args), kwargs, "openai", "responses", model=model)
    return observed


def observe_tavily(function):
    @wraps(function)
    def observed(budget, operation, arguments):
        depth_key = "search_depth" if operation == "search" else "extract_depth"
        depth = arguments.get(depth_key) if type(arguments) is dict else None
        return _call(function, (budget, operation, arguments), {}, "tavily", operation, depth=depth)
    return observed


def observe_single_search(function):
    @wraps(function)
    def observed(provider, arguments):
        # Budgeted Tavily has its own shared boundary: never double count it.
        if provider == "tavily" and current_budget() is not None:
            return function(provider, arguments)
        depth = arguments.get("search_depth") if type(arguments) is dict else None
        return _call(function, (provider, arguments), {}, provider, "search", depth=depth)
    return observed


def observe_lookup(function, context):
    """Keep legacy policy intact; note absent keys/unsupported SDK coverage.

    context is a fixed application function, not an evaluator sink. It
    returns only provider/credential-presence/SDK-mode booleans. No keys or
    query text are passed into observation records.
    """
    @wraps(function)
    def observed(*args, **kwargs):
        request = _REQUEST.get()
        if request is not None and not request.closed:
            try:
                provider, key_present, sdk_mode = context()
                if not key_present:
                    attempt = request.begin(provider, "search")
                    if attempt is not None:
                        request.update(attempt[1], phase="rejected", admitted=False, dispatched=False, failure="missing_credential")
                elif sdk_mode:
                    # Legacy unbudgeted Tavily may retry/extract within the SDK.
                    # Do not invent an attempt count from normalized evidence.
                    request.unobserved_operations = True
            except Exception:
                request.observation_lost = True
        return function(*args, **kwargs)
    return observed
