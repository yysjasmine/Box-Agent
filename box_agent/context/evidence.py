"""Shared URL normalization and evidence extraction helpers."""

from __future__ import annotations

import json
import re
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "extract_http_urls",
    "extract_search_result_evidence",
    "normalize_search_url",
]

_URL_TRACKING_PARAMS: Final[set[str]] = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}
_HTTP_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"https?://[^\s<>\"'\]\[{}()]+",
    re.IGNORECASE,
)
_SEARCH_RESULT_LIST_KEYS: Final[tuple[str, ...]] = (
    "refs",
    "results",
    "Results",
    "webResults",
    "WebResults",
    "web_results",
    "items",
    "value",
    "organic_results",
    "data",
)
_SEARCH_RESULT_URL_KEYS: Final[tuple[str, ...]] = (
    "url",
    "Url",
    "href",
    "link",
    "Link",
)
_SEARCH_RESULT_TITLE_KEYS: Final[tuple[str, ...]] = (
    "title",
    "Title",
    "name",
    "Name",
)
_SEARCH_RESULT_SNIPPET_KEYS: Final[tuple[str, ...]] = (
    "snippet",
    "Snippet",
    "summary",
    "Summary",
    "description",
    "Description",
    "content",
    "Content",
)


def normalize_search_url(value: Any) -> str:
    """Normalize a URL for evidence identity and duplicate detection."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.casefold()

    query_items = []
    for key, val in parse_qsl(parts.query, keep_blank_values=True):
        key_lower = key.casefold()
        if key_lower.startswith("utm_") or key_lower in _URL_TRACKING_PARAMS:
            continue
        query_items.append((key, val))
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path.rstrip("/") or parts.path,
            urlencode(query_items, doseq=True),
            "",
        )
    )


def extract_http_urls(value: Any) -> set[str]:
    """Return normalized HTTP(S) URLs recursively contained in ``value``."""
    if isinstance(value, str):
        return {
            normalized
            for match in _HTTP_URL_RE.findall(value)
            if (normalized := normalize_search_url(match.rstrip(".,;:!?，。；：！？")))
        }
    if isinstance(value, dict):
        urls: set[str] = set()
        for item in value.values():
            urls.update(extract_http_urls(item))
        return urls
    if isinstance(value, (list, tuple, set)):
        urls: set[str] = set()
        for item in value:
            urls.update(extract_http_urls(item))
        return urls
    return set()


def _first_string(value: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _search_result_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    if _first_string(value, _SEARCH_RESULT_URL_KEYS) and _first_string(
        value,
        _SEARCH_RESULT_SNIPPET_KEYS,
    ):
        return [value]
    for key in _SEARCH_RESULT_LIST_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, list):
            rows = [item for item in candidate if isinstance(item, dict)]
            if rows:
                return rows
        if isinstance(candidate, dict):
            rows = _search_result_rows(candidate)
            if rows:
                return rows
    for candidate in value.values():
        if isinstance(candidate, dict):
            rows = _search_result_rows(candidate)
            if rows:
                return rows
        if isinstance(candidate, list):
            rows = _search_result_rows(candidate)
            if rows:
                return rows
    return []


def extract_search_result_evidence(value: Any) -> dict[str, str]:
    """Bind each structured search-result URL to its own title and summary.

    Plain text and title-only rows are intentionally ignored: a usable search
    evidence record needs an absolute result URL and provider-returned summary
    text, so one result's excerpt cannot be attributed to a sibling URL.
    """
    payload = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {}

    evidence: dict[str, str] = {}
    for row in _search_result_rows(payload):
        url = normalize_search_url(_first_string(row, _SEARCH_RESULT_URL_KEYS))
        try:
            scheme = urlsplit(url).scheme.casefold()
        except ValueError:
            scheme = ""
        snippet = _first_string(row, _SEARCH_RESULT_SNIPPET_KEYS)
        if scheme not in {"http", "https"} or not snippet:
            continue
        title = _first_string(row, _SEARCH_RESULT_TITLE_KEYS)
        text = "\n".join(part for part in (title, snippet) if part)
        if url in evidence:
            evidence[url] = f"{evidence[url]}\n{text}"
        else:
            evidence[url] = text
    return evidence
