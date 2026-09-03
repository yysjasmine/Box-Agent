"""Host-neutral workflow policies composed by the shared runtime."""

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..config import ToolLimitsConfig
from .guards import CompletionGate
from .contract import WorkflowPolicy, is_natural_end_reason
from .browser import BrowserIntentWorkflowPolicy
from .response_continuation import ResponseContinuationWorkflowPolicy
from .attachment import AttachmentInspectionPolicy
from ..persistence.workflow_checkpoint_store import load_workflow_checkpoint
from ..persistence.workflow_owner_store import WorkflowOwner
from .controlled_presentation import RESEARCH_ROUND_LIMIT, ControlledPresentationPolicy
from .external_skill import (
    EXTERNAL_SKILL_WORKFLOW_KIND,
    ExternalSkillRunPolicy,
    build_external_skill_completion_gate,
    build_external_skill_completion_gate_from_options,
    external_skill_policy_from_options,
    external_skill_workflow_selection,
    resolve_explicit_skill_invocation,
)
from .presentation_contract import (
    IMAGE_GENERATION_POLICY_OPTION,
    RESEARCH_MODE_OPTION,
    RESEARCH_ROUND_LIMIT_OPTION,
    WORKFLOW_KIND as CONTROLLED_PRESENTATION_WORKFLOW_KIND,
)
from .presentation_preflight import (
    PresentationPreflightService,
    build_presentation_preflight_analysis_text,
    build_presentation_preflight_result,
    build_presentation_recommendation_prompt,
    load_presentation_preflight_config,
)
from .presentation_provider import (
    parse_host_presentation_config,
    resolve_presentation_skill_provider,
)
from .presentation_routing import build_presentation_completion_gate
from ..kernel.workflow_composite import CompositeWorkflowPolicy
from .goal import (
    GoalReadTool,
    GoalState,
    GoalStore,
    GoalToolStore,
    GoalWorkflowPolicy,
    GoalWriteTool,
    apply_goal_action,
    build_goal_tools,
    extend_goal_items,
    goal_action_from_metadata,
    goal_autopilot_progress_signature,
    goal_autopilot_prompt,
    goal_autopilot_terminal_message,
    goal_items,
    goal_payload,
    goal_snapshot,
    goal_state_from_payload,
    should_continue_goal_autopilot,
)
from .plan import (
    PlanWorkflowPolicy,
    SessionPlanReadTool,
    SessionPlanStore,
    SessionPlanWriteTool,
    build_plan_tools,
)
from .hooks import WorkflowEventHook
from .completion_gate import (
    CompletionGateWorkflowPolicy,
    completion_gate_from_payload,
    completion_gate_to_payload,
)
from .routing import text_requests_native_plan
from .selection import (
    CompletionGateWorkflowSelector,
    ExternalSkillWorkflowSelector,
    PresentationWorkflowSelector,
    WorkflowSelector,
    WorkflowSelectorChain,
    prompt_text,
    workflow_selector_from_registry,
)


def create_workflow_policy(
    *,
    workflow_kind: str | None,
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
    workflow_options: Mapping[str, Any] | None = None,
    available_tool_names: frozenset[str] | None = None,
    skill_loader: Any | None = None,
) -> WorkflowPolicy | None:
    """Create a per-run policy without exposing implementations to the kernel."""
    if workflow_kind == ControlledPresentationPolicy.kind:
        research_mode = (workflow_options or {}).get(RESEARCH_MODE_OPTION)
        research_round_limit = (workflow_options or {}).get(
            RESEARCH_ROUND_LIMIT_OPTION
        )
        image_generation_policy = (workflow_options or {}).get(
            IMAGE_GENERATION_POLICY_OPTION
        )
        policy = ControlledPresentationPolicy(
            workspace_dir=workspace_dir,
            artifact_root_dir=artifact_root_dir,
            research_mode=(
                research_mode if isinstance(research_mode, str) else None
            ),
            research_round_limit=(
                research_round_limit
                if isinstance(research_round_limit, int)
                and not isinstance(research_round_limit, bool)
                and research_round_limit > 0
                else RESEARCH_ROUND_LIMIT
            ),
            image_generation_policy=(
                image_generation_policy
                if isinstance(image_generation_policy, str)
                else None
            ),
            available_tool_names=available_tool_names,
            skill_loader=skill_loader,
        )
        resume_checkpoint = load_workflow_checkpoint(
            workspace_dir=workspace_dir,
            workflow_kind=workflow_kind,
        )
        if resume_checkpoint is not None:
            policy.attach_resume_checkpoint(resume_checkpoint)
        return policy
    if workflow_kind == EXTERNAL_SKILL_WORKFLOW_KIND:
        policy = external_skill_policy_from_options(
            workspace_dir=workspace_dir,
            artifact_root_dir=artifact_root_dir,
            workflow_options=workflow_options,
        )
        if skill_loader is not None:
            policy.skill_loader = skill_loader
        resume_checkpoint = load_workflow_checkpoint(
            workspace_dir=workspace_dir,
            workflow_kind=workflow_kind,
        )
        if resume_checkpoint is not None:
            policy.attach_resume_checkpoint(resume_checkpoint)
        return policy
    return None


