"""Per-invocation identity exposed to stateful Tool plugins.

Tool schemas stay provider-neutral and do not need to expose host/session
identifiers as user-controlled arguments.  The Tool Engine binds this small
context for the duration of one invocation; plugins such as session-scoped
Goal and Plan stores can therefore isolate concurrent runs without mutable
global "current session" state.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeInvocation:
    """Identity and opaque host metadata for the active tool call."""

    session_id: str = ""
    run_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata))


_CURRENT_INVOCATION: ContextVar[RuntimeInvocation] = ContextVar(
    "box_agent_runtime_invocation",
    default=RuntimeInvocation(),
)


def current_runtime_invocation() -> RuntimeInvocation:
    """Return the identity bound to the current Tool Engine task."""

    return _CURRENT_INVOCATION.get()


@contextmanager
def scoped_runtime_invocation(
    *,
    session_id: str = "",
    run_id: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> Iterator[RuntimeInvocation]:
    """Bind invocation identity and restore the previous value on exit."""

    value = RuntimeInvocation(
        session_id=str(session_id or ""),
        run_id=str(run_id or ""),
        metadata=dict(metadata or {}),
    )
    token = _CURRENT_INVOCATION.set(value)
    try:
        yield value
    finally:
        _CURRENT_INVOCATION.reset(token)


__all__ = [
    "RuntimeInvocation",
    "current_runtime_invocation",
    "scoped_runtime_invocation",
]
