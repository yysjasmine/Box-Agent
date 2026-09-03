"""Historical call-shape adapter backed exclusively by the Agent Kernel.

Scheduling, permissions, Tool execution, memory, and workflows remain owned by
the same PluginHost and Kernel used by ACP, CLI, and SDK hosts.  This module
only translates legacy arguments and events at the public compatibility edge.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from uuid import uuid4

from box_agent.adapters.plugin_host import PermissionNegotiatorGateway, build_plugin_host
from box_agent.api import (
    ContextBuildRequest,
    ContextBuildResult,
    ContextItem,
    ControlCommand,
    Message as KernelMessage,
    RunOptions,
    RunRequest,
    ToolCallRequest,
)
from box_agent.compat.events import (
    AgentEvent as LegacyAgentEvent,
    ArtifactEvent,
    ContentEvent,
    ContextCheckpointEvent,
    DoneEvent,
    ErrorEvent,
    InjectedMessageEvent,
    LLMActivityEvent,
    LLMOutputEvent,
    PermissionRequestEvent,
    PlanSnapshotEvent,
    StepEnd,
    StepStart,
    StopReason,
    SummarizationEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolCallResult as LegacyToolCallResult,
    ToolCallStart,
    WebSearchEvent,
)
from box_agent.kernel import PluginKernelComposer
from box_agent.kernel.model_stream import resolve_provider_stale_seconds
from box_agent.observability import AgentLoggerHook
from box_agent.schema import FunctionCall, ToolCall
from box_agent.context import InMemoryContextEngine
from box_agent.workflows.guards import (
    CompletionGate,
    format_injected_message,
    format_runtime_context_update,
)
from box_agent.plugins import PluginHost
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.engine import RegistryToolEngine
from box_agent.tools.mcp_exposure_engine import MCPToolExposureEngine
from box_agent.tools.model_tool_context import scoped_model_tool_context
from box_agent.tools.registration import register_tool_plugins
from box_agent.workflows import (
    BrowserIntentWorkflowPolicy,
    CompletionGateWorkflowPolicy,
    CompositeWorkflowPolicy,
    ResponseContinuationWorkflowPolicy,
    completion_gate_to_payload,
    create_workflow_policy,
)


class _CompatibilityHistoryContext:
    """Present mature Message history through the Context Engine SPI."""

    def __init__(self, messages: Sequence[Any]) -> None:
        self._messages = tuple(messages)
        self._compactor = InMemoryContextEngine()

    async def assemble(self, request: ContextBuildRequest) -> ContextBuildResult:
        items: list[ContextItem] = []
        for index, message in enumerate(self._messages):
            role = str(getattr(message, "role", "") or "user")
            raw = getattr(message, "content", "")
            content = (
                tuple(dict(block) for block in raw)
                if isinstance(raw, (list, tuple))
                and all(isinstance(block, Mapping) for block in raw)
                else str(raw)
            )
            items.append(
                ContextItem(
                    item_id=f"compat-history:{index}",
                    kind=role,
                    content=content,
                    priority=1000 if role == "system" else 100,
                    pinned=role == "system" or index == len(self._messages) - 1,
                    metadata={"role": role, "compatibility": True},
                )
            )
        existing = {item.item_id for item in items}
        for item in request.items:
            if (
                item.item_id not in existing
                and (
                    item.metadata.get("workflow_context")
                    or item.metadata.get("model_recovery")
                    or item.metadata.get("workflow_continuation")
                    or item.metadata.get("injected")
                    or item.kind not in {"system", "user"}
                )
            ):
                items.append(item)
        if not items:
            items.extend(request.items)
        return await self._compactor.assemble(
            ContextBuildRequest(
                items=tuple(items),
                token_budget=request.token_budget,
                session_id=request.session_id,
                run_id=request.run_id,
                metadata=request.metadata,
            )
        )

    async def restore(
        self, *, resource_id: str, content_version: str | None = None
    ) -> ContextItem | None:
        del resource_id, content_version
        return None


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        )
    return str(content)


def _latest_user_text(messages: Sequence[Any], fallback: str = "") -> str:
    for message in reversed(messages):
        if str(getattr(message, "role", "")) == "user":
            return _message_text(message)
    return fallback


def _activate_compatibility_skill(event: Any, kwargs: Mapping[str, Any]) -> None:
    """Persist a successfully loaded Skill at the legacy Agent boundary.

    The Kernel keeps the full Tool result in the current Run context.  The
    historical ``Agent`` additionally promised that loaded instructions remain
    active on later Runs, so its callback updates that facade's session prompt.
    This translation deliberately lives here instead of teaching the Kernel
    about the concrete ``get_skill`` Tool.
    """

    if str(getattr(event, "type", "") or "") != "tool.call.completed":
        return
    payload = getattr(event, "payload", {})
    if not isinstance(payload, Mapping) or payload.get("status") != "succeeded":
        return
    tool_name = str(payload.get("tool_name", "") or "")
    tools = kwargs.get("tools") or {}
    tool = tools.get(tool_name) if isinstance(tools, Mapping) else None
    if tool is None or not getattr(tool, "loads_active_skill_instructions", False):
        return
    arguments = payload.get("arguments")
    output = payload.get("output")
    skill_name = arguments.get("skill_name") if isinstance(arguments, Mapping) else None
    content = str(payload.get("content", "") or "")
    if (
        not isinstance(skill_name, str)
        or not skill_name.strip()
        or not content.strip()
        or (isinstance(output, Mapping) and bool(output.get("broken")))
    ):
        return
    activate = kwargs.get("active_skill_activator")
    if callable(activate):
        activate(skill_name.strip(), content)


def _workflow_from_kwargs(kwargs: Mapping[str, Any]) -> Any | None:
    policy = kwargs.get("workflow_policy")
    gate = kwargs.get("completion_gate")
    if policy is None:
        policy = create_workflow_policy(
            workflow_kind=getattr(gate, "workflow_checkpoint_kind", None),
            workspace_dir=kwargs.get("workspace_dir"),
            artifact_root_dir=kwargs.get("artifact_root_dir"),
            workflow_options=getattr(gate, "workflow_options", None),
            available_tool_names=frozenset(
                str(name) for name in (kwargs.get("tools") or {})
            ),
            skill_loader=kwargs.get("skill_loader"),
        )
    browser = BrowserIntentWorkflowPolicy().for_run(
        type(
            "CompatibilityBrowserRequest",
            (),
            {
                "user_input": KernelMessage.user(
                    str(kwargs.get("current_turn_text") or _latest_user_text(kwargs.get("messages") or ()))
                )
            },
        )()
    )
    gate_policy = (
        CompletionGateWorkflowPolicy(
            gate, workspace_dir=kwargs.get("workspace_dir")
        )
        if gate is not None
        else None
    )
    response_continuation = ResponseContinuationWorkflowPolicy(
        enabled=bool(kwargs.get("truncation_continuation_enabled", True)),
        max_continuations=max(
            0, int(kwargs.get("max_truncation_continuations", 3) or 0)
        ),
    )
    return CompositeWorkflowPolicy(
        (browser, gate_policy, policy, response_continuation)
    )


def _run_options(kwargs: Mapping[str, Any]) -> RunOptions:
    gate = kwargs.get("completion_gate")
    workflow_options: dict[str, Any] = {}
    if gate is not None:
        workflow_options["completion_gate"] = completion_gate_to_payload(gate)
    for key in (
        "force_plan_start",
        "require_plan_approval",
        "pause_after_plan_write",
    ):
        if kwargs.get(key):
            workflow_options[key] = True
    if isinstance(kwargs.get("plan_approval"), Mapping):
        workflow_options["plan_approval"] = dict(kwargs["plan_approval"])
    limit = kwargs.get("max_tool_calls")
    return RunOptions(
        max_steps=max(1, int(kwargs.get("max_steps") or 8)),
        provider_stale_seconds=resolve_provider_stale_seconds(
            kwargs.get("provider_stale_seconds")
        ),
        truncation_continuation_enabled=bool(
            kwargs.get("truncation_continuation_enabled", True)
        ),
        max_truncation_continuations=max(
            0, int(kwargs.get("max_truncation_continuations", 3) or 0)
        ),
        max_truncated_tool_call_retries=max(
            0, int(kwargs.get("max_truncated_tool_call_retries", 3) or 0)
        ),
        max_tool_calls=(
            int(limit) if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else None
        ),
        max_parallel_tools=max(1, int(kwargs.get("max_parallel_tools") or 8)),
        thinking_enabled=bool(kwargs.get("thinking_enabled", False)),
        workflow_options=workflow_options,
        context_budget=max(1, int(kwargs.get("token_limit") or 100_000)),
    )


async def _control_stream(
    queue: asyncio.Queue[Any], *, session_id: str, run_id: str
) -> AsyncIterator[ControlCommand]:
    while True:
        value = await queue.get()
        if isinstance(value, Mapping):
            content = str(value.get("content", ""))
            injection_id = str(value.get("injection_id", value.get("id", "")))
            user_visible = bool(value.get("user_visible", True))
            source = str(value.get("source", "user") or "user")
        else:
            content, injection_id = str(value), ""
            user_visible, source = True, "user"
        yield ControlCommand(
            command_id=uuid4().hex,
            session_id=session_id,
            run_id=run_id,
            kind="run.inject",
            payload={
                "text": content,
                "injection_id": injection_id or uuid4().hex,
                "user_visible": user_visible,
                "source": source,
            },
            source="compat.runtime",
        )


def _stop_reason(value: str) -> StopReason:
    return {
        "stop": StopReason.END_TURN,
        "completed": StopReason.END_TURN,
        "end_turn": StopReason.END_TURN,
        "max_steps": StopReason.MAX_STEPS,
        "max_tokens": StopReason.MAX_TOKENS,
        "cancelled": StopReason.CANCELLED,
        "checkpoint_paused": StopReason.CHECKPOINT_PAUSED,
        "error": StopReason.ERROR,
        "model_error": StopReason.ERROR,
        "error": StopReason.ERROR,
    }.get(str(value or "").lower(), StopReason.END_TURN)


def _legacy_permission_decision(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    metadata = value.get("metadata")
    result = dict(metadata) if isinstance(metadata, Mapping) else {}
    granted = bool(value.get("granted", False))
    result.setdefault("type", "policy_decision")
    result.setdefault("decision", "approved" if granted else "denied")
    result.setdefault("retry_count", 1 if granted else 0)
    result["granted"] = granted
    if value.get("reason"):
        result.setdefault("reason", value["reason"])
    return result


def _legacy_events(event: Any) -> tuple[LegacyAgentEvent, ...]:
    event_type = str(getattr(event, "type", ""))
    payload = dict(getattr(event, "payload", {}) or {})
    if event_type == "step.started":
        return (StepStart(int(payload.get("step", 0)), int(payload.get("max_steps", 0))),)
    if event_type == "step.completed":
        return (StepEnd(int(payload.get("step", 0)), float(payload.get("elapsed_seconds", 0)), float(payload.get("total_elapsed_seconds", 0))),)
    if event_type == "model.thinking.delta":
        return (ThinkingEvent(str(payload.get("content", payload.get("delta", ""))), True),)
    if event_type == "model.content.delta":
        return (ContentEvent(str(payload.get("content", payload.get("delta", ""))), True),)
    if event_type == "model.activity":
        return (LLMActivityEvent(int(payload.get("step", 0)), payload),)
    if event_type == "model.response.completed":
        usage = payload.get("usage")
        values: list[LegacyAgentEvent] = [
            LLMOutputEvent(
                step=int(payload.get("step", 0)),
                content=str(payload.get("content", "")),
                thinking=str(payload.get("thinking", "")) or None,
                tool_calls=list(payload.get("tool_calls", ()) or ()),
                finish_reason=str(payload.get("finish_reason", "stop")),
                usage=dict(usage) if isinstance(usage, Mapping) else None,
                provider_request_id=payload.get("provider_request_id"),
            )
        ]
        if isinstance(usage, Mapping):
            values.append(TokenUsageEvent(int(usage.get("total_tokens", 0) or 0)))
        return tuple(values)
    if event_type == "model.usage":
        return (TokenUsageEvent(int(payload.get("total_tokens", 0) or 0)),)
    if event_type == "context.compacted":
        metadata = payload.get("metadata")
        estimated_before = (
            metadata.get("estimated_before_tokens", payload.get("estimated_tokens", 0))
            if isinstance(metadata, Mapping)
            else payload.get("estimated_tokens", 0)
        )
        return (
            SummarizationEvent(
                estimated_tokens=int(estimated_before or 0),
                api_tokens=0,
                token_limit=int(payload.get("token_budget", 0) or 0),
                estimated_after=int(payload.get("estimated_tokens", 0) or 0),
                mode="priority",
                summary_calls=0,
            ),
        )
    if event_type == "tool.call.requested":
        meta = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
        return (ToolCallStart(str(payload.get("call_id", "")), str(payload.get("tool_name", "")), dict(payload.get("arguments", {}) or {}), tool_id=str(meta.get("tool_id", "")) or None, server_name=str(meta.get("mcp_server", "")) or None),)
    if event_type == "tool.call.completed":
        error = payload.get("error")
        error_text = str(error.get("message", "")) if isinstance(error, Mapping) else str(error or "")
        output = payload.get("output")
        meta = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
        result_event = LegacyToolCallResult(
            str(payload.get("call_id", "")),
            str(payload.get("tool_name", "")),
            payload.get("status") == "succeeded",
            str(payload.get("content", "")),
            error_text or None,
            dict(output) if isinstance(output, Mapping) else None,
            user_visible=bool(
                payload.get(
                    "user_visible",
                    not (
                        isinstance(error, Mapping)
                        and error.get("category") == "workflow"
                    ),
                )
            ),
            policy_decision=_legacy_permission_decision(
                payload.get("permission_decision")
            ),
            tool_id=str(meta.get("tool_id", "")) or None,
            server_name=str(meta.get("mcp_server", "")) or None,
        )
        search_payload = dict(output) if isinstance(output, Mapping) else None
        if payload.get("tool_name") == "web_search" and search_payload is None:
            try:
                decoded = json.loads(str(payload.get("content", "")))
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, Mapping):
                search_payload = dict(decoded)
        if search_payload is not None and (
            payload.get("tool_name") == "web_search" or "refs" in search_payload
        ):
            return (
                result_event,
                WebSearchEvent(str(payload.get("call_id", "")), search_payload),
            )
        return (result_event,)
    if event_type == "artifact.created":
        return (ArtifactEvent(str(payload.get("call_id", "")), str(payload.get("kind", "file")), str(payload.get("filename", "")), str(payload.get("rel_path", "")), str(payload.get("abs_path", "")), str(payload.get("uri", "")), str(payload.get("mime", "application/octet-stream")), int(payload.get("size", -1) or -1), str(payload.get("sha256", "")), str(payload.get("produced_at", "")), str(payload.get("layout_id", "")), str(payload.get("edit_mode", ""))),)
    if event_type == "permission.requested":
        return (PermissionRequestEvent(str(payload.get("call_id", "")), str(payload.get("scope", "")), str(payload.get("requested_scope", "")), str(payload.get("reason", "")), path=str(payload.get("path", payload.get("resource", ""))), command=str(payload.get("command", "")), risk=str(payload.get("risk", ""))),)
    if event_type == "run.injected":
        return (
            InjectedMessageEvent(
                str(payload.get("text", "")),
                str(payload.get("injection_id", "")) or None,
                user_visible=bool(payload.get("user_visible", True)),
            ),
        )
    if event_type == "model.recovery.requested":
        return (
            InjectedMessageEvent(
                str(payload.get("message", "") or ""),
                None,
                user_visible=False,
            ),
        )
    if event_type == "workflow.continuation.requested":
        message = payload.get("message")
        content = (
            str(message.get("content", ""))
            if isinstance(message, Mapping)
            else str(message or "")
        )
        return (
            InjectedMessageEvent(
                content,
                str(payload.get("continuation_id", "")) or None,
                user_visible=bool(
                    message.get("metadata", {}).get("user_visible", False)
                    if isinstance(message, Mapping)
                    and isinstance(message.get("metadata"), Mapping)
                    else False
                ),
            ),
        )
    if event_type == "workflow.plan.snapshot":
        return (PlanSnapshotEvent(payload),)
    if event_type == "workflow.checkpoint":
        return (ContextCheckpointEvent(str(payload.get("checkpoint_id", event.event_id)), str(payload.get("workflow_kind", payload.get("workflow", ""))), str(payload.get("adapter_id", "kernel")), int(payload.get("schema_version", 1) or 1), str(payload.get("workspace_identity", "")), str(payload.get("path", "")), str(payload.get("stage", "")) or None, int(payload.get("artifact_count", 0) or 0), str(payload.get("artifact_set_sha256", ""))),)
    if event_type == "run.failed":
        error = payload.get("error")
        message = str(error.get("message", "run failed")) if isinstance(error, Mapping) else str(error or "run failed")
        failed_reason = _stop_reason(str(payload.get("stop_reason", "error")))
        if failed_reason == StopReason.END_TURN:
            failed_reason = StopReason.ERROR
        return (ErrorEvent(message, True, error_code=error.get("code") if isinstance(error, Mapping) else None, error_category=error.get("category") if isinstance(error, Mapping) else None, error_details=error.get("details") if isinstance(error, Mapping) else None), DoneEvent(failed_reason, message))
    if event_type in {"run.completed", "run.cancelled"}:
        reason = str(payload.get("stop_reason", "cancelled" if event_type == "run.cancelled" else "end_turn"))
        return (DoneEvent(_stop_reason(reason), str(payload.get("final_content", payload.get("content", "")))),)
    return ()


def run_agent_loop(**kwargs: Any) -> AsyncIterator[LegacyAgentEvent]:
    """Translate the mature call shape into one plugin-composed Kernel Run."""

    async def execute() -> AsyncIterator[LegacyAgentEvent]:
        messages = tuple(kwargs.get("messages") or ())
        llm = kwargs.get("llm")
        if llm is None:
            raise ValueError("llm is required")
        raw_tools = kwargs.get("tools") or {}
        tools = tuple(raw_tools.values()) if isinstance(raw_tools, Mapping) else tuple(raw_tools)
        tool_exposure_manager = kwargs.get("tool_exposure_manager")
        exposed_tools = tools
        if tool_exposure_manager is not None:
            exposed_tools = tuple(
                tool_exposure_manager.prepare_tools(list(tools)).tools
            )
        policy = _workflow_from_kwargs(kwargs)
        configured_hooks = list(kwargs.get("hooks") or ())
        if kwargs.get("logger") is not None:
            configured_hooks.append(
                AgentLoggerHook(
                    kwargs["logger"],
                    cache_fingerprint_context=kwargs.get(
                        "cache_fingerprint_context"
                    ),
                    cache_fingerprint_sink=kwargs.get("cache_fingerprint_sink"),
                )
            )
        host = build_plugin_host(
            llm=llm,
            tools=exposed_tools,
            memory_manager=kwargs.get("memory_manager"),
            permission_negotiator=kwargs.get("permission_negotiator"),
            context_engine=_CompatibilityHistoryContext(messages),
            hooks=tuple(configured_hooks),
            workflow_policy=policy,
        )
        if tool_exposure_manager is not None:
            permission = host.registries["permission.policies"].resolve(
                "default", scope="run"
            )
            hooks = host.registries["hooks"].resolve_all(scope="run")
            host.registries["tools.engines"].register(
                "default",
                MCPToolExposureEngine(
                    tools,
                    tool_exposure_manager,
                    permission_gateway=permission,
                    hooks=hooks,
                ),
                source="box_agent.compat.mcp-exposure",
            )
        session_id = str(kwargs.get("session_id") or f"compat-{uuid4().hex}")
        run_id, turn_id = uuid4().hex, str(kwargs.get("turn_id") or uuid4().hex)
        request = RunRequest(
            request_id=uuid4().hex,
            session_id=session_id,
            turn_id=turn_id,
            user_input=KernelMessage.user(_latest_user_text(messages, str(kwargs.get("current_turn_text") or ""))),
            options=_run_options(kwargs),
            metadata={
                "run_id": run_id,
                "system_prompt": _message_text(messages[0]) if messages else "",
                "workspace_dir": str(kwargs.get("workspace_dir") or "./workspace"),
                "artifact_root_dir": str(kwargs.get("artifact_root_dir") or ""),
                "title": str(kwargs.get("title") or ""),
            },
        )
        kernel = PluginKernelComposer(host).build(request)
        queue: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()
        results: list[Any] = []

        async def drive() -> None:
            try:
                results.append(await kernel.run(
                    request,
                    emit=queue.put,
                    cancel_event=kwargs.get("is_cancelled"),
                    controls=(
                        _control_stream(kwargs["inject_queue"], session_id=session_id, run_id=run_id)
                        if isinstance(kwargs.get("inject_queue"), asyncio.Queue)
                        else None
                    ),
                ))
            finally:
                await queue.put(sentinel)

        task = asyncio.create_task(drive())
        last_model_content = ""
        last_model_thinking = ""
        try:
            while True:
                event = await queue.get()
                if event is sentinel:
                    break
                event_type = str(getattr(event, "type", "") or "")
                event_payload = getattr(event, "payload", {})
                if event_type == "model.response.completed" and isinstance(
                    event_payload, Mapping
                ):
                    last_model_content = str(event_payload.get("content", "") or "")
                    last_model_thinking = str(event_payload.get("thinking", "") or "")
                    raw_calls = event_payload.get("tool_calls", ())
                    if isinstance(raw_calls, (list, tuple)) and raw_calls:
                        target_messages = kwargs.get("messages")
                        if isinstance(target_messages, list) and target_messages:
                            legacy_calls = []
                            for raw_call in raw_calls:
                                if not isinstance(raw_call, Mapping):
                                    continue
                                legacy_calls.append(
                                    ToolCall(
                                        id=str(raw_call.get("call_id", "")),
                                        type="function",
                                        function=FunctionCall(
                                            name=str(raw_call.get("tool_name", "")),
                                            arguments=dict(
                                                raw_call.get("arguments", {}) or {}
                                            ),
                                        ),
                                    )
                                )
                            target_messages.append(
                                type(target_messages[0])(
                                    role="assistant",
                                    content=last_model_content,
                                    thinking=last_model_thinking or None,
                                    tool_calls=legacy_calls,
                                )
                            )
                        last_model_content = ""
                        last_model_thinking = ""
                if event_type == "tool.call.completed" and isinstance(
                    event_payload, Mapping
                ):
                    target_messages = kwargs.get("messages")
                    if isinstance(target_messages, list) and target_messages:
                        error = event_payload.get("error")
                        error_text = (
                            str(error.get("message", "") or "")
                            if isinstance(error, Mapping)
                            else str(error or "")
                        )
                        target_messages.append(
                            type(target_messages[0])(
                                role="tool",
                                content=str(event_payload.get("content", "") or "")
                                or error_text,
                                tool_call_id=str(
                                    event_payload.get("call_id", "") or ""
                                ),
                                name=str(
                                    event_payload.get("tool_name", "") or ""
                                ),
                            )
                        )
                if event_type in {
                    "model.recovery.requested",
                    "workflow.continuation.requested",
                }:
                    target_messages = kwargs.get("messages")
                    if isinstance(target_messages, list) and target_messages:
                        message_type = type(target_messages[0])
                        if last_model_content or last_model_thinking:
                            target_messages.append(
                                message_type(
                                    role="assistant",
                                    content=last_model_content,
                                    thinking=last_model_thinking or None,
                                )
                            )
                        if isinstance(event_payload, Mapping):
                            if event_type == "model.recovery.requested":
                                continuation_text = format_runtime_context_update(
                                    str(event_payload.get("message", "") or "")
                                )
                            else:
                                raw_message = event_payload.get("message")
                                continuation_text = (
                                    str(raw_message.get("content", "") or "")
                                    if isinstance(raw_message, Mapping)
                                    else str(raw_message or "")
                                )
                            if continuation_text:
                                target_messages.append(
                                    message_type(
                                        role="user",
                                        content=continuation_text,
                                    )
                                )
                    last_model_content = ""
                    last_model_thinking = ""
                _activate_compatibility_skill(event, kwargs)
                if str(getattr(event, "type", "") or "") == "run.injected":
                    payload = getattr(event, "payload", {})
                    target_messages = kwargs.get("messages")
                    if (
                        isinstance(payload, Mapping)
                        and isinstance(target_messages, list)
                        and target_messages
                    ):
                        text = str(payload.get("text", "") or "")
                        wrapped = (
                            format_injected_message(text)
                            if bool(payload.get("user_visible", True))
                            else format_runtime_context_update(text)
                        )
                        target_messages.append(
                            type(target_messages[0])(role="user", content=wrapped)
                        )
                for legacy_event in _legacy_events(event):
                    yield legacy_event
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        target_messages = kwargs.get("messages")
        if isinstance(target_messages, list) and messages and results:
            final = str(getattr(results[0], "final_message", "") or "")
            if final:
                try:
                    target_messages.append(type(messages[0])(role="assistant", content=final))
                except Exception:
                    pass

    llm = kwargs.get("llm")
    return scoped_model_tool_context(execute(), model=getattr(llm, "model", ""), max_output_tokens=getattr(llm, "max_output_tokens", 0))


async def invoke_tool_with_permissions(
    tool: Tool,
    arguments: dict[str, Any],
    *,
    permission_negotiator: Any | None = None,
) -> tuple[ToolResult, dict[str, Any] | None]:
    """Execute one Tool through the same validation/permission Engine as Runs."""

    host = PluginHost()
    register_tool_plugins(host, (tool,))
    engine = RegistryToolEngine(
        host.registries["tools.executors"],
        descriptor_registry=host.registries["tools.descriptors"],
        permission_gateway=(PermissionNegotiatorGateway(permission_negotiator) if permission_negotiator is not None else None),
    )
    result = await engine.execute(ToolCallRequest(uuid4().hex, tool.name, arguments))
    return (
        ToolResult(
            success=result.status == "succeeded",
            content=result.content,
            error=result.error.message if result.error is not None else None,
            raw_output=dict(result.output) if result.output is not None else None,
            permission_request=dict(result.permission_request) if result.permission_request is not None else None,
        ),
        _legacy_permission_decision(result.permission_decision),
    )


__all__ = ["CompletionGate", "invoke_tool_with_permissions", "run_agent_loop"]
