"""Run-scoped browser intent policy.

Browser access is a workflow decision: the same registered tools may be
visible or callable depending on the current user turn.  Keeping that decision
behind ``WorkflowPolicy`` makes ACP, CLI, SDK, and compatibility hosts share a
single guard without teaching the Kernel any browser-specific names.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from box_agent.tools.browser_intent import BrowserToolIntentPolicy


def _schema_name(schema: Mapping[str, Any]) -> str:
    name = schema.get("name")
    if isinstance(name, str):
        return name
    function = schema.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name", "") or "")
    return ""


class BrowserIntentWorkflowPolicy:
    """Filter and guard browser Tools from one immutable turn decision."""

    kind = "browser_intent"
    checkpoint_injection_id = "workflow:browser-intent"
    evidence_read_batch_size = 0

    def __init__(self, policy: BrowserToolIntentPolicy | None = None) -> None:
        self._policy = policy

    def for_run(self, request: Any, _bundle: Any | None = None) -> "BrowserIntentWorkflowPolicy":
        user_input = getattr(request, "user_input", None)
        content = getattr(user_input, "content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, Sequence):
            text = " ".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, Mapping)
            )
        else:
            text = str(content or "")
        return type(self)(
            BrowserToolIntentPolicy.for_turn(current_turn_text=text, messages=())
        )

    def filter_tool_schemas(
        self, schemas: Sequence[Mapping[str, Any]]
    ) -> tuple[Mapping[str, Any], ...]:
        if self._policy is None:
            return tuple(schemas)
        return tuple(
            schema
            for schema in schemas
            if self._policy.is_tool_visible(_schema_name(schema))
        )

    def tool_call_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        verified_evidence_urls: set[str] | None = None,
        parallel: bool = False,
    ) -> str | None:
        del verified_evidence_urls, parallel
        if self._policy is None:
            return None
        return self._policy.tool_call_error(tool_name, arguments)


__all__ = ["BrowserIntentWorkflowPolicy"]
