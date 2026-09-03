"""Serializable request, response, and value contracts for Agent hosts."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from .errors import ErrorInfo


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _validate_positive(value: int | None, field_name: str) -> None:
    if value is not None and (not isinstance(value, int) or value <= 0):
        raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class Message:
    """A host-neutral message that can be serialized across adapters."""

    role: str
    content: str | tuple[Mapping[str, Any], ...]
    name: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.role, "role")
        if isinstance(self.content, str):
            normalized_content = self.content
        elif isinstance(self.content, (list, tuple)) and all(
            isinstance(block, Mapping) for block in self.content
        ):
            normalized_content = tuple(dict(block) for block in self.content)
        else:
            raise ValueError("content must be a string or serializable content blocks")
        object.__setattr__(self, "content", normalized_content)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def user(
        cls,
        content: str | list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        **metadata: Any,
    ) -> "Message":
        return cls(role="user", content=content, metadata=metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": (
                list(self.content)
                if isinstance(self.content, tuple)
                else self.content
            ),
            "name": self.name,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorkflowContinuation:
    """A workflow-owned follow-up turn requested at a model boundary.

    The Kernel treats this value as data: it persists the request, appends the
    message to context, and starts the next step.  A workflow plugin decides
    whether continuation is safe and what prompt should be sent; transport
    adapters never need to implement their own autopilot loop.
    """

    continuation_id: str
    message: Message
    reason: str = "workflow"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.continuation_id, "continuation_id")
        if not isinstance(self.message, Message):
            raise ValueError("message must be Message")
        _require_text(self.reason, "reason")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "continuation_id": self.continuation_id,
            "message": self.message.to_dict(),
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    """Data-only input for a Tool Engine invocation."""

    call_id: str
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.call_id, "call_id")
        _require_text(self.tool_name, "tool_name")
        object.__setattr__(self, "arguments", dict(self.arguments))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """Normalized Tool Engine output; permission requests remain explicit."""

    call_id: str
    status: str
    content: str = ""
    output: Mapping[str, Any] | None = None
    error: ErrorInfo | None = None
    permission_request: Mapping[str, Any] | None = None
    permission_decision: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_text(self.call_id, "call_id")
        if self.status not in {"succeeded", "failed", "permission_required"}:
            raise ValueError(f"unsupported tool result status: {self.status}")
        object.__setattr__(self, "output", dict(self.output) if self.output is not None else None)
        object.__setattr__(
            self,
            "permission_request",
            dict(self.permission_request) if self.permission_request is not None else None,
        )
        object.__setattr__(
            self,
            "permission_decision",
            dict(self.permission_decision)
            if self.permission_decision is not None
            else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "status": self.status,
            "content": self.content,
            "output": dict(self.output) if self.output is not None else None,
            "error": self.error.to_dict() if self.error else None,
            "permission_request": (
                dict(self.permission_request) if self.permission_request is not None else None
            ),
            "permission_decision": (
                dict(self.permission_decision)
                if self.permission_decision is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Provider-neutral model request assembled by the Agent Loop Kernel."""

    messages: tuple[Message, ...]
    tools: tuple[Mapping[str, Any], ...] = ()
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(dict(tool) for tool in self.tools))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [message.to_dict() for message in self.messages],
            "tools": [dict(tool) for tool in self.tools],
            "session_id": self.session_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelChunk:
    """One normalized provider stream chunk consumed by the loop."""

    content: str = ""
    thinking: str = ""
    tool_calls: tuple[ToolCallRequest, ...] = ()
    finish_reason: str | None = None
    usage: Usage | None = None
    provider_response_id: str | None = None
    provider_request_id: str | None = None
    activity: Mapping[str, Any] | None = None
    truncated_tool_calls: tuple[Mapping[str, Any], ...] = ()
    raw_finish_reason: str | None = None
    stream_dropped_mid_tool: bool = False
    oversized_tool_calls: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        object.__setattr__(
            self,
            "activity",
            dict(self.activity) if self.activity is not None else None,
        )
        object.__setattr__(
            self,
            "truncated_tool_calls",
            tuple(dict(item) for item in self.truncated_tool_calls),
        )
        object.__setattr__(
            self,
            "oversized_tool_calls",
            tuple(dict(item) for item in self.oversized_tool_calls),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "thinking": self.thinking,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "finish_reason": self.finish_reason,
            "usage": self.usage.to_dict() if self.usage is not None else None,
            "provider_response_id": self.provider_response_id,
            "provider_request_id": self.provider_request_id,
            "activity": dict(self.activity) if self.activity is not None else None,
            "truncated_tool_calls": [dict(item) for item in self.truncated_tool_calls],
            "raw_finish_reason": self.raw_finish_reason,
            "stream_dropped_mid_tool": self.stream_dropped_mid_tool,
            "oversized_tool_calls": [dict(item) for item in self.oversized_tool_calls],
        }


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Pure data controls for one Run.

    Runtime collaborators are deliberately excluded.  They are assembled by
    ``AgentRuntime``/``PluginHost`` rather than sent by an external caller.
    """

    max_steps: int | None = None
    deadline_ms: int | None = None
    provider_stale_seconds: float | None = None
    truncation_continuation_enabled: bool | None = None
    max_truncation_continuations: int | None = None
    max_truncated_tool_call_retries: int | None = None
    max_tool_calls: int | None = None
    max_parallel_tools: int = 1
    thinking_enabled: bool = False
    workflow_id: str | None = None
    workflow_options: Mapping[str, Any] = field(default_factory=dict)
    context_budget: int | None = None
    # Optional run-local registry selections.  Hosts can choose a vendor
    # context/tool/memory/permission/LLM implementation without exposing
    # concrete runtime objects in the request contract.
    component_keys: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_positive(self.max_steps, "max_steps")
        _validate_positive(self.deadline_ms, "deadline_ms")
        if self.provider_stale_seconds is not None and (
            isinstance(self.provider_stale_seconds, bool)
            or not isinstance(self.provider_stale_seconds, (int, float))
            or not math.isfinite(float(self.provider_stale_seconds))
            or self.provider_stale_seconds <= 0
        ):
            raise ValueError("provider_stale_seconds must be finite and positive")
        for field_name in (
            "max_truncation_continuations",
            "max_truncated_tool_call_retries",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.truncation_continuation_enabled is not None and not isinstance(
            self.truncation_continuation_enabled, bool
        ):
            raise ValueError("truncation_continuation_enabled must be a boolean")
        _validate_positive(self.max_tool_calls, "max_tool_calls")
        _validate_positive(self.max_parallel_tools, "max_parallel_tools")
        _validate_positive(self.context_budget, "context_budget")
        if not isinstance(self.workflow_options, Mapping):
            raise ValueError("workflow_options must be a mapping")
        if not isinstance(self.component_keys, Mapping):
            raise ValueError("component_keys must be a mapping")
        for name, key in self.component_keys.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("component_keys names must be non-empty strings")
            if not isinstance(key, str) or not key.strip():
                raise ValueError("component_keys values must be non-empty strings")
        object.__setattr__(self, "workflow_options", dict(self.workflow_options))
        object.__setattr__(
            self,
            "component_keys",
            {name.strip(): key.strip() for name, key in self.component_keys.items()},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "deadline_ms": self.deadline_ms,
            "provider_stale_seconds": self.provider_stale_seconds,
            "truncation_continuation_enabled": self.truncation_continuation_enabled,
            "max_truncation_continuations": self.max_truncation_continuations,
            "max_truncated_tool_call_retries": self.max_truncated_tool_call_retries,
            "max_tool_calls": self.max_tool_calls,
            "max_parallel_tools": self.max_parallel_tools,
            "thinking_enabled": self.thinking_enabled,
            "workflow_id": self.workflow_id,
            "workflow_options": dict(self.workflow_options),
            "context_budget": self.context_budget,
            "component_keys": dict(self.component_keys),
        }


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """Serializable reference to one host-supplied Run input.

    The core protocol carries identity and location only. A plugin decides
    how a concrete attachment kind is authorized, loaded, and represented to
    a model, so transports never need to execute capability logic themselves.
    """

    attachment_id: str
    kind: str
    uri: str
    mime_type: str = "application/octet-stream"
    name: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.attachment_id, "attachment_id")
        _require_text(self.kind, "kind")
        _require_text(self.uri, "uri")
        if not isinstance(self.mime_type, str) or not self.mime_type.strip():
            raise ValueError("mime_type must be a non-empty string")
        if not isinstance(self.name, str):
            raise ValueError("name must be a string")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "kind": self.kind,
            "uri": self.uri,
            "mime_type": self.mime_type,
            "name": self.name,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Input accepted by ``AgentService.start``."""

    request_id: str
    session_id: str
    turn_id: str
    user_input: Message
    attachments: tuple[AttachmentRef, ...] = ()
    options: RunOptions = field(default_factory=RunOptions)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.request_id, "request_id")
        _require_text(self.session_id, "session_id")
        _require_text(self.turn_id, "turn_id")
        if not isinstance(self.user_input, Message):
            raise ValueError("user_input must be a Message")
        if not isinstance(self.attachments, (list, tuple)) or not all(
            isinstance(attachment, AttachmentRef)
            for attachment in self.attachments
        ):
            raise ValueError("attachments must contain AttachmentRef values")
        if not isinstance(self.options, RunOptions):
            raise ValueError("options must be RunOptions")
        object.__setattr__(self, "attachments", tuple(self.attachments))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "user_input": self.user_input.to_dict(),
            "attachments": [
                attachment.to_dict() for attachment in self.attachments
            ],
            "options": self.options.to_dict(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class Usage:
    """Normalized model usage for a completed Run."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class Artifact:
    """A durable reference to a file or other Run output."""

    artifact_id: str
    kind: str
    uri: str
    mime: str = "application/octet-stream"
    size: int = -1
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "uri": self.uri,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class RunResult:
    """Terminal result returned by ``AgentRunHandle.wait``."""

    status: str
    stop_reason: str
    final_message: str = ""
    usage: Usage | None = None
    artifacts: tuple[Artifact, ...] = ()
    error: ErrorInfo | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stop_reason": self.stop_reason,
            "final_message": self.final_message,
            "usage": self.usage.to_dict() if self.usage else None,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "error": self.error.to_dict() if self.error else None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionOpenRequest:
    """Input for opening or resuming a logical Session."""

    session_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.session_id is not None:
            _require_text(self.session_id, "session_id")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Public identity and metadata for a Session."""

    session_id: str
    created_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.session_id, "session_id")
        _require_text(self.created_at, "created_at")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RunStatus:
    """Current observable state of a Run."""

    run_id: str
    session_id: str
    state: str
    sequence: int = 0
    terminal: bool = False

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        _require_text(self.session_id, "session_id")
        _require_text(self.state, "state")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "state": self.state,
            "sequence": self.sequence,
            "terminal": self.terminal,
        }
