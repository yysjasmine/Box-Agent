"""Built-in capability registration shared by ACP, CLI, and SDK hosts."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from box_agent import __version__
from box_agent.adapters.builtin_extensions import (
    MCPDisconnectExtension,
    MCPReconnectExtension,
    MCPStatusExtension,
    MemoryProposalApplyExtension,
    MemoryProposalListExtension,
    PresentationPreflightExtension,
    SkillListExtension,
    UtilityPromptExtension,
    WorkspaceGetExtension,
    WorkspaceListExtension,
    WorkspaceSetExtension,
)
from box_agent.context import (
    ActionHintContextContributor,
    ExpertContextContributor,
    SessionEnvironmentContextContributor,
    SessionModeContextContributor,
    SkillCatalogContextContributor,
)
from box_agent.permissions import SessionPermissionResolver
from box_agent.workflows.guards import CompletionGate
from box_agent.memory_engine import (
    ConversationMemoryExtractionHook,
    MemoryProposalService,
)
from box_agent.services.utility_prompt import UtilityPromptService
from box_agent.services.follow_up_suggestions import FollowUpSuggestionsProjection
from box_agent.observability import SessionTraceHook
from box_agent.observability.session_trace import session_trace_enabled
from box_agent.tools.registration import MCPToolRegistryController
from box_agent.tools.experts import ExpertSkillToolContributor
from box_agent.tools.skillhub_contributor import (
    SkillHubContextContributor,
    SkillHubDiscoveryState,
    SkillHubToolContributor,
)
from box_agent.workflows import (
    AttachmentInspectionPolicy,
    BrowserIntentWorkflowPolicy,
    CompletionGateWorkflowPolicy,
    CompletionGateWorkflowSelector,
    CompositeWorkflowPolicy,
    ControlledPresentationPolicy,
    ExternalSkillRunPolicy,
    ExternalSkillWorkflowSelector,
    PresentationPreflightService,
    PresentationWorkflowSelector,
    GoalStore,
    GoalWorkflowPolicy,
    PlanWorkflowPolicy,
    ResponseContinuationWorkflowPolicy,
    SessionPlanStore,
    build_goal_tools,
    build_plan_tools,
)


@dataclass(frozen=True, slots=True)
class BuiltinWorkflowBundle:
    """One shared Goal/Plan/default-policy graph for every host adapter."""

    tools: tuple[Any, ...]
    goal_store: GoalStore
    plan_store: SessionPlanStore
    goal_policy: GoalWorkflowPolicy
    plan_policy: PlanWorkflowPolicy | None
    default_policy: CompositeWorkflowPolicy


def build_builtin_workflow_bundle(
    *,
    config: Any,
    tools: Iterable[Any],
    goal_store: GoalStore | None = None,
    plan_store: SessionPlanStore | None = None,
    goal_autopilot_enabled: bool | None = None,
    include_attachment_policy: bool = True,
) -> BuiltinWorkflowBundle:
    """Build host-neutral built-in state, Tools, and default policies once."""

    goals = goal_store or GoalStore()
    plans = plan_store or SessionPlanStore()
    input_tools = tuple(tools)
    values: list[Any] = []
    seen: set[int] = set()
    for tool in input_tools:
        if id(tool) in seen:
            continue
        seen.add(id(tool))
        if str(getattr(tool, "name", "") or "") in {
            "goal_read",
            "goal_write",
            "plan_read",
            "plan_write",
        }:
            continue
        values.append(tool)
    configured_plan = bool(getattr(config.tools, "enable_plan", False))
    incoming_names = {
        str(getattr(tool, "name", "") or "")
        for tool in input_tools
    }
    has_plan_tools = configured_plan or bool(
        incoming_names & {"plan_read", "plan_write"}
    )
    values.extend(build_goal_tools(goals))
    if has_plan_tools:
        values.extend(build_plan_tools(plans))

    configured_autopilot = bool(config.agent.goal_autopilot_enabled)
    autopilot = (
        configured_autopilot
        if goal_autopilot_enabled is None
        else configured_autopilot and bool(goal_autopilot_enabled)
    )
    goal_policy = GoalWorkflowPolicy(
        goals,
        autopilot_enabled=autopilot,
        max_continuations=config.agent.goal_autopilot_max_turns,
        no_progress_turns=config.agent.goal_autopilot_no_progress_turns,
        max_seconds=config.agent.goal_autopilot_max_seconds,
    )
    plan_policy = PlanWorkflowPolicy(plans) if has_plan_tools else None
    default_policy = CompositeWorkflowPolicy(
        policy
        for policy in (
            BrowserIntentWorkflowPolicy(),
            AttachmentInspectionPolicy() if include_attachment_policy else None,
            goal_policy,
            plan_policy,
            ResponseContinuationWorkflowPolicy(
                enabled=config.agent.retry_on_suspected_truncation,
                max_continuations=config.agent.max_truncation_continuations,
            ),
        )
        if policy is not None
    )
    return BuiltinWorkflowBundle(
        tools=tuple(values),
        goal_store=goals,
        plan_store=plans,
        goal_policy=goal_policy,
        plan_policy=plan_policy,
        default_policy=default_policy,
    )


def register_builtin_workflow_bundle(
    host: Any,
    bundle: BuiltinWorkflowBundle,
    *,
    workspace: Path,
    skill_loader: Any | None = None,
    tool_limits: Any | None = None,
) -> frozenset[str]:
    """Register selectable and session-state workflows through one path."""

    registered = set(
        register_builtin_workflow_policies(
            host,
            workspace=workspace,
            tools=bundle.tools,
            skill_loader=skill_loader,
        )
    )
    registered.update(
        register_native_state_workflow_aliases(
            host,
            goal_policy=bundle.goal_policy,
            plan_policy=bundle.plan_policy,
        )
    )
    register_builtin_workflow_selectors(
        host,
        workspace=workspace,
        skill_loader=skill_loader,
        tool_limits=tool_limits,
    )
    return frozenset(registered)


def register_builtin_workflow_policies(
    host: Any,
    *,
    workspace: Path,
    tools: Iterable[Any],
    skill_loader: Any | None = None,
) -> frozenset[str]:
    tool_names = frozenset(
        str(getattr(tool, "name", "") or "").strip()
        for tool in tools
        if str(getattr(tool, "name", "") or "").strip()
    )
    policies = (
        CompletionGateWorkflowPolicy(CompletionGate(), workspace_dir=str(workspace)),
        ControlledPresentationPolicy(
            workspace_dir=str(workspace),
            artifact_root_dir=workspace / "output",
            available_tool_names=tool_names,
            skill_loader=skill_loader,
        ),
        ExternalSkillRunPolicy(
            workspace_dir=str(workspace),
            artifact_root_dir=workspace / "output",
            skill_loader=skill_loader,
        ),
    )
    registry = host.registries["workflows"]
    registered: set[str] = set()
    for policy in policies:
        registry.register(
            policy.kind,
            policy,
            source="box-agent.builtin-workflows",
            version=__version__,
            state_schema="1",
        )
        registered.add(policy.kind)
        if policy.kind == "external_skill":
            registry.register(
                "skill",
                policy,
                source="box-agent.builtin-workflows",
                version=__version__,
                state_schema="1",
            )
            registered.add("skill")
    return frozenset(registered)


def register_builtin_workflow_selectors(
    host: Any,
    *,
    workspace: Path,
    skill_loader: Any | None = None,
    tool_limits: Any | None = None,
) -> None:
    from box_agent.workflows.completion import build_auto_completion_gate

    registry = host.registries["workflow.selectors"]
    common = {
        "source": "box-agent.builtin-workflows",
        "version": __version__,
        "state_schema": "1",
    }
    registry.register(
        "presentation",
        PresentationWorkflowSelector(workspace, skill_loader, tool_limits),
        priority=100,
        **common,
    )
    registry.register(
        "external_skill",
        ExternalSkillWorkflowSelector(skill_loader),
        priority=50,
        **common,
    )
    registry.register(
        "completion_gate",
        CompletionGateWorkflowSelector(
            workspace,
            build_auto_completion_gate,
            tool_limits,
        ),
        priority=10,
        **common,
    )


def register_builtin_context_contributors(
    host: Any,
    *,
    skill_loader: Any | None,
    mode_prompts: dict[str, str] | None = None,
    permission_resolver: SessionPermissionResolver | None = None,
    memory_manager: Any | None = None,
    mcp_config_path: Path | None = None,
    mcp_globally_enabled: bool = True,
    sandbox_prompt_builder: Callable[[bool], str] | None = None,
    file_delivery_prompt_builder: Callable[[bool], str] | None = None,
) -> None:
    registry = host.registries["context.contributors"]
    registry.register(
        "experts",
        ExpertContextContributor(),
        source="box-agent.builtin-context",
        priority=200,
        version=__version__,
        state_schema="1",
    )
    if mode_prompts or sandbox_prompt_builder or file_delivery_prompt_builder:
        registry.register(
            "session-mode",
            SessionModeContextContributor(
                mode_prompts or {},
                sandbox_prompt_builder=sandbox_prompt_builder,
                file_delivery_prompt_builder=file_delivery_prompt_builder,
            ),
            source="box-agent.builtin-context",
            priority=180,
            version=__version__,
            state_schema="1",
        )
    if permission_resolver is not None:
        registry.register(
            "session-environment",
            SessionEnvironmentContextContributor(permission_resolver),
            source="box-agent.builtin-context",
            priority=170,
            version=__version__,
            state_schema="1",
        )
    if skill_loader is not None:
        registry.register(
            "skill_catalog",
            SkillCatalogContextContributor(skill_loader),
            source="box-agent.builtin-context",
            priority=100,
            version=__version__,
            state_schema="1",
        )
    registry.register(
        "action-hints",
        ActionHintContextContributor(
            memory_manager=memory_manager,
            mcp_config_path=mcp_config_path,
            mcp_globally_enabled=mcp_globally_enabled,
        ),
        source="box-agent.builtin-context",
        priority=50,
        version=__version__,
        state_schema="1",
    )
    registry.register(
        "skill-marketplace",
        SkillHubContextContributor(),
        source="box-agent.builtin-context",
        priority=40,
        version=__version__,
        state_schema="1",
    )


def register_builtin_session_tool_contributors(
    host: Any,
    *,
    skill_loader: Any | None,
    skillhub_connection: Any | None = None,
    skillhub_discovery_state: SkillHubDiscoveryState | None = None,
) -> None:
    """Register Tool factories whose instances must stay Session-local."""

    registry = host.registries["tools.session_contributors"]
    if skill_loader is not None:
        registry.register(
            "expert-skills",
            ExpertSkillToolContributor(skill_loader),
            source="box-agent.builtin-tools",
            priority=100,
            version=__version__,
            state_schema="1",
        )
    if skillhub_connection is not None:
        registry.register(
            "skill-marketplace",
            SkillHubToolContributor(
                skillhub_connection,
                skill_loader=skill_loader,
                discovery_state=skillhub_discovery_state,
            ),
            source="box-agent.builtin-tools",
            priority=90,
            version=__version__,
            state_schema="1",
        )
    if skillhub_discovery_state is not None:
        host.registries["hooks"].register(
            "skill-marketplace-discovery",
            skillhub_discovery_state,
            source="box-agent.builtin-tools",
            version=__version__,
            state_schema="1",
        )


def register_builtin_memory_extraction_hook(
    host: Any,
    *,
    llm: Any,
    memory_manager: Any | None,
    cooldown: int,
    step_interval: int,
) -> ConversationMemoryExtractionHook | None:
    """Register event-driven extraction without coupling it to a host adapter."""

    if memory_manager is None:
        return None
    from box_agent.memory_engine import MemoryExtractor

    hook = ConversationMemoryExtractionHook(
        lambda session_id: MemoryExtractor(
            llm=llm,
            memory_manager=memory_manager,
            session_id=session_id,
            cooldown=cooldown,
            step_interval=step_interval,
        )
    )
    host.registries["hooks"].register(
        "memory-extraction",
        hook,
        source="box-agent.builtin-memory",
        version=__version__,
        state_schema="1",
    )
    return hook


def register_builtin_session_trace_hook(host: Any) -> SessionTraceHook | None:
    if not session_trace_enabled():
        return None
    hook = SessionTraceHook()
    host.registries["hooks"].register(
        "session-trace",
        hook,
        source="box-agent.builtin-observability",
        version=__version__,
        state_schema="1",
    )
    return hook


def register_builtin_host_extensions(
    host: Any,
    *,
    skill_loader: Any | None,
    utility_prompt_service: UtilityPromptService | None = None,
    memory_proposal_service: MemoryProposalService | None = None,
) -> None:
    registry = host.registries["host.extensions"]
    common = {
        "source": "box-agent.builtin-host-extensions",
        "version": __version__,
        "state_schema": "1",
    }
    mcp_controller = MCPToolRegistryController(host)
    extensions: list[tuple[str, Any]] = [
        ("list_skills", SkillListExtension(skill_loader)),
        ("workspace/list", WorkspaceListExtension()),
        ("workspace/get", WorkspaceGetExtension()),
        ("workspace/set", WorkspaceSetExtension()),
        ("mcp/status", MCPStatusExtension()),
        ("mcp/reconnect", MCPReconnectExtension(mcp_controller)),
        ("mcp/disconnect", MCPDisconnectExtension(mcp_controller)),
    ]
    if utility_prompt_service is not None:
        extensions.extend(
            (
                ("llm/prompt", UtilityPromptExtension(utility_prompt_service)),
                (
                    "presentation/preflight",
                    PresentationPreflightExtension(
                        PresentationPreflightService(utility_prompt_service)
                    ),
                ),
            )
        )
    if memory_proposal_service is not None:
        extensions.extend(
            (
                (
                    "memory_proposal_list",
                    MemoryProposalListExtension(memory_proposal_service),
                ),
                (
                    "memory_proposal_apply",
                    MemoryProposalApplyExtension(memory_proposal_service),
                ),
            )
        )
    for method, handler in extensions:
        registry.register(method, handler, **common)


def register_builtin_host_projections(
    host: Any,
    *,
    utility_prompt_service: UtilityPromptService,
) -> None:
    host.registries["host.projections"].register(
        "follow-up-suggestions",
        FollowUpSuggestionsProjection(utility_prompt_service),
        source="box-agent.builtin-host-projections",
        version=__version__,
        state_schema="1",
    )


def register_native_state_workflow_aliases(
    host: Any,
    *,
    goal_policy: Any,
    plan_policy: Any | None = None,
) -> frozenset[str]:
    registry = host.registries["workflows"]
    common = {
        "source": "box-agent.builtin-workflows",
        "version": __version__,
        "state_schema": "1",
    }
    registry.register("goal", goal_policy, **common)
    registry.register("autopilot", goal_policy, **common)
    registered = {"goal", "autopilot"}
    if plan_policy is not None:
        registry.register("plan", plan_policy, **common)
        registered.add("plan")
    return frozenset(registered)


__all__ = [
    "BuiltinWorkflowBundle",
    "build_builtin_workflow_bundle",
    "register_builtin_workflow_bundle",
    "register_builtin_context_contributors",
    "register_builtin_host_extensions",
    "register_builtin_host_projections",
    "register_builtin_memory_extraction_hook",
    "register_builtin_session_tool_contributors",
    "register_builtin_session_trace_hook",
    "register_builtin_workflow_policies",
    "register_builtin_workflow_selectors",
    "register_native_state_workflow_aliases",
]
