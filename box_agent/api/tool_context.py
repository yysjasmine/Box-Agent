"""Runtime context passed to one Tool plugin invocation."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .permissions import PermissionDecision, PermissionRequest


ToolProgressPublisher = Callable[
    [str, Mapping[str, Any]], Awaitable[None] | None
]


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Identity, model context, and ordered progress channel for one Tool call.

    A Tool may report progress, but it cannot mint arbitrary Kernel events.
    ``publish_progress`` always wraps plugin data in the stable
    ``tool.progress`` boundary with the owning call identity.
    """

    session_id: str
    run_id: str
    turn_id: str
    call_id: str
    tool_name: str
    _publish: ToolProgressPublisher = field(repr=False, compare=False)
    model_context: Any | None = None
    model: str = ""
    max_output_tokens: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.session_id, "session_id"),
            (self.run_id, "run_id"),
            (self.turn_id, "turn_id"),
            (self.call_id, "call_id"),
            (self.tool_name, "tool_name"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not callable(self._publish):
            raise ValueError("publish must be callable")
        if not isinstance(self.model, str):
            raise ValueError("model must be a string")
        if (
            not isinstance(self.max_output_tokens, int)
            or isinstance(self.max_output_tokens, bool)
            or self.max_output_tokens < 0
        ):
            raise ValueError("max_output_tokens must be a non-negative integer")
        object.__setattr__(self, "metadata", dict(self.metadata))

    async def publish_progress(
        self,
        kind: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        """Publish one ordered, data-only progress fact through the Kernel."""

        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("kind must be a non-empty string")
        if data is not None and not isinstance(data, Mapping):
            raise ValueError("data must be a mapping")
        value = self._publish(
            "tool.progress",
            {
                "call_id": self.call_id,
                "tool_name": self.tool_name,
                "kind": kind,
                "data": dict(data or {}),
            },
        )
        if inspect.isawaitable(value):
            await value

    async def publish_permission_requested(
        self,
        request: PermissionRequest,
    ) -> None:
        """Publish the auditable boundary before a permission broker runs."""

        if not isinstance(request, PermissionRequest):
            raise ValueError("request must be PermissionRequest")
        value = self._publish(
            "permission.requested",
            {"call_id": self.call_id, **request.to_dict()},
        )
        if inspect.isawaitable(value):
            await value

    async def publish_permission_resolved(
        self,
        decision: PermissionDecision,
    ) -> None:
        """Publish the broker decision before the Tool executor is entered."""

        if not isinstance(decision, PermissionDecision):
            raise ValueError("decision must be PermissionDecision")
        value = self._publish(
            "permission.resolved",
            {"call_id": self.call_id, **decision.to_dict()},
        )
        if inspect.isawaitable(value):
            await value


__all__ = ["ToolExecutionContext", "ToolProgressPublisher"]
