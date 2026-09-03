"""Stable data contracts for Context Engine plugins."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class ContextItem:
    """One model-visible context item with retention metadata."""

    item_id: str
    content: str | tuple[dict[str, Any], ...]
    kind: str = "text"
    priority: int = 0
    pinned: bool = False
    resource_id: str | None = None
    content_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.item_id, "item_id")
        _require_text(self.kind, "kind")
        if isinstance(self.content, str):
            normalized_content = self.content
        elif isinstance(self.content, (list, tuple)) and all(
            isinstance(block, dict) for block in self.content
        ):
            normalized_content = tuple(dict(block) for block in self.content)
        else:
            raise ValueError("content must be a string or serializable content blocks")
        object.__setattr__(self, "content", normalized_content)
        if not isinstance(self.priority, int):
            raise ValueError("priority must be an integer")
        if self.resource_id is not None:
            _require_text(self.resource_id, "resource_id")
        if self.content_version is not None:
            _require_text(self.content_version, "content_version")
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def estimated_tokens(self) -> int:
        """Cheap deterministic estimate used before a provider tokenizer exists."""

        if isinstance(self.content, str):
            # Whitespace-only estimates undercount minified JSON, source code,
            # CJK text, and long generated strings by orders of magnitude.
            # The larger of word-count and four-characters-per-token is still
            # cheap while remaining safe across those common context shapes.
            return max(1, len(self.content.split()), (len(self.content) + 3) // 4)
        text = "".join(str(block.get("text", "")) for block in self.content)
        return max(1, len(text.split()), (len(text) + 3) // 4)


@dataclass(frozen=True, slots=True)
class ContextBuildRequest:
    """Input to a Context Engine assembly/compaction operation."""

    items: tuple[ContextItem, ...]
    token_budget: int
    session_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.token_budget, int) or self.token_budget <= 0:
            raise ValueError("token_budget must be a positive integer")
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class ContextManifest:
    """Replay fence describing the provider and exact assembled content."""

    provider: str
    version: str
    sources: tuple[str, ...]
    content_hash: str
    pinned: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, str)
            or not self.provider.strip()
            or not isinstance(self.version, str)
            or not self.version.strip()
        ):
            raise ValueError("context provider and version are required")
        if not isinstance(self.content_hash, str) or not self.content_hash.strip():
            raise ValueError("context content_hash is required")
        object.__setattr__(self, "sources", tuple(str(value) for value in self.sources))
        object.__setattr__(self, "pinned", tuple(str(value) for value in self.pinned))

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "version": self.version,
            "sources": list(self.sources),
            "content_hash": self.content_hash,
            "pinned": list(self.pinned),
        }


@dataclass(frozen=True, slots=True)
class HostProjection:
    """A replayable, host-visible projection produced by a Context plugin."""

    projection_id: str
    payload: Mapping[str, Any]
    surface: str = "raw_output"
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.projection_id, "projection_id")
        _require_text(self.surface, "surface")
        if self.surface != "raw_output":
            raise ValueError(f"unsupported host projection surface: {self.surface}")
        if not isinstance(self.schema_version, int) or self.schema_version <= 0:
            raise ValueError("schema_version must be a positive integer")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")
        object.__setattr__(self, "payload", dict(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "surface": self.surface,
            "schema_version": self.schema_version,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class ContextBuildResult:
    """Output of context assembly, including deterministic compaction facts."""

    items: tuple[ContextItem, ...]
    estimated_tokens: int
    compacted: bool = False
    removed_item_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    manifest: ContextManifest | None = None
    host_projections: tuple[HostProjection, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "removed_item_ids", tuple(self.removed_item_ids))
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(self, "host_projections", tuple(self.host_projections))
        if not all(
            isinstance(projection, HostProjection)
            for projection in self.host_projections
        ):
            raise ValueError("host_projections must contain HostProjection values")
        if self.manifest is not None and not isinstance(self.manifest, ContextManifest):
            raise ValueError("manifest must be a ContextManifest")


class ContextEngine(Protocol):
    """SPI consumed by the Agent Loop for context assembly and restoration."""

    async def assemble(self, request: ContextBuildRequest) -> ContextBuildResult:
        ...

    async def restore(
        self,
        *,
        resource_id: str,
        content_version: str | None = None,
    ) -> ContextItem | None:
        ...


class ContextProvider(Protocol):
    """Minimal provider used by the optional provider registry."""

    async def provide(self, request: ContextBuildRequest) -> Any:
        ...


class ContextCompactor(Protocol):
    """Optional compactor applied after provider output."""

    async def compact(self, request: ContextBuildRequest) -> ContextBuildResult:
        ...


class ContextEventSink(Protocol):
    """Optional event callback for Context Engine observability."""

    def __call__(self, event_type: str, payload: dict[str, Any]) -> Awaitable[None]:
        ...
