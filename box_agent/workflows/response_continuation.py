"""Conservative suspected-truncation continuation WorkflowPolicy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from box_agent.api import Message, WorkflowContinuation
from box_agent.workflows.guards import (
    looks_like_truncated_output,
    reply_is_substantial,
    truncation_continuation_text,
)


class ResponseContinuationWorkflowPolicy:
    """Request one bounded continuation when a normal response looks cut off."""

    kind = "response_continuation"
    checkpoint_injection_id = "workflow:response-continuation"
    evidence_read_batch_size = 0
    max_tool_calls = None

    def __init__(self, *, enabled: bool = True, max_continuations: int = 3) -> None:
        self._enabled = bool(enabled)
        self._max_continuations = max(0, int(max_continuations))
        self._continuations = 0
        self._last_content = ""
        self._last_finish_reason = ""
        self._last_output_tokens: int | None = None

    def for_run(self, request: Any, bundle: Any | None = None) -> "ResponseContinuationWorkflowPolicy":
        options = getattr(request, "options", None)
        enabled = getattr(options, "truncation_continuation_enabled", None)
        max_continuations = getattr(options, "max_truncation_continuations", None)
        policy = type(self)(
            enabled=self._enabled if enabled is None else bool(enabled),
            max_continuations=int(
                self._max_continuations
                if max_continuations is None
                else max_continuations
            ),
        )
        events = ()
        if isinstance(bundle, Mapping):
            events = bundle.get("events", ())
        elif bundle is not None:
            events = getattr(bundle, "events", ())
        policy._continuations = sum(
            1
            for event in events or ()
            if str(getattr(event, "type", "") or "")
            == "workflow.continuation.requested"
            and isinstance(getattr(event, "payload", None), Mapping)
            and event.payload.get("reason") == "suspected_truncation"
        )
        return policy

    def record_model_response(
        self,
        *,
        content: str,
        finish_reason: str,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        self._last_content = content
        self._last_finish_reason = finish_reason
        output_tokens = usage.get("output_tokens") if isinstance(usage, Mapping) else None
        self._last_output_tokens = (
            int(output_tokens)
            if isinstance(output_tokens, int) and not isinstance(output_tokens, bool)
            else None
        )

    def next_continuation(
        self,
        *,
        stop_reason: str,
        final_content: str,
        step: int,
    ) -> WorkflowContinuation | None:
        del step
        content = final_content or self._last_content
        if (
            not self._enabled
            or stop_reason not in {"stop", "end_turn"}
            or self._last_finish_reason not in {"", "stop", "end_turn"}
            or self._continuations >= self._max_continuations
            or not reply_is_substantial(len(content), self._last_output_tokens)
            or not looks_like_truncated_output(content)
        ):
            return None
        self._continuations += 1
        return WorkflowContinuation(
            continuation_id=f"suspected-truncation-{self._continuations}",
            message=Message.user(
                truncation_continuation_text(content.rstrip()[-80:]),
                user_visible=False,
                workflow=self.kind,
            ),
            reason="suspected_truncation",
            metadata={"attempt": self._continuations},
        )


__all__ = ["ResponseContinuationWorkflowPolicy"]
