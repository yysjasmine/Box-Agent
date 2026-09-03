"""Compatibility names after retirement of the pre-Kernel Loop.

Only historically imported public helpers remain.  Execution delegates to
``compat.runtime``; artifact and turn-classification helpers delegate to their
own capability modules.
"""

from box_agent.persistence.artifacts import (
    avoid_collision,
    ensure_output_dir,
    make_artifact as _make_artifact,
    safe_output_name,
)
from box_agent.config import ToolLimitsConfig
from box_agent.workflows.guards import CompletionGate
from box_agent.workflows.turn_policy import (
    text_is_short_acknowledgement,
    text_is_short_non_task_reply,
    text_requests_plan_start,
)

from .runtime import run_agent_loop

FINAL_SUMMARY_TOOL_CALL_THRESHOLD = (
    ToolLimitsConfig().general.final_summary_after_calls
)

__all__ = [
    "CompletionGate",
    "FINAL_SUMMARY_TOOL_CALL_THRESHOLD",
    "_make_artifact",
    "avoid_collision",
    "ensure_output_dir",
    "run_agent_loop",
    "safe_output_name",
    "text_is_short_acknowledgement",
    "text_is_short_non_task_reply",
    "text_requests_plan_start",
]
