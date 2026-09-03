"""ACP stdio host backed exclusively by the plugin-composed Agent Kernel."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from box_agent import __version__
from box_agent.adapters import (
    ACPPermissionGateway,
    HostExtensionRouter,
    KernelACPAgent,
    PostRunProjectionManager,
    build_plugin_host,
)
from box_agent.config import Config
from box_agent.compat.hooks import load_hooks
from box_agent.llm import LLMClient, SessionBoundLLM
from box_agent.kernel.model_stream import resolve_provider_stale_seconds
from box_agent.memory_engine import MemoryProposalService
from box_agent.persistence import (
    SQLiteEffectLedger,
    SQLiteEventLog,
    SQLiteLeaseStore,
    SQLiteSessionStore,
    TaskRegistryArtifactProcessor,
)
from box_agent.plugins.builtins import (
    build_builtin_workflow_bundle,
    register_builtin_context_contributors,
    register_builtin_host_extensions,
    register_builtin_host_projections,
    register_builtin_memory_extraction_hook,
    register_builtin_session_tool_contributors,
    register_builtin_session_trace_hook,
    register_builtin_workflow_policies,
    register_builtin_workflow_bundle,
    register_builtin_workflow_selectors,
    register_native_state_workflow_aliases,
)
from box_agent.permissions import SessionPermissionResolver
from box_agent.llm.retry import RetryConfig as RetryConfigBase
from box_agent.schema import LLMProvider
from box_agent.services.kernel import KernelAgentService
from box_agent.services.utility_prompt import (
    UtilityPromptService,
    default_utility_llm_resolver,
)
from box_agent.services.workspace_profiles import WorkspaceProfileSessionMetadata
from box_agent.tools.jupyter_tool import JupyterSandboxTool
from box_agent.tools.mcp_loader import cleanup_mcp_connections
from box_agent.tools.setup import (
    add_workspace_tools,
    await_mcp_tools,
    await_skill_discovery,
    build_file_delivery_prompt,
    build_image_generation_prompt,
    build_sandbox_info_prompt,
    initialize_base_tools,
    merge_mcp_tools,
    render_system_prompt_template,
)
from box_agent.tools.workspace import SessionScopedToolEngineFactory
from box_agent.tools.session_workspace import SessionWorkspaceToolBuilder
from box_agent.tools.skillhub_contributor import (
    SkillHubDiscoveryState,
    SkillHubHostBridge,
)
from box_agent.workflows import (
    WorkflowEventHook,
    workflow_selector_from_registry,
)


def _build_system_prompt(config: Config, *, skills_enabled: bool) -> str:
    prompt_path = Config.find_config_file(config.agent.system_prompt_path)
    prompt = (
        render_system_prompt_template(prompt_path.read_text(encoding="utf-8"))
        if prompt_path and prompt_path.exists()
        else "You are Box-Agent, an intelligent assistant that can help users complete tasks."
    )
    # Skill metadata is supplied by a Context Engine plugin, so the host
    # template must not retain a second mutable injection slot.
    prompt = prompt.replace("{SKILLS_METADATA}", "" if skills_enabled else "")
    # Workspace execution and delivery rules vary by Session. They are
    # contributed later by SessionModeContextContributor from the same
    # immutable artifact_mode metadata used to construct the Tool Engine.
    prompt = prompt.replace("{SANDBOX_INFO}", "")
    prompt = prompt.replace("{FILE_DELIVERY_INFO}", "")
    return f"{prompt.rstrip()}\n\n{build_image_generation_prompt(config)}"


@dataclass(frozen=True, slots=True)
class KernelACPRuntime:
    """Assembled Kernel dependencies, independent of the ACP transport."""

    agent_factory: Callable[[Any], KernelACPAgent]
    stores: tuple[Any, ...]

    def build_agent(self, conn: Any) -> KernelACPAgent:
        return self.agent_factory(conn)

    async def close(self) -> None:
        for store in self.stores:
            store.shutdown()
        await cleanup_mcp_connections()
        await JupyterSandboxTool.shutdown_all()


async def build_kernel_acp_runtime(
    config: Config,
    *,
    output: Callable[[str], None],
) -> KernelACPRuntime:
    """Compose the plugin-backed Kernel without owning host transport."""

    rcfg = config.llm.retry
    provider = (
        LLMProvider.ANTHROPIC
        if config.llm.provider.lower() == "anthropic"
        else LLMProvider.OPENAI
    )
    llm = LLMClient(
        api_key=config.llm.api_key,
        provider=provider,
        api_base=config.llm.api_base,
        model=config.llm.model,
        retry_config=RetryConfigBase(
            enabled=rcfg.enabled,
            max_retries=rcfg.max_retries,
            initial_delay=rcfg.initial_delay,
            max_delay=rcfg.max_delay,
            exponential_base=rcfg.exponential_base,
        ),
        max_output_tokens=config.llm.max_output_tokens,
        auth_file=config.llm.auth_file,
        timeout=config.llm.timeout,
    )

    memory_manager = None
    if config.agent.enable_memory:
        from box_agent.memory_engine import MemoryManager

        memory_manager = MemoryManager(
            memory_dir=config.agent.memory_dir,
            dedup_jaccard_threshold=config.agent.memory_dedup_jaccard,
        )

    tools, skill_loader, mcp_task, skill_task = await initialize_base_tools(
        config,
        output=output,
        memory_manager=memory_manager,
        llm=llm,
    )
    if mcp_task is not None:
        merge_mcp_tools(tools, await await_mcp_tools(mcp_task))
    await await_skill_discovery(skill_task)

    workspace = Path(config.agent.workspace_dir).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    workspace_preview_tools: list[Any] = []
    add_workspace_tools(
        workspace_preview_tools,
        config,
        workspace,
        sandbox_mode=True,
        allow_full_access=config.tools.allow_full_access,
        non_interactive=False,
        output=output,
        llm=llm,
        env_context=None,
    )
    tools.extend(workspace_preview_tools)

    workflow_bundle = build_builtin_workflow_bundle(
        config=config,
        tools=tools,
    )
    goal_store = workflow_bundle.goal_store
    native_tools = list(workflow_bundle.tools)
    workflow_policy = workflow_bundle.default_policy
    configured_hooks = tuple(
        load_hooks(config.hooks.hooks) if config.hooks.hooks else ()
    )
    workflow_hook = WorkflowEventHook((workflow_policy,))
    system_prompt = _build_system_prompt(
        config,
        skills_enabled=skill_loader is not None,
    )

    db_path = workspace / ".box-agent" / "runs.sqlite3"
    event_log = SQLiteEventLog(db_path)
    effect_ledger = SQLiteEffectLedger(db_path)
    session_store = SQLiteSessionStore(db_path)
    lease_store = SQLiteLeaseStore(db_path)
    stores = (event_log, effect_ledger, session_store, lease_store)
    permission_gateway = ACPPermissionGateway()
    host = build_plugin_host(
        llm=llm,
        tools=native_tools,
        memory_manager=memory_manager,
        workflow_policy=workflow_policy,
        hooks=(*configured_hooks, workflow_hook),
    )
    host.registries["artifacts.processors"].register(
        "default",
        TaskRegistryArtifactProcessor(),
        source="box-agent.task-registry",
        version=__version__,
        state_schema="1",
    )
    if config.agent.enable_memory_extraction:
        register_builtin_memory_extraction_hook(
            host,
            llm=llm,
            memory_manager=memory_manager,
            cooldown=config.agent.memory_extraction_cooldown,
            step_interval=config.agent.memory_extraction_step_interval,
        )
    mode_prompts: dict[str, str] = {}
    for mode, prompt_path_value in {
        "data_analysis": config.agent.analysis_prompt_path,
        "code_agent": config.agent.code_prompt_path,
    }.items():
        prompt_path = Config.find_config_file(prompt_path_value)
        if prompt_path and prompt_path.exists():
            mode_prompts[mode] = prompt_path.read_text(encoding="utf-8")
    permission_resolver = SessionPermissionResolver(config)
    user_mcp_config = Path.home() / ".box-agent" / "config" / "mcp.json"
    mcp_config_path = (
        user_mcp_config
        if user_mcp_config.exists()
        else Config.find_config_file(config.tools.mcp_config_path)
    )
    register_builtin_context_contributors(
        host,
        skill_loader=skill_loader,
        mode_prompts=mode_prompts,
        permission_resolver=permission_resolver,
        memory_manager=memory_manager,
        mcp_config_path=mcp_config_path,
        mcp_globally_enabled=config.tools.enable_mcp,
        sandbox_prompt_builder=build_sandbox_info_prompt,
        file_delivery_prompt_builder=build_file_delivery_prompt,
    )
    skillhub_bridge = SkillHubHostBridge()
    skillhub_discovery_state = SkillHubDiscoveryState(
        tool_search_available=(
            config.tools.enable_mcp and config.tools.mcp.deferred_loading_enabled
        )
    )
    register_builtin_session_tool_contributors(
        host,
        skill_loader=skill_loader,
        skillhub_connection=skillhub_bridge,
        skillhub_discovery_state=skillhub_discovery_state,
    )
    register_builtin_session_trace_hook(host)

    def memory_planning_llm(session_id: str) -> SessionBoundLLM:
        planning_id = session_id or f"local-agent-memory-review-{uuid4()}"
        bound = SessionBoundLLM(llm)
        bound.set_request_context(
            session_id=planning_id,
            turn_id=planning_id,
            title="本地 Agent 记忆整理",
        )
        return bound

    utility_prompt_service = UtilityPromptService(
        default_utility_llm_resolver(llm)
    )
    register_builtin_host_extensions(
        host,
        skill_loader=skill_loader,
        utility_prompt_service=utility_prompt_service,
        memory_proposal_service=MemoryProposalService(
            memory_manager,
            hit_threshold=config.agent.memory_promotion_hit_threshold,
            cooldown_days=config.agent.memory_promotion_cooldown_days,
            planning_llm_resolver=memory_planning_llm,
        ),
    )
    register_builtin_host_projections(
        host,
        utility_prompt_service=utility_prompt_service,
    )
    host.registries["session.metadata"].register(
        "workspace-profile",
        WorkspaceProfileSessionMetadata(),
        source="box-agent.builtin-session-metadata",
        version=__version__,
        state_schema="1",
    )
    native_workflow_ids = register_builtin_workflow_bundle(
        host,
        workflow_bundle,
        workspace=workspace,
        skill_loader=skill_loader,
        tool_limits=config.tool_limits,
    )
    host.registries["permission.policies"].register(
        "default",
        permission_gateway,
        source="kernel.acp",
    )

    preview_tool_ids = {id(tool) for tool in workspace_preview_tools}

    def active_global_tools() -> tuple[Any, ...]:
        """Resolve current global plugins while excluding workspace previews."""

        registry = host.registries["tools.executors"]
        values: list[Any] = []
        seen: set[int] = set()
        for tool in registry.resolve_all(scope="run"):
            object_id = id(tool)
            if object_id in seen or object_id in preview_tool_ids:
                continue
            seen.add(object_id)
            values.append(tool)
        return tuple(values)

    build_session_workspace_tools = SessionWorkspaceToolBuilder(
        config=config,
        default_workspace=workspace,
        permission_resolver=permission_resolver,
        llm=llm,
        output=output,
        skill_loader=skill_loader,
    )

    session_tool_factory = SessionScopedToolEngineFactory(
        base_tools_provider=active_global_tools,
        workspace_tools_builder=build_session_workspace_tools,
        session_tool_contributors_provider=lambda: host.registries[
            "tools.session_contributors"
        ].resolve_all(scope="run"),
        permission_gateway=permission_gateway,
        effect_ledger=effect_ledger,
        hooks_provider=lambda: host.registries["hooks"].resolve_all(scope="run"),
        defer_effect_prepare=True,
        defer_effect_completion=True,
    )
    host.registries["tools.engines"].register(
        "default",
        session_tool_factory,
        source="kernel.acp.session-tools",
        version="1",
        state_schema="1",
    )
    host.registries["session.lifecycle"].register(
        "session-tools",
        session_tool_factory,
        source="kernel.acp.session-tools",
        version="1",
        state_schema="1",
    )
    service = KernelAgentService.from_plugin_host(
        host,
        event_log=event_log,
        effect_ledger=effect_ledger,
        session_store=session_store,
        lease_store=lease_store,
        owner_id=f"acp-kernel-{uuid4().hex}",
    )

    def build_agent(conn: Any) -> KernelACPAgent:
        permission_gateway.bind(conn)
        skillhub_bridge.bind(conn)
        return KernelACPAgent(
            conn,
            service,
            system_prompt=system_prompt,
            workspace_dir=str(workspace),
            goal_store=goal_store,
            native_workflow_ids=native_workflow_ids,
            workflow_selector=workflow_selector_from_registry(host),
            extension_router=HostExtensionRouter(
                host.registries["host.extensions"]
            ),
            projection_manager=PostRunProjectionManager(
                lambda: host.registries["host.projections"].resolve_all(
                    scope="run"
                )
            ),
            session_metadata_contributors=host.registries[
                "session.metadata"
            ].resolve_all(scope="session"),
            provider_stale_seconds=resolve_provider_stale_seconds(
                config.agent.provider_stale_seconds
            ),
            truncation_continuation_enabled=(
                config.agent.retry_on_suspected_truncation
            ),
            max_truncation_continuations=(
                config.agent.max_truncation_continuations
            ),
            max_truncated_tool_call_retries=(
                config.agent.max_truncated_tool_call_retries
            ),
        )

    return KernelACPRuntime(build_agent, stores)


__all__ = [
    "register_builtin_context_contributors",
    "register_builtin_host_extensions",
    "register_builtin_host_projections",
    "register_builtin_workflow_policies",
    "register_builtin_workflow_selectors",
    "register_native_state_workflow_aliases",
    "KernelACPRuntime",
    "build_kernel_acp_runtime",
]
