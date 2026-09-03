"""Deprecated import facade for the retired pre-Kernel execution module.

Use :mod:`box_agent.api`, :mod:`box_agent.kernel`, and
``KernelAgentService.from_plugin_host`` for new integrations.
"""

from box_agent.compat.core import (
    CompletionGate,
    FINAL_SUMMARY_TOOL_CALL_THRESHOLD,
    _make_artifact,
    avoid_collision,
    ensure_output_dir,
    run_agent_loop,
    safe_output_name,
    text_is_short_acknowledgement,
    text_is_short_non_task_reply,
    text_requests_plan_start,
)

__all__ = [
    "CompletionGate",
    "FINAL_SUMMARY_TOOL_CALL_THRESHOLD",
    "run_agent_loop",
]
