"""Composition helpers for independently registered memory capabilities."""

from __future__ import annotations

import inspect
from typing import Any

from .api import MemoryEntry, MemoryQuery, MemoryRecall


class CompositeMemoryEngine:
    """Join provider/store/writer plugins behind one MemoryEngine port.

    A host may register a complete engine as a single capability, or split the
    responsibilities across the canonical typed registries.  This adapter
    keeps the Kernel unaware of that choice and fails closed when recall is not
    available; writes remain a no-op only when no writer was registered.
    """

    def __init__(
        self,
        *,
        provider: Any | None = None,
        store: Any | None = None,
        writer: Any | None = None,
    ) -> None:
        self._provider = provider
        self._store = store
        self._writer = writer

    async def recall(self, query: MemoryQuery) -> MemoryRecall:
        target = self._provider or self._store
        method = getattr(target, "recall", None) or getattr(target, "query", None)
        if not callable(method):
            raise TypeError("memory provider/store must provide recall(query) or query(query)")
        value = method(query)
        return await value if inspect.isawaitable(value) else value

    async def write(self, entry: MemoryEntry) -> MemoryEntry:
        target = self._writer or self._store or self._provider
        method = getattr(target, "write", None)
        if not callable(method):
            return entry
        value = method(entry)
        return await value if inspect.isawaitable(value) else value

    async def flush(self) -> None:
        seen: set[int] = set()
        for target in (self._writer, self._store, self._provider):
            if target is None or id(target) in seen:
                continue
            seen.add(id(target))
            method = getattr(target, "flush", None)
            if not callable(method):
                continue
            value = method()
            if inspect.isawaitable(value):
                await value


__all__ = ["CompositeMemoryEngine"]
