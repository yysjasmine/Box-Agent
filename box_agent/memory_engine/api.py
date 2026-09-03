"""Stable data contracts for Memory Engine plugins."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol


class MemoryConflictError(RuntimeError):
    """Raised when an entry identity is reused with different content."""


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """One durable memory item independent of a concrete backend."""

    entry_id: str
    text: str
    kind: str = "fact"
    score: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, str) or not self.entry_id.strip():
            raise ValueError("entry_id must be a non-empty string")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("kind must be a non-empty string")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def with_score(self, score: float) -> "MemoryEntry":
        return replace(self, score=float(score))

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "text": self.text,
            "kind": self.kind,
            "score": self.score,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """Data-only recall request."""

    text: str
    limit: int = 10
    kind: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("query text must be a non-empty string")
        if not isinstance(self.limit, int) or self.limit <= 0:
            raise ValueError("limit must be a positive integer")
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class MemoryRecall:
    """Recall result with ordered, scored entries."""

    entries: tuple[MemoryEntry, ...]
    query: MemoryQuery

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))


class MemoryEngine(Protocol):
    """SPI for recall, durable writes, and lifecycle flush."""

    async def recall(self, query: MemoryQuery) -> MemoryRecall:
        ...

    async def write(self, entry: MemoryEntry) -> MemoryEntry:
        ...

    async def flush(self) -> None:
        ...
