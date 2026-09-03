"""Host-backed Skill marketplace search over the internal SkillHub protocol."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .base import Tool, ToolResult

SKILLHUB_RECOMMENDATIONS_TYPE = "skillhub_recommendations"
SKILLHUB_SEARCH_METHOD = "session/skillhub_search"
SKILLHUB_SEARCH_CAPABILITY_VERSION = 1
SEARCH_REQUEST_KINDS = ("capability_gap", "explicit_marketplace_request")
GAP_TYPES = (
    "missing_connector",
    "missing_tool_or_runtime",
    "missing_artifact_format",
    "missing_specialized_workflow",
    "missing_domain_knowledge",
)

_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{8,}\d)(?!\d)")
_ABSOLUTE_PATH_RE = re.compile(r"(?:^|\s)(?:/[^\s]+|[A-Za-z]:[\\/][^\s]+|~[/\\][^\s]+)")
_URL_RE = re.compile(r"https?://|file://", re.IGNORECASE)

CapabilitySnapshotProvider = Callable[[], Mapping[str, Any]]
SkillHubSearcher = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

HARD_CAPABILITY_GAP_PROMPT = """## Skill marketplace capability-gap fallback
When an executable outcome cannot be delivered with installed Skills or tools, use
`tool_search` first when deferred MCP discovery is available. If a hard capability
gap remains, call `search_skillhub` once. Missing input, ambiguity, authentication,
permission, unsupported platforms, and transient failures are not capability gaps.

