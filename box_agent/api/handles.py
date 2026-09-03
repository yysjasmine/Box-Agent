"""Host-facing service and run-handle protocols."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Protocol

from .contracts import (
    RunRequest,
    RunResult,
    RunStatus,
    SessionInfo,
    SessionOpenRequest,
)
from .controls import CommandAck, ControlCommand
from .events import AgentEvent


class AgentRunHandle(Protocol):
    """Control and observation surface for one Run."""

    run_id: str

    def events(self, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        ...

    async def send(self, command: ControlCommand) -> CommandAck:
        ...

    async def cancel(self, reason: str | None = None) -> CommandAck:
        ...

    async def status(self) -> RunStatus:
        ...

    async def wait(self) -> RunResult:
        ...


class AgentLoop(Protocol):
    """One deterministic execution of a ``RunRequest``.

    The loop owns orchestration and invariants (ordered events, cancellation,
    terminal result).  Context, tools, permissions, memory, LLM, hooks, and
    workflow policy are injected into the implementation through ports; they
    are never selected by ACP/CLI adapters.
    """

    async def run(
        self,
        request: RunRequest,
        *,
        emit: Callable[[AgentEvent], Awaitable[None]],
        cancel_event: Any,
        controls: AsyncIterator[ControlCommand] | None = None,
    ) -> RunResult:
        """
        Execute until a terminal result and emit each event exactly once.

        Implementations must honor cancellation at safe boundaries and make
        externally visible effects idempotent using the injected persistence
        ports.
        """
        ...


class AgentService(Protocol):
    """Stable entry point used by adapters and third-party applications."""

    async def open_session(
        self,
        request: SessionOpenRequest,
    ) -> SessionInfo:
        ...

    async def load_session(
        self,
        request: SessionOpenRequest,
    ) -> SessionInfo:
        """Load an existing durable Session; reject unknown identifiers."""
        ...

    async def update_session_metadata(
        self,
        session_id: str,
        metadata: Mapping[str, Any],
    ) -> SessionInfo:
        """Persist metadata at a between-runs control boundary."""
        ...

    async def start(self, request: RunRequest) -> AgentRunHandle:
        ...

    async def resume(self, run_id: str) -> AgentRunHandle:
        ...

    async def attach(self, run_id: str) -> AgentRunHandle:
        """Attach as a read-only observer without taking execution ownership."""
        ...

    async def get_status(
        self,
        session_id: str,
        run_id: str | None = None,
    ) -> RunStatus:
        ...

    async def close_session(self, session_id: str) -> None:
        ...
