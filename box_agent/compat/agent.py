"""Source-compatible ``Agent`` facade over the plugin-composed Kernel.

This class preserves the mature convenience API for embedders.  It owns only
local presentation/history state; execution always crosses ``compat.runtime``
into the canonical Agent Loop Kernel.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from box_agent.config import AgentConfig, ToolLimitsConfig
from box_agent.context.resource_ledger import ContextResourceLedger
from box_agent.compat.events import (
    AgentEvent,
    ContentEvent,
    ContextCheckpointEvent,
    DoneEvent,
    MemoryProposalEvent,
    StopReason,
    ThinkingEvent,
    TokenUsageEvent,
)
from box_agent.observability.logger import AgentLogger
from box_agent.workflows.guards import CompletionGate
from box_agent.persistence.session_continuation import ContinuationMessage
from box_agent.schema import Message
from box_agent.tools.result_storage import ToolResultStorage
from box_agent.tools.base import Tool, build_tool_name_index
from box_agent.tools.mcp_tool_catalog import get_mcp_tool_catalog
from box_agent.tools.mcp_tool_search import (
    ActivatedMCPTool,
    MCPToolExposureManager,
    ToolSearchTool,
)
from box_agent.tools.skill_preload import build_active_skills_prompt
from box_agent.workflows.goal import (
    GoalReadTool,
    GoalState,
    GoalWriteTool,
    goal_autopilot_progress_signature,
    goal_autopilot_prompt,
    goal_payload,
    goal_state_from_payload,
    should_continue_goal_autopilot as _continue_goal,
)
from box_agent.compat.goal import LegacyGoalStore
from box_agent.workflows import GoalWorkflowPolicy

from .runtime import run_agent_loop

_DEFAULT_AGENT_CONFIG = AgentConfig()
_ACTIVE_SKILL_TOKEN_BUDGET = 32_000


@dataclass(frozen=True, slots=True)
class AgentRunOptions:
    """Legacy collaborator snapshot translated only at the compatibility edge."""

    llm: Any
    summary_llm: Any | None = None
    is_cancelled: Callable[[], bool] | None = None
    logger: AgentLogger | None = None
    permission_negotiator: Any | None = None
    hooks: list[Any] | None = None
    memory_manager: Any | None = None
    memory_extractor: Any | None = None
    memory_turn_id: str = ""
    inject_queue: asyncio.Queue[Any] | None = None
    session_id: str = ""
    turn_id: str = ""
    title: str = ""
    force_plan_start: bool = False
    require_plan_approval: bool = False
    plan_approval: dict[str, Any] | None = None
    plan_start_text: str | None = None
    pause_after_plan_write: bool = False
    max_tool_calls: int | None = None
    web_search_total_limit: int | None = None
    no_progress_limit: int | None = None
    completion_gate: CompletionGate | None = None
    artifact_detection_enabled: bool = True
    artifact_root_dir: str | Path | None = None
    cache_fingerprint_context: dict[str, Any] | None = None
    cache_fingerprint_sink: Callable[[dict[str, Any]], None] | None = None
    workflow_policy: Any | None = None
    current_turn_text: str | None = None


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"


class Agent:
    """Convenience session facade; the Kernel remains the sole executor."""

    def __init__(
        self,
        llm_client: Any,
        system_prompt: str,
        tools: list[Tool],
        max_steps: int = _DEFAULT_AGENT_CONFIG.max_steps,
        workspace_dir: str = "./workspace",
        token_limit: int = 113_400,
        hooks: list[Any] | None = None,
        thinking_enabled: bool = False,
        memory_promotion_enabled: bool = False,
        memory_promotion_hit_threshold: int = 5,
        memory_promotion_cooldown_days: int = 14,
        max_parallel_tools: int = 8,
        parallel_tool_timeout_seconds: float | None = 900.0,
        provider_stale_seconds: float | None = None,
        truncation_continuation_enabled: bool = True,
        max_truncation_continuations: int = 3,
        max_truncated_tool_call_retries: int = 3,
        truncated_tool_call_boost_cap: int = 32_768,
        context_resource_dedup_enabled: bool = True,
        tool_limits: ToolLimitsConfig | None = None,
        deferred_mcp_loading_enabled: bool = True,
    ) -> None:
        del parallel_tool_timeout_seconds, truncated_tool_call_boost_cap
        self.llm = llm_client
        self.max_steps = max_steps
        self.token_limit = token_limit
        self.tool_limits = tool_limits or ToolLimitsConfig()
        self.max_parallel_tools = max_parallel_tools
        self.provider_stale_seconds = provider_stale_seconds
        self.truncation_continuation_enabled = truncation_continuation_enabled
        self.max_truncation_continuations = max_truncation_continuations
        self.max_truncated_tool_call_retries = max_truncated_tool_call_retries
        self.tools = {
            tool.name: tool
            for tool in tools
            if not deferred_mcp_loading_enabled
            or getattr(tool, "mcp_tool_id", None) is None
        }
        self.activated_mcp_tools: OrderedDict[str, ActivatedMCPTool] = OrderedDict()
        self.mcp_tool_exposure: MCPToolExposureManager | None = None
        if deferred_mcp_loading_enabled:
            catalog = get_mcp_tool_catalog()
            self.mcp_tool_exposure = MCPToolExposureManager(
                catalog,
                self.activated_mcp_tools,
            )
            self.tools["tool_search"] = ToolSearchTool(
                catalog,
                self.activated_mcp_tools,
                protected_names_provider=lambda: frozenset(
                    build_tool_name_index(self.tools.values())
                ),
            )
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        workspace_note = (
            "\n\n## Current Workspace\n"
            f"You are currently working in: `{self.workspace_dir.resolve()}`\n"
            "This directory is the session workspace and default working root; "
            "it does not by itself define every path the runtime may allow. "
            "Relative tool paths resolve from each tool's active project/artifact root."
        )
        self.system_prompt = (
            system_prompt
            if "Current Workspace" in system_prompt
            else system_prompt + workspace_note
        )
        if self.mcp_tool_exposure is not None:
            self.system_prompt = (
                f"{self.system_prompt.rstrip()}\n\n## Deferred MCP tools\n"
                "Use `tool_search` when the visible tools do not cover the task. "
                "Only returned matches and always-load tools become visible on "
                "the next model step; other catalog tools remain hidden."
            )
        self.messages = [Message(role="system", content=self.system_prompt)]
        self._goal_store = LegacyGoalStore()
        self._goal_session_id = "compat-agent"
        self.tools["goal_read"] = GoalReadTool(
            self._goal_store, session_id=self._goal_session_id
        )
        self.tools["goal_write"] = GoalWriteTool(
            self._goal_store, session_id=self._goal_session_id
        )
        self._hooks = hooks
        self._permission_negotiator: Any | None = None
        self._proposal_negotiator: Any | None = None
        self._memory_extractor: Any | None = None
        self.thinking_enabled = thinking_enabled
        self.memory_promotion_enabled = memory_promotion_enabled
        self.memory_promotion_hit_threshold = memory_promotion_hit_threshold
        self.memory_promotion_cooldown_days = memory_promotion_cooldown_days
        self.cancel_event: asyncio.Event | None = None
        self.inject_queue: asyncio.Queue[Any] = asyncio.Queue()
        self.logger = AgentLogger()
        self.api_total_tokens = 0
        self.last_stop_reason: str | None = None
        self.last_checkpoint: dict[str, Any] | None = None
        self.cache_fingerprint_context: dict[str, Any] = {}
        self.context_resource_dedup_enabled = context_resource_dedup_enabled
        self.context_resource_ledger = ContextResourceLedger()
        self.tool_result_storage = ToolResultStorage(Path.home() / ".box-agent" / "sessions")
        self._active_skill_prompts: dict[str, str] = {}
        self._active_skill_hashes: dict[str, str] = {}
        self._active_skill_order: list[str] = []
        for tool in self.tools.values():
            setter = getattr(tool, "set_tool_provider", None)
            if callable(setter):
                setter(self._inherited_tools)
            prompt_setter = getattr(tool, "set_parent_system_prompt", None)
            if callable(prompt_setter):
                prompt_setter(self.system_prompt)

    @property
    def goal(self) -> GoalState | None:
        return self._goal_store.compatibility_state(self._goal_session_id)

    def _inherited_tools(self) -> dict[str, Tool]:
        if self.mcp_tool_exposure is None:
            return self.tools
        return self.mcp_tool_exposure.inherited_tools(self.tools)

    @goal.setter
    def goal(self, value: GoalState | None) -> None:
        self._goal_store.set_compatibility_state(self._goal_session_id, value)

    def set_goal(self, objective: str, **values: Any) -> GoalState:
        self._goal_store.set(self._goal_session_id, objective, **values)
        return self.goal  # type: ignore[return-value]

    def pause_goal(self) -> GoalState | None:
        self._goal_store.pause(self._goal_session_id)
        return self.goal

    def resume_goal(self) -> GoalState | None:
        self._goal_store.resume(self._goal_session_id)
        return self.goal

    def complete_goal(self, **values: Any) -> GoalState | None:
        self._goal_store.complete(self._goal_session_id, **values)
        return self.goal

    def update_goal_progress(self, progress: object, **values: Any) -> GoalState | None:
        self._goal_store.progress(self._goal_session_id, progress, **values)
        return self.goal

    def block_goal(self, blocked_reason: str, **values: Any) -> GoalState | None:
        self._goal_store.block(self._goal_session_id, blocked_reason, **values)
        return self.goal

    def clear_goal(self) -> GoalState | None:
        old = self.goal
        self._goal_store.clear(self._goal_session_id)
        return old

    def restore_goal(self, payload: object) -> GoalState | None:
        self._goal_store.restore(self._goal_session_id, payload)
        return self.goal

    def add_user_message(self, content: str) -> None:
        self.messages.append(Message(role="user", content=content))

    def inject(self, content: Any) -> None:
        self.inject_queue.put_nowait(content)

    def seed_continuation_messages(self, messages: tuple[ContinuationMessage, ...]) -> int:
        if len(self.messages) != 1:
            return 0
        self.messages.extend(Message(role=item.role, content=item.content) for item in messages)
        return len(messages)

    def set_permission_negotiator(self, value: Any | None) -> None:
        self._permission_negotiator = value

    def set_memory_extractor(self, value: Any | None) -> None:
        self._memory_extractor = value

    def set_memory_proposal_negotiator(self, value: Any | None) -> None:
        self._proposal_negotiator = value

    def set_system_prompt(self, prompt: str) -> None:
        self.system_prompt = build_active_skills_prompt(prompt, self._active_skill_prompts)
        self.messages[0] = Message(role="system", content=self.system_prompt)

    def activate_skill_instructions(self, name: str, prompt: str) -> None:
        name = name.strip()
        if not name or not prompt.strip():
            return
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if self._active_skill_hashes.get(name) == digest:
            return
        self._active_skill_prompts[name] = prompt
        self._active_skill_hashes[name] = digest
        if name not in self._active_skill_order:
            self._active_skill_order.append(name)
        self.set_system_prompt(self.system_prompt)

    def deactivate_skill_instructions(self, name: str) -> bool:
        if name not in self._active_skill_prompts:
            return False
        self._active_skill_prompts.pop(name)
        self._active_skill_hashes.pop(name, None)
        self._active_skill_order = [item for item in self._active_skill_order if item != name]
        self.set_system_prompt(self.system_prompt)
        return True

    def clear_active_skill_instructions(self) -> None:
        self._active_skill_prompts.clear()
        self._active_skill_hashes.clear()
        self._active_skill_order.clear()
        self.set_system_prompt(self.system_prompt)

    def active_skill_diagnostics(self) -> dict[str, object]:
        names = tuple(name for name in self._active_skill_order if name in self._active_skill_prompts)
        tokens = sum(max(1, len(self._active_skill_prompts[name]) // 4) for name in names)
        return {"names": names, "hashes": tuple((name, self._active_skill_hashes[name]) for name in names), "estimated_tokens": tokens, "token_budget": _ACTIVE_SKILL_TOKEN_BUDGET, "budget_exceeded": tokens > _ACTIVE_SKILL_TOKEN_BUDGET}

    def clear_history(self) -> int:
        removed = max(0, len(self.messages) - 1)
        del self.messages[1:]
        self.context_resource_ledger.rotate_epoch()
        return removed

    def get_history(self) -> list[Message]:
        return self.messages.copy()

    def _check_cancelled(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())

    def default_run_options(self) -> AgentRunOptions:
        return AgentRunOptions(
            llm=self.llm,
            is_cancelled=self._check_cancelled,
            logger=self.logger,
            permission_negotiator=self._permission_negotiator,
            hooks=self._hooks,
            memory_manager=getattr(self._memory_extractor, "_mgr", None),
            memory_extractor=self._memory_extractor,
            inject_queue=self.inject_queue,
            cache_fingerprint_context=self.cache_fingerprint_context,
        )

    async def run_events(
        self,
        cancel_event: asyncio.Event | None = None,
        *,
        options: AgentRunOptions | None = None,
        **overrides: Any,
    ) -> AsyncIterator[AgentEvent]:
        effective = options or self.default_run_options()
        if cancel_event is not None:
            self.cancel_event = cancel_event
            effective = replace(effective, is_cancelled=self._check_cancelled)
        values = {name: value for name, value in overrides.items() if value is not None}
        if values:
            effective = replace(effective, **values)
        sub_agent = self.tools.get("sub_agent")
        bind_permission = getattr(sub_agent, "set_permission_negotiator", None)
        if callable(bind_permission):
            bind_permission(effective.permission_negotiator)
        workflow_policy = effective.workflow_policy or GoalWorkflowPolicy(
            self._goal_store,
            session_id=self._goal_session_id,
        )
        async for event in run_agent_loop(
            llm=effective.llm,
            summary_llm=effective.summary_llm,
            messages=self.messages,
            tools=self.tools,
            tool_limits=self.tool_limits,
            max_steps=self.max_steps,
            max_tool_calls=effective.max_tool_calls,
            token_limit=self.token_limit,
            is_cancelled=effective.is_cancelled,
            workspace_dir=str(self.workspace_dir),
            permission_negotiator=effective.permission_negotiator,
            hooks=effective.hooks,
            memory_manager=effective.memory_manager,
            memory_extractor=effective.memory_extractor,
            memory_turn_id=effective.memory_turn_id,
            inject_queue=effective.inject_queue,
            thinking_enabled=self.thinking_enabled,
            session_id=effective.session_id or self._goal_session_id,
            turn_id=effective.turn_id,
            title=effective.title,
            max_parallel_tools=self.max_parallel_tools,
            provider_stale_seconds=self.provider_stale_seconds,
            truncation_continuation_enabled=self.truncation_continuation_enabled,
            max_truncation_continuations=self.max_truncation_continuations,
            max_truncated_tool_call_retries=self.max_truncated_tool_call_retries,
            completion_gate=effective.completion_gate,
            force_plan_start=effective.force_plan_start,
            require_plan_approval=effective.require_plan_approval,
            plan_approval=effective.plan_approval,
            plan_start_text=effective.plan_start_text,
            pause_after_plan_write=effective.pause_after_plan_write,
            web_search_total_limit=effective.web_search_total_limit,
            no_progress_limit=effective.no_progress_limit,
            artifact_detection_enabled=effective.artifact_detection_enabled,
            artifact_root_dir=effective.artifact_root_dir,
            cache_fingerprint_context=effective.cache_fingerprint_context,
            cache_fingerprint_sink=effective.cache_fingerprint_sink,
            active_skill_activator=self.activate_skill_instructions,
            workflow_policy=workflow_policy,
            current_turn_text=effective.current_turn_text,
            context_resource_ledger=self.context_resource_ledger,
            context_resource_dedup_enabled=self.context_resource_dedup_enabled,
            tool_exposure_manager=self.mcp_tool_exposure,
        ):
            if isinstance(event, TokenUsageEvent):
                self.api_total_tokens = event.total_tokens
            yield event

    async def run(self, cancel_event: asyncio.Event | None = None, **options: Any) -> str:
        final = ""
        self.last_stop_reason = None
        self.last_checkpoint = None
        async for event in self.run_events(cancel_event, **options):
            self._render_event(event)
            if isinstance(event, MemoryProposalEvent) and self._proposal_negotiator is not None:
                value = self._proposal_negotiator.negotiate(event)
                if hasattr(value, "__await__"):
                    await value
            if isinstance(event, DoneEvent):
                final = event.final_content
                self.last_stop_reason = event.stop_reason.value
            elif isinstance(event, ContextCheckpointEvent):
                self.last_checkpoint = {"checkpointId": event.checkpoint_id, "workflowKind": event.workflow_kind, "stage": event.stage, "path": event.path}
        return final

    @staticmethod
    def _render_event(event: AgentEvent) -> None:
        if isinstance(event, ThinkingEvent) and event.content:
            print(event.content, end="" if event._streaming else "\n", flush=True)
        elif isinstance(event, ContentEvent) and event.content:
            print(event.content, end="" if event._streaming else "\n", flush=True)


def should_continue_goal_autopilot(agent: Agent, stop_reason: str | None) -> bool:
    return _continue_goal(agent.goal, stop_reason)


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
