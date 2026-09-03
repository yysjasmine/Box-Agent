"""Deprecated convenience facade; execution is owned by the Agent Kernel."""

from box_agent.compat.agent import (
    Agent,
    AgentRunOptions,
    Colors,
    GoalState,
    goal_autopilot_progress_signature,
    goal_autopilot_prompt,
    goal_payload,
    goal_state_from_payload,
    should_continue_goal_autopilot,
)

__all__ = [
    "Agent",
    "AgentRunOptions",
    "Colors",
    "GoalState",
    "goal_autopilot_prompt",
    "goal_autopilot_progress_signature",
    "goal_payload",
    "goal_state_from_payload",
    "should_continue_goal_autopilot",
]
