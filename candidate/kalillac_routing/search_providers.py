"""Explicit search policy and Brave LLM Context adaptation.

No credentials are read here; callers supply environment-injected keys.
Brave data is evidence, never an answer. Normalized records retain Kalillac's
five-field source shape. Page age/fetch timestamps are not observation dates.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import urllib.request
from urllib.parse import urlparse

BRAVE_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"
# Same selected byte ceiling as the existing Tavily search transport when
# request budgeting is disabled. Budgeted calls use the operator's ceiling.
SEARCH_MAX_BYTES = 262144


class SearchProviderConfigError(ValueError):
    def __init__(self):
        super().__init__("Invalid search policy: use tavily/none, brave/none, or brave/tavily for KALILLAC_SEARCH_PRIMARY/KALILLAC_SEARCH_FALLBACK.")


@dataclass(frozen=True)
class SearchProviderPolicy:
    primary: str = "tavily"
    fallback: str = "none"

    def __post_init__(self):
        if (self.primary, self.fallback) not in {("tavily", "none"), ("brave", "none"), ("brave", "tavily")}:
            raise SearchProviderConfigError()

    @property
    def label(self):
        if self.fallback == "tavily":
            return "Brave (with bounded Tavily fallback)"
        return self.primary.title()

    def facts(self):
        return {"primary": self.primary.title(),
                "fallback": None if self.fallback == "none" else self.fallback.title(),
                "fallback_max_attempts": 0 if self.fallback == "none" else 1,
                "fallback_after": "missing credential, provider/transport failure, malformed response, or no usable evidence",
                "fallback_for_quality_or_age": False,
                "per_response_evidence_provider_available": False}

    def context(self):
        return (f"Configured search primary: {self.primary.title()}; fallback: "
                + ("none." if self.fallback == "none" else "Tavily, at most one search attempt after an eligible Brave failure; no extraction or retry.")
                + " This is search fallback, not model fallback. Configuration does not prove which search provider supplied a particular answer; do not claim fallback ran without request evidence.")


def load_policy(environ):
    # Explicit empty, misspelled, or unsupported values are errors, not defaults.
    return SearchProviderPolicy(environ.get("KALILLAC_SEARCH_PRIMARY", "tavily"),
                                environ.get("KALILLAC_SEARCH_FALLBACK", "none"))


class SearchResponseInvalid(Exception):
    def __init__(self, reason="malformed"):
        self.reason = reason
        super().__init__("Search provider returned no usable evidence.")


def domains_for_query(domains, query):
    values = list(domains or []) or re.findall(r"(?<![-\w])site:([A-Za-z0-9.-]+)", query, re.IGNORECASE)
    normalized = []
    for value in values:
        host = str(value).strip().lower().rstrip(".")
        if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", host):
            raise ValueError("invalid search domain restriction")
        if host not in normalized:
            normalized.append(host)
    return normalized[:5]


def allowed_url(url, domains):
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return not domains or any(host == d or host.endswith("." + d) or (d.startswith("www.") and host == d[4:]) for d in domains)


def brave_payload(query, domains, max_results):
    q = " ".join(str(query).strip()[:400].split()[:75])
    if not q:
        raise ValueError("search query must not be empty")
    payload = {"q": q, "maximum_number_of_urls": max_results,
               "maximum_number_of_tokens": 2048, "maximum_number_of_tokens_per_url": 512,
               "maximum_number_of_snippets": max_results * 3, "maximum_number_of_snippets_per_url": 3,
               "enable_source_metadata": True}
    if domains:
        # Generic discard with site exceptions is Brave's documented allowlist.
        payload["goggles"] = "$discard\n" + "\n".join("$site=" + d for d in domains)
    return payload


def normalize_brave(response, domains, max_results, content_limit):
    if not isinstance(response, dict) or not isinstance(response.get("grounding"), dict):
        raise SearchResponseInvalid()
    grounding = response["grounding"]
    items = grounding.get("generic", [])
    sources = response.get("sources", {})
    if not isinstance(items, list) or not isinstance(sources, dict):
        raise SearchResponseInvalid()
    items = list(items)
    poi = grounding.get("poi")
    if poi is not None:
        if not isinstance(poi, dict):
            raise SearchResponseInvalid()
        items.append(poi)
    maps = grounding.get("map", [])
    if not isinstance(maps, list):
        raise SearchResponseInvalid()
    items.extend(maps)
    results, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise SearchResponseInvalid()
        url = item.get("url", "")
        snippets = item.get("snippets") or []
        if not isinstance(url, str) or not isinstance(snippets, list) or any(not isinstance(s, str) for s in snippets):
            raise SearchResponseInvalid()
        url = url.strip()
        if not url or not allowed_url(url, domains) or url in seen:
            continue
        content = "\n\n".join(s.strip() for s in snippets if s.strip())
        if not content:
            continue
        metadata = sources.get(url, {})
        if not isinstance(metadata, dict):
            raise SearchResponseInvalid()
        title = item.get("title") or item.get("name") or metadata.get("title") or url
        if not isinstance(title, str):
            raise SearchResponseInvalid()
        notes = []
        age = metadata.get("age")
        if age is not None:
            notes.append("Source page age (publication/modified metadata, not observation time): " + json.dumps(age, ensure_ascii=False)[:256])
        fetched = metadata.get("fetched_content_timestamp")
        if fetched is not None:
            notes.append("Source content fetch timestamp (not observation time): " + str(fetched)[:128])
        # Metadata and extracted text share the existing per-source character cap.
        if notes:
            content = "\n".join(notes) + "\n\n" + content
        results.append({"title": title.strip() or url, "url": url, "content": content[:content_limit],
                        "published": "", "score": None})
        seen.add(url)
        if len(results) >= max_results:
            break
    if not results:
        raise SearchResponseInvalid("no_evidence")
    return results


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_json_unbudgeted(url, payload, headers, timeout, max_bytes=SEARCH_MAX_BYTES):
    """One POST, no redirects/retries, capped read and existing call timeout.

    No request-wide allowance/deadline exists when the budget flag is off.
    DNS retains urllib's OS behavior, as in the existing unbudgeted SDK path.
    """
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "Accept": "application/json", **headers}, method="POST")
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise SearchResponseInvalid()
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise SearchResponseInvalid()
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise SearchResponseInvalid() from None
