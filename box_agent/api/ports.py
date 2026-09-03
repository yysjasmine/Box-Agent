"""Capability ports consumed by the Agent Loop."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from .contracts import (
    LLMRequest,
    ModelChunk,
    SessionInfo,
    ToolCallRequest,
    ToolCallResult,
    WorkflowContinuation,
)
from .controls import CommandAck, ControlCommand
from .permissions import PermissionDecision, PermissionRequest
from .workflows import WorkflowAction, WorkflowCheckpointUpdate

if TYPE_CHECKING:
    from box_agent.memory_engine.api import MemoryEntry, MemoryQuery, MemoryRecall


class ToolExecutor(Protocol):
    async def execute(
        self, call: ToolCallRequest, context: Any | None = None
    ) -> ToolCallResult:
        ...


class ToolEngine(ToolExecutor, Protocol):
    """Optional orchestration port for durable Tool boundaries.

    ``prepare_call`` is invoked before ``tool.call.requested`` so validation,
    permission preflight, Hook rewriting, and an effect fence can complete
    without entering an executor. Implementations that do not need a two-phase
    fence may expose only :class:`ToolExecutor` and use the compatibility path.
    """

    def schemas(self) -> Sequence[Mapping[str, Any]]:
        ...

    async def prepare_call(
        self,
        call: ToolCallRequest,
        *,
        context: Any | None = None,
    ) -> tuple[ToolCallRequest, ToolCallResult | None]:
        ...

    async def end_run(
        self,
        *,
        session_id: str,
        run_id: str,
        metadata: Mapping[str, Any],
    ) -> None:
        """Release run-scoped resources after every terminal path."""
        ...

    def restricted_passthrough_tool_names(self) -> Sequence[str]:
        """Return dynamically activated tools that survive workflow filtering."""
        ...


class ToolPlugin(Protocol):
    """Optional per-tool contract used by the registry execution boundary.

    ``validate`` and ``preflight`` are deliberately separate from ``invoke``:
    the engine must reject malformed arguments, then resolve permission, before
    it enters the executor.  Legacy plugins may omit either optional method;
    the engine keeps the historical path for them.
    """

    name: str
    parameters: Mapping[str, Any]

    def validate(self, arguments: Mapping[str, Any]) -> Any | None:
        ...

    async def preflight(
        self,
        arguments: Mapping[str, Any],
        *,
        context: Any | None = None,
    ) -> Any | None:
        ...

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        *,
        context: Any | None = None,
    ) -> Any:
        ...

    def end_run(self) -> Any:
        """Optional cleanup for resources that cannot cross Run boundaries."""
        ...


class SessionToolContributor(Protocol):
    """Create Tool plugins bound to one durable Agent Session."""

    def provide_tools(self, request: Any) -> Sequence[ToolPlugin]:
        ...


class SessionMetadataContributor(Protocol):
    """Supply durable Session defaults without owning host protocol parsing."""

    def contribute(
        self,
        request: Any,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        ...


class SessionLifecycle(Protocol):
    """Release resources owned by one logical Session."""

    def on_session_close(self, session_id: str) -> Any | Awaitable[Any]:
        ...


class PermissionPolicy(Protocol):
    async def decide(self, request: PermissionRequest) -> PermissionDecision:
        ...


class MemoryProvider(Protocol):
    async def recall(self, request: MemoryQuery) -> MemoryRecall:
        ...


class MemoryWriter(Protocol):
    async def write(self, observation: MemoryEntry) -> MemoryEntry:
        ...


class MemoryStore(Protocol):
    async def query(self, request: MemoryQuery) -> MemoryRecall:
        ...


class SessionStore(Protocol):
    """Durable logical-session identity used by the Agent Service."""

    async def get(self, session_id: str) -> SessionInfo | None:
        ...

    async def put(self, session: SessionInfo) -> None:
        ...

    async def update(self, session: SessionInfo) -> None:
        """Replace mutable metadata while preserving Session identity."""
        ...

    async def close(self, session_id: str) -> None:
        ...


class LLMPort(Protocol):
    async def stream(self, request: LLMRequest) -> AsyncIterator[ModelChunk]:
        ...


class Hook(Protocol):
    """Cross-cutting observer/interceptor for the native runtime.

    ``on_event`` is observational.  ``before_tool`` may return a replacement
    ``ToolCallRequest``/arguments mapping or a terminal ``ToolCallResult``;
    ``after_tool`` may return a replacement result.  Both methods are optional
    at runtime so existing event-only hooks remain source compatible.
    """

    async def on_event(self, event: Any) -> Any:
        ...

    async def before_tool(self, call: ToolCallRequest, context: Any | None = None) -> Any:
        ...

    async def after_tool(
        self,
        call: ToolCallRequest,
        result: ToolCallResult,
        context: Any | None = None,
    ) -> Any:
        ...


class WorkflowPolicy(Protocol):
    """Stable workflow SPI exposed from the protocol package.

    Concrete workflow implementations live under ``box_agent.workflows`` (or
    in a third-party plugin).  Every method after ``decide`` is optional at
    runtime; the protocol documents the complete seam without importing any
    implementation into ``box_agent.api``.
    """

    kind: str
    checkpoint_injection_id: str
    evidence_read_batch_size: int

    # Optional workflow default; an explicit RunOptions.max_tool_calls wins.
    max_tool_calls: int | None

    def decide(self, context: Any) -> Any:
        ...

    def context_items(self, context: Any) -> Sequence[Any]:
        ...

    def initial_events(self, context: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        """Optional namespaced facts emitted immediately after ``run.started``."""
        ...

    def build_checkpoint(self) -> str | None:
        ...

    def build_checkpoint_payload(self) -> Mapping[str, Any]:
        """Return structured workflow state for boundary checkpoints."""
        ...

    def update_checkpoint(
        self, checkpoint_text: str
    ) -> WorkflowCheckpointUpdate:
        ...

    def next_deterministic_action(self) -> WorkflowAction | None:
        ...

    def plan_scope_error(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        ...

    def tool_call_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        verified_evidence_urls: set[str],
        parallel: bool = False,
    ) -> str | None:
        ...

    def record_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        *,
        executed: bool = True,
    ) -> None:
        ...

    def result_output(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> Mapping[str, Any] | None:
        """Optionally decorate normalized tool output before publication."""
        ...

    def pause_after_tool(
        self,
        tool_name: str,
        result: Any,
    ) -> str | None:
        """Optionally stop the run after a successful workflow boundary."""
        ...

    def terminal_metadata(
        self,
        stop_reason: str,
        final_content: str,
    ) -> Mapping[str, Any]:
        """Return workflow-owned facts for the terminal RunResult/event."""
        ...

    def terminal_message(
        self,
        stop_reason: str,
        final_content: str,
    ) -> str | None:
        """Optionally replace content at a recoverable workflow boundary."""
        ...

    def next_continuation(
        self,
        *,
        stop_reason: str,
        final_content: str,
        step: int,
    ) -> WorkflowContinuation | None:
        """Return one durable follow-up turn, or ``None`` to finish the Run.

        This is deliberately a workflow decision, not a transport concern.
        The Kernel persists the returned message as an event before asking the
        LLM for another step. Implementations may omit the optional method;
        omitted means no continuation.
        """
        ...

    def record_model_response(
        self,
        *,
        content: str,
        finish_reason: str,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        """Observe the normalized model boundary before terminal decisions."""
        ...

    def handle_control(self, command: ControlCommand) -> Any:
        """Apply an accepted host control at the next safe loop boundary."""
        ...

    # Optional schema/visibility hooks.  They remain outside the required
    # lifecycle so older policies can be activated unchanged.
    def hidden_tool_names(self) -> Sequence[str]:
        """Return model-facing tool names hidden for the current workflow stage."""
        ...

    def filter_tool_schemas(
        self, schemas: Sequence[Mapping[str, Any]]
    ) -> Sequence[Mapping[str, Any]]:
        """Optionally filter or annotate the model-facing tool catalog."""
        ...

    def required_tool_names(self) -> Sequence[str]:
        """Return tools that must remain visible while a gate is pending."""
        ...

    def begin_tool_decision(self, decision_id: int | str) -> None: ...

    def record_visible_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        visible_content: str,
    ) -> None: ...

    def evidence_urls(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> Sequence[str]: ...

    def exempts_tool_budget(self, tool_name: str) -> bool: ...

    def uses_evidence_read_budget(self, tool_name: str) -> bool: ...

    def is_direct_evidence_read_tool(self, tool_name: str) -> bool: ...

    def direct_evidence_url(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> str | None: ...

    def allows_completion_continuation(self) -> bool: ...

    def suppresses_generic_final_summary(self) -> bool: ...


ControlHandler = Callable[
    [ControlCommand],
    CommandAck
    | bool
    | Mapping[str, Any]
    | Awaitable[CommandAck | bool | Mapping[str, Any] | None]
    | None,
]
