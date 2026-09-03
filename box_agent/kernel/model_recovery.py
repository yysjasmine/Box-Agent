"""Bounded recovery decisions for incomplete provider tool output.

This is a Loop invariant rather than workflow policy: a provider response that
admits an incomplete tool call must never reach the Tool Engine, regardless of
which workflow or tool plugin is active.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from box_agent.tools.argument_limits import RECOMMENDED_GENERATED_BODY_CHARS


TRUNCATED_TOOL_REPAIR_LIMIT = 3
OVERSIZED_ARGUMENT_REPAIR_LIMIT = 1
PROVIDER_STALE_RECOVERY_LIMIT = 3


_TRUNCATED_TOOL_RECOVERY = (
    "The previous response ended because it reached the maximum output length. "
    "None of its tool calls were executed, and no tool side effects occurred. "
    "Retry and complete the original task. Do not assume that any tool call from "
    "that response took effect."
)

_TRUNCATED_WRITE_RECOVERY = (
    "The previous response ended because it reached the maximum output length. "
    "None of the tool calls in that response were executed, so that response made "
    "no file-system changes. Previously accepted chunks, if any, are still pending. "
    "Retry and complete the original task without emitting the entire large file in "
    "one write_file call. For each path, continue with the next_chunk_index returned "
    "by its last successful write_file result; use chunk_index=0 only when no chunk "
    "has been accepted for that path. Keep final=false until the last chunk, then set "
    "final=true."
)


@dataclass(frozen=True, slots=True)
class ModelRecoveryDecision:
    """One provider-output repair or terminal fail-closed decision."""

    reason: str
    attempt: int
    max_attempts: int
    message: str | None
    terminal_message: str
    details: Mapping[str, Any]

    @property
    def exhausted(self) -> bool:
        return self.message is None


def decide_model_recovery(
    *,
    finish_reason: str,
    tool_names: Sequence[str],
    truncated_tool_calls: Sequence[Mapping[str, Any]],
    oversized_tool_calls: Sequence[Mapping[str, Any]],
    attempts: Mapping[str, int],
    partial_content: str = "",
    max_output_continuations: int = TRUNCATED_TOOL_REPAIR_LIMIT,
    max_tool_repair_attempts: int = TRUNCATED_TOOL_REPAIR_LIMIT,
) -> ModelRecoveryDecision | None:
    """Classify provider diagnostics without importing a concrete workflow."""

    normalized_names = tuple(sorted({name for name in tool_names if name}))
    if finish_reason == "provider_stale":
        reason = "provider_stale"
        completed = max(0, int(attempts.get(reason, 0)))
        attempt = completed + 1
        message = None
        if attempt <= PROVIDER_STALE_RECOVERY_LIMIT:
            message = (
                "The model provider stopped producing data before the response "
                "completed. Continue the unfinished task without repeating content "
                "that was already produced, and do not assume an incomplete tool "
                "call took effect."
            )
        return ModelRecoveryDecision(
            reason=reason,
            attempt=attempt,
            max_attempts=PROVIDER_STALE_RECOVERY_LIMIT,
            message=message,
            terminal_message=(
                "The model provider repeatedly stopped producing data; the run "
                "ended without treating the incomplete response as success."
            ),
            details={},
        )

    if finish_reason == "tool_argument_limit" or oversized_tool_calls:
        reason = "tool_argument_limit"
        completed = max(0, int(attempts.get(reason, 0)))
        attempt = completed + 1
        rendered = ", ".join(
            f"{item.get('name') or '?'}={item.get('arguments_len', 0)}/"
            f"{item.get('limit', 0)} chars"
            for item in oversized_tool_calls
        ) or "unknown tool"
        message = None
        if attempt <= OVERSIZED_ARGUMENT_REPAIR_LIMIT:
            message = (
                "The previous tool arguments exceeded the safe streaming budget and "
                f"no tool executed ({rendered}). Do not repeat the same large payload "
                "or increase the token budget. Keep bash commands short and write long "
                f"content with write_file ordered chunks of about "
                f"{RECOMMENDED_GENERATED_BODY_CHARS:,} characters. Continue from the "
                "last accepted next_chunk_index, or use chunk_index=0 only when no "
                "chunk was accepted; set final=true only on the last chunk, then verify "
                "the file."
            )
        return ModelRecoveryDecision(
            reason=reason,
            attempt=attempt,
            max_attempts=OVERSIZED_ARGUMENT_REPAIR_LIMIT,
            message=message,
            terminal_message=(
                "Tool arguments repeatedly exceeded the safe streaming budget; "
                "the run stopped before executing the incomplete call."
            ),
            details={
                "tool_names": list(normalized_names),
                "oversized_tool_calls": [dict(item) for item in oversized_tool_calls],
            },
        )

    has_tool_attempt = bool(normalized_names or truncated_tool_calls)
    if finish_reason not in {"length", "max_tokens"}:
        return None

    if not has_tool_attempt:
        reason = "truncated_output"
        completed = max(0, int(attempts.get(reason, 0)))
        attempt = completed + 1
        limit = max(0, int(max_output_continuations))
        message = None
        if attempt <= limit:
            tail = partial_content.rstrip()[-80:]
            message = (
                "The previous response reached the provider output limit and is "
                f"incomplete near: {tail!r}. Continue directly from that point "
                "without repeating content already emitted."
            )
        return ModelRecoveryDecision(
            reason=reason,
            attempt=attempt,
            max_attempts=limit,
            message=message,
            terminal_message=(
                "The provider repeatedly reached its output limit before the "
                "response completed."
            ),
            details={},
        )

    reason = "truncated_tool_call"
    completed = max(0, int(attempts.get(reason, 0)))
    attempt = completed + 1
    message = None
    limit = max(0, int(max_tool_repair_attempts))
    if attempt <= limit:
        message = (
            _TRUNCATED_WRITE_RECOVERY
            if "write_file" in normalized_names
            else _TRUNCATED_TOOL_RECOVERY
        )
    return ModelRecoveryDecision(
        reason=reason,
        attempt=attempt,
        max_attempts=limit,
        message=message,
        terminal_message=(
            "The provider repeatedly truncated a tool call at the output limit; "
            "the incomplete calls were not executed."
        ),
        details={
            "tool_names": list(normalized_names),
            "truncated_tool_calls": [dict(item) for item in truncated_tool_calls],
        },
    )


__all__ = [
    "ModelRecoveryDecision",
    "OVERSIZED_ARGUMENT_REPAIR_LIMIT",
    "PROVIDER_STALE_RECOVERY_LIMIT",
    "TRUNCATED_TOOL_REPAIR_LIMIT",
    "decide_model_recovery",
]
