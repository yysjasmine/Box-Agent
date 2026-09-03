"""Public plugin lifecycle and registration contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


PluginScope = Literal["global", "session", "run"]


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Identity, dependency, and capability metadata for a plugin."""

    id: str
    version: str
    provides: tuple[str, ...] = ()
    requires: Mapping[str, str] = field(default_factory=dict)
    state_schema: str = "1"

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("plugin id must be a non-empty string")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("plugin version must be a non-empty string")
        if not isinstance(self.state_schema, str) or not self.state_schema.strip():
            raise ValueError("plugin state_schema must be a non-empty string")
        object.__setattr__(self, "provides", tuple(self.provides))
        object.__setattr__(self, "requires", dict(self.requires))


@dataclass(slots=True)
class Registration:
    """A scoped registry entry with disposal and replay-fence metadata."""

    registration_id: str
    key: str
    source: str
    scope: PluginScope
    _dispose_callback: Callable[[], Awaitable[None]] = field(repr=False)
    version: str = "unversioned"
    state_schema: str = "1"
    _disposed: bool = field(default=False, init=False, repr=False)

    async def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        await self._dispose_callback()


@dataclass(frozen=True, slots=True)
class PluginContext:
    """Host-safe services exposed to a plugin during activation."""

    registries: Mapping[str, Any]
    config: Mapping[str, Any] = field(default_factory=dict)
    services: Mapping[str, Any] = field(default_factory=dict)

    def registry(self, name: str) -> Any:
        try:
            return self.registries[name]
        except KeyError as exc:
            raise KeyError(f"unknown plugin registry: {name}") from exc


class Plugin(Protocol):
    manifest: PluginManifest

    async def activate(self, ctx: PluginContext) -> None:
        ...

    async def deactivate(self) -> None:
        ...

    async def dispose(self) -> None:
        ...