An explicit request to find or install a Skill from the product marketplace may call
`search_skillhub` directly. A repository URL or general web discovery request is not
an explicit marketplace request. Search is read-only. Send only 2-5 short independent
capability keywords; never send conversation text, file contents, paths, credentials,
contact details, or personal data. Install only an exact returned `skill_id` through
`install_skillhub_skill`, which owns user confirmation. Never bypass denial or failure
with a package manager, shell command, browser automation, or another installer.
"""


def _bounded_text(value: object, *, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _query_is_safe(query: str) -> bool:
    return not (
        "\n" in query
        or "\r" in query
        or _EMAIL_RE.search(query)
        or _PHONE_RE.search(query)
        or _ABSOLUTE_PATH_RE.search(query)
        or _URL_RE.search(query)
    )


def _normalize_candidate(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    skill_id = _bounded_text(value.get("id"), limit=80)
    slug = _bounded_text(value.get("slug"), limit=80)
    name = _bounded_text(value.get("name"), limit=160)
    if not skill_id or not slug or not name:
        return None
    platforms = value.get("platforms")
    risk_labels = value.get("riskLabels")
    download_count = value.get("downloadCount")
    return {
        "id": skill_id,
        "slug": slug,
        "name": name,
        "description": _bounded_text(value.get("description"), limit=500),
        "publisherDisplayName": _bounded_text(
            value.get("publisherDisplayName"), limit=160
        ),
        "currentVersion": _bounded_text(value.get("currentVersion"), limit=40),
        "platforms": [
            item[:40]
            for item in platforms[:8]
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(platforms, list)
        else [],
        "riskLabels": [
            item[:80]
            for item in risk_labels[:8]
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(risk_labels, list)
        else [],
        "downloadCount": (
            download_count
            if isinstance(download_count, int)
            and not isinstance(download_count, bool)
            and download_count >= 0
            else 0
        ),
    }


class SkillHubSearchTool(Tool):
    """Search the Skill marketplace for a hard gap or explicit request."""

    parallel_safe = False
    max_result_size_chars = 8_000

    def __init__(
        self,
        searcher: SkillHubSearcher,
        *,
        snapshot_provider: CapabilitySnapshotProvider | None = None,
        timeout_seconds: float = 8.0,
        installation_available: bool = False,
    ) -> None:
        self._searcher = searcher
        self._snapshot_provider = snapshot_provider or (lambda: {})
        self._timeout_seconds = timeout_seconds
        self._installation_available = installation_available
        self._searched_this_turn = False
        self._candidates_by_id: dict[str, dict[str, Any]] = {}

    def set_snapshot_provider(self, provider: CapabilitySnapshotProvider) -> None:
        self._snapshot_provider = provider

    def reset_turn(self) -> None:
        self._searched_this_turn = False

    def end_run(self) -> None:
        """Reset the one-search budget at the canonical Run boundary."""
        self.reset_turn()

    def candidate(self, skill_id: str) -> dict[str, Any] | None:
        candidate = self._candidates_by_id.get(skill_id.strip())
        return dict(candidate) if candidate is not None else None

    def candidates(self) -> list[dict[str, Any]]:
        return [dict(candidate) for candidate in self._candidates_by_id.values()]

    @property
    def name(self) -> str:
        return "search_skillhub"

    @property
    def description(self) -> str:
        return (
            "Search the Skill marketplace once after a hard capability gap or an "
            "explicit marketplace request. Capability-gap searches require deferred "
            "MCP discovery first when available. Search is read-only and accepts no "
            "user text, files, paths, URLs, credentials, contacts, or personal data."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "requested_outcome": {"type": "string", "minLength": 2, "maxLength": 300},
                "request_kind": {"type": "string", "enum": list(SEARCH_REQUEST_KINDS)},
                "missing_capability": {"type": "string", "minLength": 2, "maxLength": 300},
                "gap_type": {"type": "string", "enum": list(GAP_TYPES)},
                "fallback_assessment": {"type": "string", "minLength": 2, "maxLength": 500},
                "queries": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 5,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 2, "maxLength": 60},
                },
            },
            "required": [
                "requested_outcome",
                "request_kind",
                "missing_capability",
                "gap_type",
                "fallback_assessment",
                "queries",
            ],
            "additionalProperties": False,
        }

    async def execute(
        self,
        requested_outcome: str,
        request_kind: str,
        missing_capability: str,
        gap_type: str,
        fallback_assessment: str,
        queries: list[str],
    ) -> ToolResult:
        outcome = requested_outcome.strip()
        missing = missing_capability.strip()
        fallback = fallback_assessment.strip()
        normalized_queries: list[str] = []
        for query in queries if isinstance(queries, list) else []:
            if isinstance(query, str) and query.strip() and query.strip() not in normalized_queries:
                normalized_queries.append(query.strip())

        if request_kind not in SEARCH_REQUEST_KINDS:
            return self._guard_error("INVALID_SEARCH_REQUEST", "Unsupported request kind.")
        if gap_type not in GAP_TYPES or not all((outcome, missing, fallback)):
            return self._guard_error("NOT_CAPABILITY_GAP", "Complete gap evidence is required.")
        if not 2 <= len(normalized_queries) <= 5 or any(
            not 2 <= len(query) <= 60 or not _query_is_safe(query)
            for query in normalized_queries
        ):
            return self._guard_error(
                "UNSAFE_MARKET_QUERY",
                "Provide 2-5 short, independent, non-sensitive capability keywords.",
            )
        if self._searched_this_turn:
            return self._guard_error(
                "SEARCH_ALREADY_PERFORMED",
                "Do not search the Skill marketplace again this turn.",
            )
        snapshot = dict(self._snapshot_provider() or {})
        if (
            request_kind == "capability_gap"
            and snapshot.get("tool_search_available")
            and not snapshot.get("tool_search_used")
        ):
            return self._guard_error(
                "LOCAL_DISCOVERY_REQUIRED",
                "Use tool_search once before the Skill marketplace.",
            )

        self._searched_this_turn = True

        async def search_queries() -> tuple[list[dict[str, Any]], list[str], bool]:
            candidates: list[dict[str, Any]] = []
            candidate_ids: set[str] = set()
            searched: list[str] = []
            unavailable = False
            for query in normalized_queries:
                searched.append(query)
                try:
                    response = await self._searcher(
                        {"query": query, "gapType": gap_type, "limit": 3}
                    )
                except Exception:
                    unavailable = True
                    continue
                if not isinstance(response, dict) or response.get("status") == "unavailable":
                    unavailable = True
                    continue
                items = response.get("items")
                if response.get("status") != "found" or not isinstance(items, list):
                    continue
                for item in items:
                    candidate = _normalize_candidate(item)
                    if candidate is None or candidate["id"] in candidate_ids:
                        continue
                    candidate_ids.add(candidate["id"])
                    candidates.append(candidate)
                    if len(candidates) == 3:
                        return candidates, searched, unavailable
            return candidates, searched, unavailable

        try:
            candidates, searched_queries, unavailable = await asyncio.wait_for(
                search_queries(), timeout=self._timeout_seconds
            )
        except Exception:
            candidates, searched_queries, unavailable = [], [], True

        if candidates:
            self._candidates_by_id = {
                candidate["id"]: dict(candidate) for candidate in candidates
            }
            names = ", ".join(candidate["name"] for candidate in candidates)
            guidance = (
                "Immediately call install_skillhub_skill with the selected candidate's "
                "exact skill_id. Do not ask for confirmation in prose and do not present "
                "prose-only letter choices; that tool owns confirmation."
                if self._installation_available
                else "Installation is unavailable; direct the user to the marketplace card."
            )
            content = (
                f"The Skill marketplace found {len(candidates)} candidate Skill(s): "
                f"{names}. They are not installed. {guidance}"
            )
            candidate_context = "\n".join(
                f"- skill_id={item['id']!r}; slug={item['slug']!r}; name={item['name']!r}"
                for item in candidates
            )
            model_context = (
                f"{content}\n\nExact Skill marketplace candidates returned for this session:\n"
                f"{candidate_context}\nUse the exact skill_id; never guess an identifier."
            )
            status = "found"
        elif not unavailable:
            self._candidates_by_id = {}
            query_summary = ", ".join(repr(query) for query in searched_queries)
            content = (
                f"The Skill marketplace was searched for {query_summary} and returned no "
                "matching Skill. This result is scoped only to the Skill marketplace. "
                "Do not end the turn solely because of this result; continue broader "
                "discovery or the safest bounded fallback."
            )
            model_context, status, candidates = content, "empty", []
        else:
            self._candidates_by_id = {}
            content = (
                "The Skill marketplace could not be searched right now. Do not say that "
                "no matching Skill exists; continue with the safest bounded fallback."
            )
            model_context, status, candidates = content, "unavailable", []

        return ToolResult(
            success=True,
            content=content,
            model_context=model_context,
            raw_output={
                "type": SKILLHUB_RECOMMENDATIONS_TYPE,
                "status": status,
                "requestKind": request_kind,
                "query": searched_queries[0] if searched_queries else normalized_queries[0],
                "queries": normalized_queries,
                "searchedQueries": searched_queries,
                "gapType": gap_type,
                "missingCapability": missing[:300],
                "items": candidates,
            },
        )

    @staticmethod
    def _guard_error(code: str, message: str) -> ToolResult:
        return ToolResult(
            success=False,
            error=f"{code}: {message}",
            raw_output={
                "type": SKILLHUB_RECOMMENDATIONS_TYPE,
                "status": "rejected",
                "code": code,
            },
        )


def capability_snapshot(agent: Any, skill_loader: Any | None) -> dict[str, Any]:
    """Return objective session evidence used by the pre-search guard."""
    tool_names = sorted(str(name) for name in getattr(agent, "tools", {}))
    tool_search_used = False
    for message in reversed(getattr(agent, "messages", [])):
        if getattr(message, "role", "") == "user":
            break
        for tool_call in getattr(message, "tool_calls", None) or ():
            function = getattr(tool_call, "function", None)
            if getattr(function, "name", "") == "tool_search":
                tool_search_used = True
                break
        if tool_search_used:
            break
    try:
        skill_names = sorted(skill_loader.list_skills()) if skill_loader is not None else []
    except Exception:
        skill_names = []
    return {
        "tool_names": tool_names,
        "skill_names": skill_names,
        "tool_search_available": "tool_search" in tool_names,
        "tool_search_used": tool_search_used,
    }


__all__ = [
    "HARD_CAPABILITY_GAP_PROMPT",
    "SKILLHUB_SEARCH_CAPABILITY_VERSION",
    "SKILLHUB_SEARCH_METHOD",
    "SkillHubSearchTool",
    "capability_snapshot",
]