def recover_completion_gate(
    workspace_dir: str | Path,
    tool_limits: ToolLimitsConfig | None = None,
) -> CompletionGate | None:
    """Recover the first incomplete built-in workflow from durable artifacts."""
    from .presentation_recovery import recover_presentation_completion_gate

    external_checkpoint = load_workflow_checkpoint(
        workspace_dir=workspace_dir,
        workflow_kind=EXTERNAL_SKILL_WORKFLOW_KIND,
    )
    controlled_checkpoint = load_workflow_checkpoint(
        workspace_dir=workspace_dir,
        workflow_kind=CONTROLLED_PRESENTATION_WORKFLOW_KIND,
    )
    if external_checkpoint is not None and controlled_checkpoint is not None:
        return None
    if external_checkpoint is not None:
        return build_external_skill_completion_gate_from_options(
            workspace_dir=workspace_dir,
            workflow_options=external_checkpoint.workflow_options,
            tool_limits=tool_limits,
        )
    if controlled_checkpoint is not None:
        gate = build_presentation_completion_gate(
            "继续制作 PPT",
            workspace_dir,
            confirmed_presentation=True,
            tool_limits=tool_limits,
        )
        if gate is not None:
            return replace(
                gate,
                workflow_options={
                    **gate.workflow_options,
                    **controlled_checkpoint.workflow_options,
                },
            )

    controlled = recover_presentation_completion_gate(
        workspace_dir,
        tool_limits=tool_limits,
    )
    if controlled is not None:
        return controlled
    return None


def completion_gate_from_owner(
    owner: WorkflowOwner,
    *,
    workspace_dir: str | Path,
    tool_limits: ToolLimitsConfig | None = None,
) -> CompletionGate | None:
    """Rebuild exactly the runtime-selected workflow kind for a new ACP session."""
    if owner.workflow_kind == EXTERNAL_SKILL_WORKFLOW_KIND:
        return build_external_skill_completion_gate_from_options(
            workspace_dir=workspace_dir,
            workflow_options=owner.workflow_options,
            tool_limits=tool_limits,
        )
    if owner.workflow_kind == CONTROLLED_PRESENTATION_WORKFLOW_KIND:
        gate = build_presentation_completion_gate(
            "继续制作 PPT",
            workspace_dir,
            confirmed_presentation=True,
            tool_limits=tool_limits,
        )
        if gate is None:
            return None
        return replace(
            gate,
            workflow_options={**gate.workflow_options, **owner.workflow_options},
        )
    return None


__all__ = [
    "BrowserIntentWorkflowPolicy",
    "ResponseContinuationWorkflowPolicy",
    "CompositeWorkflowPolicy",
    "CompletionGateWorkflowPolicy",
    "CompletionGateWorkflowSelector",
    "completion_gate_from_payload",
    "completion_gate_to_payload",
    "ControlledPresentationPolicy",
    "completion_gate_from_owner",
    "CONTROLLED_PRESENTATION_WORKFLOW_KIND",
    "EXTERNAL_SKILL_WORKFLOW_KIND",
    "ExternalSkillRunPolicy",
    "ExternalSkillWorkflowSelector",
    "GoalReadTool",
    "GoalState",
    "GoalStore",
    "GoalToolStore",
    "GoalWorkflowPolicy",
    "GoalWriteTool",
    "apply_goal_action",
    "WorkflowEventHook",
    "is_natural_end_reason",
    "PlanWorkflowPolicy",
    "PresentationWorkflowSelector",
    "PresentationPreflightService",
    "SessionPlanReadTool",
    "SessionPlanStore",
    "SessionPlanWriteTool",
    "WorkflowSelector",
    "WorkflowSelectorChain",
    "build_external_skill_completion_gate",
    "external_skill_workflow_selection",
    "build_presentation_preflight_analysis_text",
    "build_presentation_preflight_result",
    "build_presentation_recommendation_prompt",
    "build_goal_tools",
    "extend_goal_items",
    "goal_action_from_metadata",
    "goal_items",
    "goal_payload",
    "goal_snapshot",
    "goal_state_from_payload",
    "goal_autopilot_progress_signature",
    "goal_autopilot_prompt",
    "goal_autopilot_terminal_message",
    "build_plan_tools",
    "create_workflow_policy",
    "load_presentation_preflight_config",
    "parse_host_presentation_config",
    "recover_completion_gate",
    "resolve_explicit_skill_invocation",
    "resolve_presentation_skill_provider",
    "should_continue_goal_autopilot",
    "text_requests_native_plan",
    "prompt_text",
    "workflow_selector_from_registry",
]
