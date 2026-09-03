"""Reference Memory Engine for tests and local plugin development."""

from __future__ import annotations

import inspect

from .api import MemoryConflictError, MemoryEntry, MemoryQuery, MemoryRecall


class MemoryCapabilityAdapter:
    """Fill optional ``write``/``flush`` methods for recall-only plugins."""

    def __init__(self, capability) -> None:
        self._capability = capability

    async def recall(self, query: MemoryQuery) -> MemoryRecall:
        method = getattr(self._capability, "recall", None) or getattr(
            self._capability, "query", None
        )
        if not callable(method):
            raise TypeError("memory plugin must provide recall(query) or query(query)")
        value = method(query)
        return await value if inspect.isawaitable(value) else value

    async def write(self, entry: MemoryEntry) -> MemoryEntry:
        method = getattr(self._capability, "write", None)
        if not callable(method):
            return entry
        value = method(entry)
        return await value if inspect.isawaitable(value) else value

    async def flush(self) -> None:
        method = getattr(self._capability, "flush", None)
        if callable(method):
            value = method()
            if inspect.isawaitable(value):
                await value


class InMemoryMemoryEngine:
    """Deterministic lexical recall with idempotent writes."""

    def __init__(self) -> None:
        self._entries: dict[str, MemoryEntry] = {}

    async def write(self, entry: MemoryEntry) -> MemoryEntry:
        existing = self._entries.get(entry.entry_id)
        if existing is not None:
            if (
                existing.text != entry.text
                or existing.kind != entry.kind
                or dict(existing.metadata) != dict(entry.metadata)
            ):
                raise MemoryConflictError(
                    f"entry_id {entry.entry_id!r} already contains different data"
                )
            return existing
        stored = entry.with_score(0.0)
        self._entries[entry.entry_id] = stored
        return stored

    async def recall(self, query: MemoryQuery) -> MemoryRecall:
        query_terms = set(query.text.lower().split())
        candidates: list[tuple[float, str, MemoryEntry]] = []
        for entry in self._entries.values():
            if query.kind is not None and entry.kind != query.kind:
                continue
            if any(entry.metadata.get(key) != value for key, value in query.metadata.items()):
                continue
            entry_terms = set(entry.text.lower().split())
            overlap = query_terms & entry_terms
            if not overlap:
                continue
            score = len(overlap) / max(1, len(query_terms))
            candidates.append((score, entry.entry_id, entry.with_score(score)))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return MemoryRecall(
            entries=tuple(item[2] for item in candidates[: query.limit]),
            query=query,
        )

    async def flush(self) -> None:
        return None
