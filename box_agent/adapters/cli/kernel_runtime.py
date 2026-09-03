"""Persistent interactive CLI host for the plugin-composed Agent Kernel."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import uuid4

from box_agent.adapters.plugin_host import build_plugin_host
from box_agent.api import Message, RunOptions, RunRequest, SessionOpenRequest
from box_agent.kernel import PluginKernelComposer
from box_agent.kernel.model_stream import resolve_provider_stale_seconds
from box_agent.persistence import (
    SQLiteEffectLedger,
    SQLiteEventLog,
    SQLiteLeaseStore,
    SQLiteSessionStore,
)
from box_agent.plugins.builtins import (
    build_builtin_workflow_bundle,
    register_builtin_context_contributors,
    register_builtin_memory_extraction_hook,
    register_builtin_workflow_bundle,
)
from box_agent.services.kernel import KernelAgentService
from box_agent.tools.registration import MCPToolRegistryController
from box_agent.workflows import (
    GoalStore,
    SessionPlanStore,
    completion_gate_to_payload,
    goal_payload,
    workflow_selector_from_registry,
)


class KernelCLIConversation:
    """Own one durable CLI Session across an arbitrary number of turns."""

    @classmethod
    async def create(
        cls,
        *,
        workspace_dir: Path,
        config: Any,
        llm: Any,
        tools: dict[str, Any],
        system_prompt: str,
        memory_manager: Any | None,
        permission_negotiator: Any | None,
        skill_loader: Any | None,
        initial_goal: Any | None,
        goal_autopilot_enabled: bool,
        hooks: Iterable[Any] = (),
    ) -> "KernelCLIConversation":
        self = cls()
        self.workspace_dir = workspace_dir
        self.config = config
        self.system_prompt = system_prompt
        self.session_id = f"cli-{uuid4().hex}"
        self.turn_count = 0
        self.api_total_tokens = 0
        self.last_result: Any | None = None
        self.goal_autopilot_enabled = bool(goal_autopilot_enabled)
        self.goal_store = GoalStore()
        if initial_goal is not None:
            self.goal_store.restore(self.session_id, goal_payload(initial_goal))
        self.plan_store = SessionPlanStore()

        workflow_bundle = build_builtin_workflow_bundle(
            config=config,
            tools=tuple(tools.values()),
            goal_store=self.goal_store,
            plan_store=self.plan_store,
            goal_autopilot_enabled=goal_autopilot_enabled,
            include_attachment_policy=False,
        )
        native_tools = {tool.name: tool for tool in workflow_bundle.tools}
        self.host = build_plugin_host(
            llm=llm,
            tools=tuple(native_tools.values()),
            memory_manager=memory_manager,
            permission_negotiator=permission_negotiator,
            hooks=tuple(hooks),
            workflow_policy=workflow_bundle.default_policy,
        )
        if bool(getattr(config.agent, "enable_memory_extraction", False)):
            register_builtin_memory_extraction_hook(
                self.host,
                llm=llm,
                memory_manager=memory_manager,
                cooldown=config.agent.memory_extraction_cooldown,
                step_interval=config.agent.memory_extraction_step_interval,
            )
        register_builtin_context_contributors(self.host, skill_loader=skill_loader)
        register_builtin_workflow_bundle(
            self.host,
            workflow_bundle,
            workspace=workspace_dir,
            skill_loader=skill_loader,
            tool_limits=config.tool_limits,
        )
        self.selector = workflow_selector_from_registry(self.host)
        self.mcp_tools = MCPToolRegistryController(self.host)
        self._mcp_server_names = {
            str(getattr(tool, "server_name", "") or "").strip()
            for tool in native_tools.values()
            if str(getattr(tool, "server_name", "") or "").strip()
        }

        db_path = workspace_dir / ".box-agent" / "runs.sqlite3"
        self.stores = (
            SQLiteEventLog(db_path),
            SQLiteEffectLedger(db_path),
            SQLiteSessionStore(db_path),
            SQLiteLeaseStore(db_path),
        )
        event_log, effect_ledger, session_store, lease_store = self.stores
        composer = PluginKernelComposer(self.host, effect_ledger=effect_ledger)
        self.service = KernelAgentService(
            kernel_factory=composer.build,
            event_log=event_log,
            session_store=session_store,
            lease_store=lease_store,
            owner_id=f"cli-worker-{uuid4().hex}",
            plugin_lock_provider=self.host.lock_snapshot,
            plugin_snapshot_provider=self.host.snapshot,
            plugin_restore_provider=self.host.restore_snapshot,
        )
        await self._open_session()
        return self

    async def _open_session(self) -> None:
        await self.service.open_session(
            SessionOpenRequest(
                session_id=self.session_id,
                metadata={"workspace": str(self.workspace_dir), "runtime": "kernel"},
            )
        )

    async def reset_session(self, goal: Any | None = None) -> None:
        self.session_id = f"cli-{uuid4().hex}"
        self.turn_count = 0
        if goal is not None:
            self.goal_store.restore(self.session_id, goal_payload(goal))
        await self._open_session()

    def sync_goal(self, goal: Any | None) -> None:
        self.goal_store.restore(self.session_id, goal_payload(goal))

    @property
    def goal(self) -> Any | None:
        return self.current_goal()

    def current_goal(self) -> Any | None:
        return self.goal_store.get(self.session_id)

    def set_goal(self, objective: str) -> Any:
        return self.goal_store.set(self.session_id, objective)

    def pause_goal(self) -> Any | None:
        return self.goal_store.pause(self.session_id)

    def resume_goal(self) -> Any | None:
        return self.goal_store.resume(self.session_id)

    def update_goal_progress(self, values: object) -> Any | None:
        return self.goal_store.progress(self.session_id, values)

    def block_goal(self, reason: str) -> Any | None:
        return self.goal_store.block(self.session_id, reason)

    def clear_goal(self) -> Any | None:
        return self.goal_store.clear(self.session_id)

    def complete_goal(
        self,
        *,
        evidence: object = None,
        progress: object = None,
        completed_by: str | None = None,
    ) -> Any | None:
        return self.goal_store.complete(
            self.session_id,
            evidence=evidence,
            progress=progress,
            completed_by=completed_by,
        )

    async def start_turn(
        self,
        text: str,
        *,
        completion_gate: Any | None,
        force_plan_start: bool,
        thinking_enabled: bool,
    ) -> Any:
        selection = self._selection(text, completion_gate, force_plan_start)
        workflow_id = selection.get("workflow_id") if selection else None
        workflow_options = (
            dict(selection.get("workflow_options", {})) if selection else {}
        )
        workflow_options.update(
            {
                "autopilot_enabled": bool(
                    self.goal_autopilot_enabled
                    and self.config.agent.goal_autopilot_enabled
                ),
                "max_continuations": self.config.agent.goal_autopilot_max_turns,
                "no_progress_turns": self.config.agent.goal_autopilot_no_progress_turns,
                "max_seconds": self.config.agent.goal_autopilot_max_seconds,
                "plan_start_text": text,
                "pause_after_plan_write": bool(
                    force_plan_start or _requests_plan(text)
                ),
            }
        )
        self.turn_count += 1
        return await self.service.start(
            RunRequest(
                request_id=uuid4().hex,
                session_id=self.session_id,
                turn_id=uuid4().hex,
                user_input=Message.user(text),
                options=RunOptions(
                    max_steps=self.config.agent.max_steps,
                    provider_stale_seconds=resolve_provider_stale_seconds(
                        self.config.agent.provider_stale_seconds
                    ),
                    truncation_continuation_enabled=(
                        self.config.agent.retry_on_suspected_truncation
                    ),
                    max_truncation_continuations=(
                        self.config.agent.max_truncation_continuations
                    ),
                    max_truncated_tool_call_retries=(
                        self.config.agent.max_truncated_tool_call_retries
                    ),
                    max_tool_calls=(
                        completion_gate.max_tool_calls
                        if completion_gate is not None
                        else None
                    ),
                    max_parallel_tools=self.config.agent.max_parallel_tools,
                    thinking_enabled=thinking_enabled,
                    workflow_id=workflow_id,
                    workflow_options=workflow_options,
                    context_budget=self.config.llm.context_token_limit,
                ),
                metadata={
                    "system_prompt": self.system_prompt,
                    "workspace_dir": str(self.workspace_dir),
                    "artifact_root_dir": str(self.workspace_dir / "output"),
                },
            )
        )

    def record_result(self, result: Any) -> None:
        self.last_result = result
        usage = getattr(result, "usage", None)
        self.api_total_tokens += int(getattr(usage, "total_tokens", 0) or 0)

    @property
    def tool_count(self) -> int:
        registry = self.host.registries["tools.executors"]
        return len(
            {
                id(value)
                for registration in registry.registrations()
                if (value := registry.get(
                    registration.key,
                    scope=registration.scope,
                )) is not None
            }
        )

    def _selection(
        self,
        text: str,
        completion_gate: Any | None,
        force_plan_start: bool,
    ) -> dict[str, Any] | None:
        if completion_gate is not None:
            kind = getattr(completion_gate, "workflow_checkpoint_kind", None)
            if kind in {"controlled_presentation", "external_skill"}:
                return {
                    "workflow_id": kind,
                    "workflow_options": dict(completion_gate.workflow_options),
                }
            return {
                "workflow_id": "completion_gate",
                "workflow_options": {
                    "completion_gate": completion_gate_to_payload(completion_gate)
                },
            }
        selected = self.selector(text, {})
        if selected is not None:
            return dict(selected)
        return None

    async def replace_mcp_server(self, name: str, tools: tuple[Any, ...]) -> None:
        await self.mcp_tools.replace_server(name, tools)
        if tools:
            self._mcp_server_names.add(name)
        else:
            self._mcp_server_names.discard(name)

    async def replace_mcp_catalog(self, tools: Iterable[Any]) -> None:
        """Publish a complete connected-server snapshot between Runs."""

        grouped: dict[str, list[Any]] = {}
        for tool in tools:
            server_name = str(getattr(tool, "server_name", "") or "").strip()
            if not server_name:
                raise ValueError("MCP catalog contains a tool without server ownership")
            grouped.setdefault(server_name, []).append(tool)
        for server_name in sorted(self._mcp_server_names | set(grouped)):
            await self.replace_mcp_server(
                server_name,
                tuple(grouped.get(server_name, ())),
            )

    async def close(self) -> None:
        for store in self.stores:
            store.shutdown()


__all__ = ["KernelCLIConversation"]


def _requests_plan(text: str) -> bool:
    # Imported lazily to keep the CLI adapter import boundary independent from
    # the compatibility CLI module that also re-exports this predicate.
    from box_agent.workflows import text_requests_native_plan

    return text_requests_native_plan(text)
