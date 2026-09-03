"""Host-extension routing independent of ACP, CLI, or SDK implementations."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class HostExtensionContext:
    """Runtime references available to a host-extension plugin."""

    session_id: str = ""
    session_metadata: Mapping[str, Any] = field(default_factory=dict)
    service: Any | None = None
    active_handle: Any | None = None
    connection: Any | None = None


class HostExtensionRouter:
    """Resolve one namespaced extension method from a typed registry."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def handle(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        context: HostExtensionContext,
    ) -> dict[str, Any] | None:
        handler = self._registry.resolve(
            method,
            scope="session" if context.session_id else "global",
        )
        if handler is None:
            return None
        target = getattr(handler, "handle", handler)
        if not callable(target):
            raise TypeError(f"host extension '{method}' is not callable")
        value = target(dict(params), context)
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, Mapping):
            raise TypeError(f"host extension '{method}' must return a mapping")
        return dict(value)


__all__ = ["HostExtensionContext", "HostExtensionRouter"]
