"""The plugin-composed Agent Loop Kernel.

Adapters submit a data-only
``RunRequest`` and receive ordered ``AgentEvent`` facts, while every capability
is supplied through a replaceable port.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from box_agent.api import (
    Artifact,
    ArtifactPublishRequest,
    AgentEvent,
    ContextBuildRequest,
    ContextBuildResult,
    ContextEngine,
    ContextItem,
    ContextManifest,
    ErrorCode,
    ErrorInfo,
    LLMPort,
    LLMRequest,
    MemoryEngine,
    MemoryEntry,
    MemoryQuery,
    ModelChunk,
    RunOptions,
    RunRequest,
    RunResult,
    ToolCallRequest,
    ToolCallResult,
    ToolExecutionContext,
    Usage,
    WorkflowContinuation,
)
from box_agent.kernel.model_recovery import decide_model_recovery
from box_agent.kernel.model_stream import stream_with_liveness


_EMPTY_FINAL_ANSWER_ERROR = "工具已执行完成，但模型未生成最终答复，请重试。"


def _schema_tool_name(schema: Mapping[str, Any]) -> str:
    name = schema.get("name")
    if isinstance(name, str) and name:
        return name
    function = schema.get("function")
    return (
        str(function.get("name", "") or "")
        if isinstance(function, Mapping)
        else ""
    )


def _empty_final_answer_retry_text(tool_call_count: int) -> str:
    return (
        "The previous natural end produced no visible final answer after using "
        f"{tool_call_count} visible tool call(s). "
        "Answer the user now with a concise final conclusion. Do not call tools "
        "unless the task is impossible to summarize without one."
    )


class _IdentityContext:
    async def assemble(self, request: ContextBuildRequest) -> ContextBuildResult:
        return ContextBuildResult(
            items=request.items,
            estimated_tokens=sum(item.estimated_tokens for item in request.items),
        )

    async def restore(self, *, resource_id: str, content_version: str | None = None):
        return None


def _ensure_context_manifest(result: ContextBuildResult) -> ContextBuildResult:
    """Give every context provider a deterministic replay manifest."""

    if result.manifest is not None:
        return result
    digest = hashlib.sha256(
        json.dumps(
            [
                {
                    "item_id": item.item_id,
                    "kind": item.kind,
                    "content": item.content,
                    "metadata": item.metadata,
                }
                for item in result.items
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return ContextBuildResult(
        items=result.items,
        estimated_tokens=result.estimated_tokens,
        compacted=result.compacted,
        removed_item_ids=result.removed_item_ids,
        metadata=result.metadata,
        host_projections=result.host_projections,
        manifest=ContextManifest(
            provider="context.provider",
            version="1",
            sources=tuple(item.resource_id or item.item_id for item in result.items),
            content_hash=digest,
            pinned=tuple(item.item_id for item in result.items if item.pinned),
        ),
    )


def _cancelled(cancel_event: Any) -> bool:
    if cancel_event is None:
        return False
    if callable(cancel_event):
        return bool(cancel_event())
    is_set = getattr(cancel_event, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _as_model_chunk(value: Any) -> ModelChunk:
    if isinstance(value, ModelChunk):
        return value
    return ModelChunk(
        content=str(getattr(value, "content", "") or ""),
        thinking=str(getattr(value, "thinking", "") or ""),
        tool_calls=tuple(getattr(value, "tool_calls", ()) or ()),
        finish_reason=getattr(value, "finish_reason", None),
        usage=getattr(value, "usage", None),
        provider_response_id=getattr(value, "provider_response_id", None),
        provider_request_id=getattr(value, "provider_request_id", None),
        activity=getattr(value, "activity", None),
        truncated_tool_calls=tuple(getattr(value, "truncated_tool_calls", ()) or ()),
        raw_finish_reason=getattr(value, "raw_finish_reason", None),
        stream_dropped_mid_tool=bool(getattr(value, "stream_dropped_mid_tool", False)),
        oversized_tool_calls=tuple(getattr(value, "oversized_tool_calls", ()) or ()),
    )


def _context_message(item: ContextItem):
    from box_agent.api import Message

    role = item.metadata.get("role")
    if role not in {"system", "user", "assistant", "tool"}:
        role = "user" if item.kind not in {"system", "tool"} else item.kind
    content = item.content
    if isinstance(content, tuple):
        content = tuple(dict(block) for block in content)
    return Message(role=role, content=content, metadata=item.metadata)


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, Mapping)
    ).strip()


def _recovery_events(value: Any) -> tuple[AgentEvent, ...]:
    """Read event facts from a persistence bundle without importing storage."""

    if value is None:
        return ()
    if isinstance(value, Mapping):
        raw_events = value.get("events", ())
    else:
        raw_events = getattr(value, "events", ())
    events: list[AgentEvent] = []
    for raw in raw_events or ():
        if isinstance(raw, AgentEvent):
            events.append(raw)
            continue
        if not isinstance(raw, Mapping):
            continue
        try:
            events.append(
                AgentEvent(
                    event_id=str(raw["event_id"]),
                    sequence=int(raw["sequence"]),
                    session_id=str(raw["session_id"]),
                    run_id=str(raw["run_id"]),
                    turn_id=raw.get("turn_id"),
                    type=str(raw["type"]),
                    payload=raw.get("payload", {}),
                    audience=tuple(raw.get("audience", ())),
                    occurred_at=raw.get("occurred_at"),
                    correlation_id=raw.get("correlation_id"),
                    protocol_version=str(raw.get("protocol_version", "1")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(sorted(events, key=lambda event: event.sequence))


def _recovered_usage(events: Sequence[AgentEvent]) -> Usage | None:
    # ``model.response.completed`` is the crash-safe source because it is
    # committed before the convenience ``model.usage`` projection. Sum one
    # usage value per completed provider call; never treat the last call as
    # the whole Run. Older logs without response usage fall back to dedicated
    # usage events.
    values = [
        event.payload.get("usage")
        for event in events
        if event.type == "model.response.completed"
        and isinstance(event.payload.get("usage"), Mapping)
    ]
    if not values:
        values = [
            event.payload
            for event in events
            if event.type == "model.usage"
        ]
    total: Usage | None = None
    for value in values:
        if not isinstance(value, Mapping):
            continue
        total = _add_usage(
            total,
            Usage(
                input_tokens=int(value.get("input_tokens", 0)),
                output_tokens=int(value.get("output_tokens", 0)),
                total_tokens=int(value.get("total_tokens", 0)),
            ),
        )
    return total


def _add_usage(total: Usage | None, current: Usage) -> Usage:
    """Accumulate independent provider-call usage into one Run total."""

    if total is None:
        return current
    return Usage(
        input_tokens=total.input_tokens + current.input_tokens,
        output_tokens=total.output_tokens + current.output_tokens,
        total_tokens=total.total_tokens + current.total_tokens,
    )


class _WorkflowToolResultView:
    """Attribute-compatible, dependency-free view passed to workflow plugins."""

    __slots__ = (
        "success",
        "content",
        "error",
        "model_context",
        "raw_output",
        "permission_request",
        "permission_decision",
    )

    def __init__(self, result: ToolCallResult) -> None:
        self.success = result.status == "succeeded"
        self.content = result.content
        self.error = result.error.message if result.error is not None else None
        # ``ToolCallResult.content`` is already the provider-visible content.
        # Legacy policies may still inspect the optional ToolResult field; an
        # explicit ``None`` preserves that contract without duplicating data.
        self.model_context = None
        self.raw_output = dict(result.output or {}) if result.output is not None else None
        self.permission_request = (
            dict(result.permission_request)
            if result.permission_request is not None
            else None
        )
        self.permission_decision = (
            dict(result.permission_decision)
            if result.permission_decision is not None
            else None
        )


def _active_skill_hashes(items: Sequence[ContextItem]) -> dict[str, str]:
    """Project run-local Skill instructions into Tool invocation metadata."""

    active: dict[str, str] = {}
    for item in items:
        metadata = getattr(item, "metadata", None)
        if not isinstance(metadata, Mapping):
            continue
        name = metadata.get("skill_name")
        content_hash = metadata.get("skill_prompt_hash")
        if (
            isinstance(name, str)
            and name.strip()
            and isinstance(content_hash, str)
            and content_hash.strip()
        ):
            active[name.strip()] = content_hash.strip()
    return active


def _recovered_context_items(
    events: Sequence[AgentEvent],
    *,
    include_user: bool = False,
) -> tuple[ContextItem, ...]:
    """Rebuild model/tool turns from durable facts after a process restart."""

    items: list[ContextItem] = []
    represented_call_ids = {
        str(call.get("call_id", call.get("id", "")) or "")
        for event in events
        if event.type == "model.response.completed"
        for call in (event.payload.get("tool_calls", ()) or ())
        if isinstance(call, Mapping)
    }
    partial_content = ""
    for event in events:
        payload = event.payload
        if event.type == "run.started":
            # A missing terminal model event in one Run must not bleed its
            # partial stream into the next Run in the same Session.
            if partial_content:
                items.append(
                    ContextItem(
                        item_id=f"recovery:model:{event.run_id}:partial-before-restart",
                        kind="assistant",
                        content=partial_content,
                        priority=10,
                        metadata={"role": "assistant", "recovered": True, "partial": True},
                    )
                )
            partial_content = ""
        if event.type == "run.started" and include_user:
            raw_input = payload.get("user_input")
            if isinstance(raw_input, Mapping):
                role = str(raw_input.get("role", "user"))
                content = raw_input.get("content", "")
                if isinstance(content, list):
                    content = tuple(
                        dict(block)
                        for block in content
                        if isinstance(block, Mapping)
                    )
                items.append(
                    ContextItem(
                        item_id=f"recovery:user:{event.run_id}:{event.sequence}",
                        kind="user",
                        content=content if isinstance(content, (str, tuple)) else str(content),
                        priority=100,
                        metadata={"role": role, "recovered": True},
                    )
                )
        elif event.type == "model.content.delta":
            partial_content += str(payload.get("content", "") or "")
        elif event.type == "model.response.completed":
            content = str(payload.get("content", "") or "")
            thinking = str(payload.get("thinking", "") or "")
            call_ids = tuple(str(item) for item in payload.get("tool_call_ids", ()) or ())
            raw_tool_calls = tuple(
                dict(item)
                for item in (payload.get("tool_calls", ()) or ())
                if isinstance(item, Mapping)
            )
            if content or thinking or call_ids:
                if call_ids and not content:
                    content = json.dumps({"tool_call_ids": list(call_ids)}, sort_keys=True)
                items.append(
                    ContextItem(
                        item_id=f"recovery:model:{event.run_id}:{event.sequence}",
                        kind="assistant",
                        content=content,
                        priority=40,
                        metadata={
                            "role": "assistant",
                            "thinking": thinking,
                            "tool_call_ids": list(call_ids),
                            "tool_calls": list(raw_tool_calls),
                            "recovered": True,
                        },
                    )
                )
            partial_content = ""
        elif event.type == "tool.call.completed":
            content = str(payload.get("content", "") or "")
            error = payload.get("error")
            if not content and isinstance(error, Mapping):
                content = str(error.get("message", "") or "")
            items.append(
                ContextItem(
                    item_id=f"recovery:tool:{event.run_id}:{event.sequence}",
                    kind="tool",
                    content=content,
                    priority=20,
                    metadata={
                        "role": "tool",
                        "tool_call_id": payload.get("call_id", ""),
                        "name": payload.get("tool_name", ""),
                        "recovered": True,
                    },
                )
            )
        elif event.type == "tool.call.requested":
            # A crash between request and completion is an explicit recovery
            # boundary. Preserve the pending call in context so a replacement
            # worker can reconcile it (or let the model choose a safe retry)
            # instead of silently forgetting the side effect intent.
            if str(payload.get("call_id", "") or "") in represented_call_ids:
                continue
            items.append(
                ContextItem(
                    item_id=f"recovery:tool-request:{event.run_id}:{event.sequence}",
                    kind="assistant",
                    content=json.dumps(
                        {
                            "tool_call_id": payload.get("call_id", ""),
                            "tool_name": payload.get("tool_name", ""),
                            "arguments": payload.get("arguments", {}),
                        },
                        sort_keys=True,
                    ),
                    priority=30,
                    metadata={
                        "role": "assistant",
                        "pending_tool_call": True,
                        "recovered": True,
                    },
                )
            )
        elif event.type in {"memory.recalled", "memory.written"}:
            raw_entries = payload.get("entries")
            if event.type == "memory.written" and not raw_entries:
                raw_entries = (payload.get("entry"),)
            for raw_entry in raw_entries or ():
                if not isinstance(raw_entry, Mapping):
                    continue
                entry_id = str(raw_entry.get("entry_id", "") or "")
                text = str(raw_entry.get("text", "") or "")
                if not entry_id or not text:
                    continue
                items.append(
                    ContextItem(
                        item_id=f"recovery:memory:{entry_id}",
                        kind="memory",
                        content=text,
                        priority=30,
                        metadata={
                            "role": "user",
                            "entry_id": entry_id,
                            "recovered": True,
                        },
                    )
                )
        elif event.type == "workflow.checkpoint":
            checkpoint_text = str(payload.get("text", "") or "")
            if checkpoint_text:
                items.append(
                    ContextItem(
                        item_id=f"recovery:workflow:{event.run_id}:{event.sequence}",
                        kind="workflow",
                        content=checkpoint_text,
                        priority=80,
                        metadata={"role": "system", "recovered": True},
                    )
                )
        elif event.type == "workflow.continuation.requested":
            raw_message = payload.get("message")
            if not isinstance(raw_message, Mapping):
                continue
            content = raw_message.get("content", "")
            if isinstance(content, list):
                content = tuple(
                    dict(block) for block in content if isinstance(block, Mapping)
                )
            if not isinstance(content, (str, tuple)):
                content = str(content)
            continuation_metadata = raw_message.get("metadata", {})
            if not isinstance(continuation_metadata, Mapping):
                continuation_metadata = {}
            items.append(
                ContextItem(
                    item_id=f"recovery:continuation:{event.run_id}:{event.sequence}",
                    kind="user",
                    content=content,
                    priority=100,
                    metadata={
                        **dict(continuation_metadata),
                        "role": str(raw_message.get("role", "user")),
                        "workflow_continuation": True,
                        "continuation_id": payload.get("continuation_id", ""),
                        "recovered": True,
                    },
                )
            )
        elif event.type == "model.recovery.requested":
            content = str(payload.get("message", "") or "")
            if content:
                items.append(
                    ContextItem(
                        item_id=f"recovery:model-repair:{event.run_id}:{event.sequence}",
                        kind="user",
                        content=content,
                        priority=100,
                        metadata={
                            "role": "user",
                            "model_recovery": True,
                            "reason": payload.get("reason", ""),
                            "attempt": payload.get("attempt", 0),
                            "recovered": True,
                        },
                    )
                )
        elif event.type == "run.injected":
            injection_id = str(payload.get("injection_id", "") or "")
            content = str(payload.get("text", "") or "")
            if injection_id and content:
                items.append(
                    ContextItem(
                        item_id=f"injection:{injection_id}",
                        kind="user",
                        content=content,
                        priority=100,
                        pinned=True,
                        metadata={
                            "role": "user",
                            "injected": True,
                            "injection_id": injection_id,
                            "recovered": True,
                        },
                    )
                )
        elif event.type == "run.injection.cancelled":
            injection_id = str(payload.get("injection_id", "") or "")
            if injection_id:
                items[:] = [
                    item
                    for item in items
                    if item.item_id != f"injection:{injection_id}"
                ]
    if partial_content:
        items.append(
            ContextItem(
                item_id="recovery:model:partial",
                kind="assistant",
                content=partial_content,
                priority=10,
                metadata={"role": "assistant", "recovered": True, "partial": True},
            )
        )
    return tuple(items)


async def _restore_context_resources(
    context_engine: Any,
    recovery: Any,
    context_items: list[ContextItem],
) -> None:
    """Rehydrate provider-owned context referenced by a checkpoint manifest.

    Event replay restores inline facts.  A Context Engine may additionally
    keep large or external resources outside the event log; the manifest is
    the stable list of resource identities that must be reattached after a
    worker restart.  Missing resources are left to the provider's normal
    compaction/error policy rather than fabricated by the Kernel.
    """

    if recovery is None:
        return
    checkpoint = getattr(recovery, "checkpoint", None)
    state = getattr(checkpoint, "state", {}) if checkpoint is not None else {}
    manifest = state.get("context_manifest") if isinstance(state, Mapping) else None
    if not isinstance(manifest, Mapping):
        return
    restore = getattr(context_engine, "restore", None)
    if not callable(restore):
        return
    existing = {item.item_id for item in context_items}
    for source in manifest.get("sources", ()) or ():
        resource_id = str(source or "")
        if not resource_id:
            continue
        try:
            value = restore(
                resource_id=resource_id,
                content_version=manifest.get("version"),
            )
            if inspect.isawaitable(value):
                value = await value
        except Exception:
            # Resource restoration is best effort.  The provider can expose a
            # diagnostic through its own event hook; replay remains anchored
            # to the durable manifest and inline event facts.
            continue
        values = value if isinstance(value, (list, tuple)) else (value,)
        for item in values:
            if isinstance(item, ContextItem) and item.item_id not in existing:
                context_items.append(item)
                existing.add(item.item_id)


class AgentLoopKernel:
    """Minimal deterministic state machine composed entirely from plugin ports."""

    def __init__(
        self,
        *,
        llm: LLMPort,
        context_engine: ContextEngine | None = None,
        memory_engine: MemoryEngine | None = None,
        tool_engine: Any | None = None,
        hooks: Sequence[Any] = (),
        workflow_policy: Any | None = None,
        artifact_processor: Any | None = None,
        default_token_budget: int = 100_000,
        recovery: Any | None = None,
    ) -> None:
        if default_token_budget <= 0:
            raise ValueError("default_token_budget must be positive")
        self._llm = llm
        self._context = context_engine or _IdentityContext()
        self._memory = memory_engine
        self._tools = tool_engine
        self._hooks = tuple(hooks)
        self._workflow = workflow_policy
        self._artifact_processor = artifact_processor
        self._default_token_budget = default_token_budget
        self._recovery = recovery

    async def run(
        self,
        request: RunRequest,
        *,
        emit: Callable[[AgentEvent], Awaitable[None] | None],
        cancel_event: Any,
        controls: AsyncIterator[Any] | None = None,
    ) -> RunResult:
        clock = asyncio.get_running_loop().time
        run_started_at = clock()
        sequence = 0
        max_steps = request.options.max_steps or 8
        recovery = self._recovery or request.metadata.get("_recovery_bundle")
        recovery_events = _recovery_events(recovery)
        # A new turn in an existing Session receives prior facts as context,
        # but those facts must not advance this Run's step/tool counters or
        # trigger the "already completed" recovery fast path.
        session_events = _recovery_events(request.metadata.get("_session_events"))
        tool_calls_used = sum(
            1 for event in recovery_events if event.type == "tool.call.requested"
        )
        recovered_steps = sum(
            1 for event in recovery_events if event.type == "model.response.completed"
        )
        model_recovery_attempts: dict[str, int] = {}
        for event in recovery_events:
            if event.type != "model.recovery.requested":
                continue
            reason = str(event.payload.get("reason", "") or "")
            attempt = event.payload.get("attempt")
            if reason and isinstance(attempt, int) and not isinstance(attempt, bool):
                model_recovery_attempts[reason] = max(
                    model_recovery_attempts.get(reason, 0),
                    attempt,
                )
        usage: Usage | None = _recovered_usage(recovery_events)
        artifacts: list[Artifact] = []
        for event in recovery_events:
            if event.type != "artifact.created":
                continue
            artifact = _artifact_from_payload(event.payload)
            if artifact is not None:
                artifacts.append(artifact)
        # URL provenance is workflow state, not a model/provider concern.  A
        # native policy may use the set to reject duplicate/unverified reads
        # and to recover evidence across a continuation boundary.
        verified_evidence_urls: set[str] = set()
        context_items: list[ContextItem] = []
        system_prompt = request.metadata.get("system_prompt")
        if isinstance(system_prompt, str) and system_prompt.strip():
            context_items.append(
                ContextItem(
                    item_id=f"{request.session_id}:system",
                    kind="system",
                    content=system_prompt,
                    priority=1000,
                    pinned=True,
                    metadata={"role": "system"},
                )
            )
        context_items.append(
            ContextItem(
                item_id=f"{request.turn_id}:user",
                kind="user",
                content=request.user_input.content,
                priority=100,
                pinned=True,
                metadata={"role": request.user_input.role},
            )
        )
        context_items.extend(_recovered_context_items(session_events, include_user=True))
        context_items.extend(_recovered_context_items(recovery_events))

        control_queue: asyncio.Queue[Any] = asyncio.Queue()
        control_task: asyncio.Task[None] | None = None
        deadline_task: asyncio.Task[None] | None = None
        cancel_reason = "cancelled"
        publish_lock = asyncio.Lock()
        permission_event_state: dict[str, set[str]] = {}
        previous_empty_call_signature: str | None = None

        async def watch_controls() -> None:
            """Keep cancellation responsive while the provider is streaming."""

            if controls is None:
                return
            try:
                async for command in controls:
                    if getattr(command, "kind", "") == "run.cancel":
                        setter = getattr(cancel_event, "set", None)
                        if callable(setter):
                            setter()
                    # Keep cancellation observable as a control fact too. The
                    # event is drained at the same safe boundary as every
                    # other extension command; setting the flag above merely
                    # makes provider waits responsive.
                    await control_queue.put(command)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A disconnected control stream must not turn a successful
                # provider response into a failed run.
                return

        async def watch_deadline() -> None:
            nonlocal cancel_reason
            if request.options.deadline_ms is None:
                return
            try:
                await asyncio.sleep(request.options.deadline_ms / 1000)
                cancel_reason = "deadline"
                setter = getattr(cancel_event, "set", None)
                if callable(setter):
                    setter()
            except asyncio.CancelledError:
                raise

        async def publish(event_type: str, payload: Mapping[str, Any]) -> None:
            nonlocal sequence
            # Tool progress may arrive concurrently from parallel executors.
            # Serialize sequence assignment, durable emission, and Hook
            # observation as one boundary so hosts never see reordered facts.
            async with publish_lock:
                event_payload = dict(payload)
                if self._artifact_processor is not None:
                    lifecycle_name = (
                        "begin_run"
                        if event_type == "run.started"
                        else "end_run"
                        if event_type in {"run.completed", "run.cancelled", "run.failed"}
                        else ""
                    )
                    lifecycle = getattr(
                        self._artifact_processor,
                        lifecycle_name,
                        None,
                    ) if lifecycle_name else None
                    if callable(lifecycle):
                        try:
                            value = (
                                lifecycle(request)
                                if lifecycle_name == "begin_run"
                                else lifecycle(
                                    request,
                                    event_type=event_type,
                                    payload=event_payload,
                                )
                            )
                            if inspect.isawaitable(value):
                                await value
                        except Exception as exc:
                            if event_type != "run.failed":
                                raise
                            # A registry failure must turn a would-be success
                            # into ``run.failed``.  Do not let the same failing
                            # terminal cleanup suppress that failure fact.
                            event_payload["artifact_registry_error"] = (
                                f"{type(exc).__name__}: {exc}"
                            )
                if event_type in {"permission.requested", "permission.resolved"}:
                    call_id = str(event_payload.get("call_id", "") or "")
                    if call_id:
                        permission_event_state.setdefault(call_id, set()).add(
                            event_type
                        )
                sequence += 1
                event = AgentEvent(
                    event_id=uuid4().hex,
                    sequence=sequence,
                    session_id=request.session_id,
                    run_id=request.metadata.get("run_id", "") or request.request_id,
                    turn_id=request.turn_id,
                    type=event_type,
                    payload=event_payload,
                )
                result = emit(event)
                if inspect.isawaitable(result):
                    await result
                for hook in self._hooks:
                    on_event = getattr(hook, "on_event", None)
                    if not callable(on_event):
                        continue
                    try:
                        hook_result = on_event(event)
                        if inspect.isawaitable(hook_result):
                            await hook_result
                    except Exception:
                        # Observability must not change the loop's business result.
                        continue

        def step_completed_payload(
            *,
            step: int,
            step_started_at: float | None = None,
            **extra: Any,
        ) -> dict[str, Any]:
            """Build the complete, stable timing boundary for one step."""

            completed_at = clock()
            return {
                "step": step,
                "elapsed_seconds": (
                    max(0.0, completed_at - step_started_at)
                    if step_started_at is not None
                    else 0.0
                ),
                "total_elapsed_seconds": max(0.0, completed_at - run_started_at),
                **extra,
            }

        async def publish_workflow_initial_events() -> None:
            """Publish optional workflow facts at the run-start boundary."""

            if self._workflow is None:
                return
            initial_events = getattr(self._workflow, "initial_events", None)
            if not callable(initial_events):
                return
            workflow_options = request.options.workflow_options
            latest_text = _text_content(request.user_input.content)
            context = {
                "phase": "run_started",
                "session_id": request.session_id,
                "run_id": run_id,
                "turn_id": request.turn_id,
                "latest_user_text": latest_text,
                "plan_start_text": workflow_options.get(
                    "plan_start_text",
                    workflow_options.get("planStartText", ""),
                ),
                "force_plan_start": bool(
                    workflow_options.get(
                        "force_plan_start",
                        workflow_options.get(
                            "forcePlanStart",
                            request.metadata.get(
                                "force_plan_start",
                                request.metadata.get("forcePlanStart", False),
                            ),
                        ),
                    )
                ),
                "tool_names": tuple(
                    str(schema.get("name", ""))
                    for schema in self._tool_schemas()
                    if isinstance(schema, Mapping) and schema.get("name")
                ),
                "recovery_events": recovery_events,
            }
            try:
                values = initial_events(context)
                if inspect.isawaitable(values):
                    values = await values
            except Exception:
                return
            if isinstance(values, Mapping):
                values = (values,)
            if not isinstance(values, (list, tuple)):
                return
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                event_type = value.get("type")
                payload = value.get("payload", {})
                if (
                    not isinstance(event_type, str)
                    or not event_type.strip()
                    or not isinstance(payload, Mapping)
                ):
                    continue
                # Workflow extensions may only emit their own namespaced
                # facts; lifecycle events remain Kernel-owned invariants.
                if not event_type.startswith("workflow."):
                    continue
                await publish(event_type, payload)

        async def drain_controls() -> int:
            """Apply and publish accepted extension commands at boundaries."""

            accepted_injections = 0
            while True:
                try:
                    command = control_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return accepted_injections
                to_dict = getattr(command, "to_dict", None)
                payload = to_dict() if callable(to_dict) else {"kind": str(command)}
                await publish("control.received", payload)
                command_kind = str(getattr(command, "kind", "") or "")
                command_payload = getattr(command, "payload", {})
                if command_kind == "run.inject" and isinstance(
                    command_payload, Mapping
                ):
                    injection_id = str(
                        command_payload.get("injection_id", "") or ""
                    )
                    text = str(
                        command_payload.get(
                            "text", command_payload.get("content", "")
                        )
                        or ""
                    )
                    if injection_id and text:
                        user_visible = bool(command_payload.get("user_visible", True))
                        source = str(command_payload.get("source", "user") or "user")
                        item_id = f"injection:{injection_id}"
                        context_items[:] = [
                            item for item in context_items if item.item_id != item_id
                        ]
                        context_items.append(
                            ContextItem(
                                item_id=item_id,
                                kind="user",
                                content=text,
                                priority=100,
                                pinned=True,
                                metadata={
                                    "role": "user",
                                    "injected": True,
                                    "injection_id": injection_id,
                                    "user_visible": user_visible,
                                    "source": source,
                                },
                            )
                        )
                        accepted_injections += 1
                        await publish(
                            "run.injected",
                            {
                                "injection_id": injection_id,
                                "text": text,
                                "user_visible": user_visible,
                                "source": source,
                            },
                        )
                    continue
                if command_kind == "run.cancel_inject" and isinstance(
                    command_payload, Mapping
                ):
                    injection_id = str(
                        command_payload.get("injection_id", "") or ""
                    )
                    item_id = f"injection:{injection_id}"
                    removed = any(item.item_id == item_id for item in context_items)
                    context_items[:] = [
                        item for item in context_items if item.item_id != item_id
                    ]
                    await publish(
                        "run.injection.cancelled",
                        {"injection_id": injection_id, "removed": removed},
                    )
                    continue
                if self._workflow is None:
                    continue
                handler = getattr(self._workflow, "handle_control", None)
                if not callable(handler):
                    continue
                try:
                    handled = handler(command)
                    if inspect.isawaitable(handled):
                        handled = await handled
                except Exception:
                    # A faulty workflow extension must not corrupt the run.
                    continue
                if handled is True or isinstance(handled, Mapping):
                    await publish(
                        "workflow.control.applied",
                        {
                            "command_id": getattr(command, "command_id", ""),
                            "kind": getattr(command, "kind", ""),
                            "result": (
                                dict(handled)
                                if isinstance(handled, Mapping)
                                else {}
                            ),
                        },
                    )

        async def workflow_action_for_step(step: int) -> ToolCallRequest | None:
            """Resolve a trusted workflow action without coupling the kernel.

            The native SPI uses ``decide`` while the existing presentation and
            external-skill policies expose ``next_deterministic_action``.  Both
            are intentionally duck-typed here so those policies can migrate
            without moving their implementation into the loop.
            """

            if self._workflow is None:
                return None
            action: Any | None = None
            next_action = getattr(self._workflow, "next_deterministic_action", None)
            if callable(next_action):
                try:
                    action = next_action()
                    if inspect.isawaitable(action):
                        action = await action
                except Exception:
                    action = None
            if action is None:
                decide = getattr(self._workflow, "decide", None)
                if callable(decide):
                    try:
                        decision = decide(
                            {
                                "phase": "before_model",
                                "step": step,
                                "session_id": request.session_id,
                                "run_id": run_id,
                                "turn_id": request.turn_id,
                                "context_items": tuple(context_items),
                            }
                        )
                        action = (
                            await decision
                            if inspect.isawaitable(decision)
                            else decision
                        )
                        if isinstance(action, Mapping) and isinstance(
                            action.get("action"), Mapping
                        ):
                            action = action["action"]
                    except Exception:
                        action = None
            if action is None or action is False or action is True:
                return None
            get = action.get if isinstance(action, Mapping) else None
            action_id = (
                get("action_id") if get is not None else getattr(action, "action_id", None)
            )
            tool_name = (
                get("tool_name") if get is not None else getattr(action, "tool_name", None)
            )
            arguments = (
                get("arguments", {})
                if get is not None
                else getattr(action, "arguments", {})
            )
            capability = (
                get("capability", "workflow")
                if get is not None
                else getattr(action, "capability", "workflow")
            )
            if not isinstance(tool_name, str) or not tool_name.strip():
                return None
            if not isinstance(arguments, Mapping):
                return None
            action_id = str(action_id or f"{tool_name}:{step}")
            capability = str(capability or "workflow")
            supports_action = getattr(self._tools, "supports_workflow_action", None)
            supported = False
            if callable(supports_action):
                try:
                    supported = supports_action(tool_name, capability)
                    if inspect.isawaitable(supported):
                        supported = await supported
                except Exception:
                    supported = False
            if supported is not True:
                await publish(
                    "workflow.action.rejected",
                    {
                        "action_id": action_id,
                        "capability": capability,
                        "tool_name": tool_name,
                        "reason": "tool_did_not_declare_runtime_workflow_action",
                    },
                )
                return None
            call_id = "workflow_" + hashlib.sha256(
                action_id.encode("utf-8")
            ).hexdigest()[:24]
            return ToolCallRequest(
                call_id=call_id,
                tool_name=tool_name,
                arguments=dict(arguments),
                metadata={
                    "workflow_action_id": action_id,
                    "workflow_capability": capability,
                },
            )

        async def workflow_preflight(
            call: ToolCallRequest,
            *,
            parallel: bool = False,
        ) -> str | None:
            """Let trusted policies reject an unsafe/out-of-scope call."""

            if self._workflow is None:
                return None
            for method_name, kwargs in (
                ("plan_scope_error", {}),
                (
                    "tool_call_error",
                    {
                        "verified_evidence_urls": verified_evidence_urls,
                        "parallel": parallel,
                    },
                ),
            ):
                method = getattr(self._workflow, method_name, None)
                if not callable(method):
                    continue
                try:
                    try:
                        value = method(call.tool_name, dict(call.arguments), **kwargs)
                    except TypeError as exc:
                        # ``parallel`` was added as an optional workflow
                        # capability. Retry the historical signature instead
                        # of silently dropping a third-party policy that has
                        # not adopted the keyword yet.
                        if method_name != "tool_call_error" or "parallel" not in kwargs:
                            raise
                        if "parallel" not in str(exc):
                            raise
                        legacy_kwargs = {
                            key: value
                            for key, value in kwargs.items()
                            if key != "parallel"
                        }
                        value = method(
                            call.tool_name,
                            dict(call.arguments),
                            **legacy_kwargs,
                        )
                    value = await value if inspect.isawaitable(value) else value
                except Exception:
                    continue
                if isinstance(value, str) and value.strip():
                    return value
            return None

        async def workflow_exempts_tool_budget(tool_name: str) -> bool:
            """Ask a policy whether this call is outside its run budget."""

            if self._workflow is None:
                return False
            method = getattr(self._workflow, "exempts_tool_budget", None)
            if not callable(method):
                return False
            try:
                value = method(tool_name)
                if inspect.isawaitable(value):
                    value = await value
                return bool(value)
            except Exception:
                # A faulty optional policy must fail closed for budgets.
                return False

        async def record_visible_workflow_evidence(
            call: ToolCallRequest,
            result: ToolCallResult,
        ) -> None:
            """Forward the exact model-visible tool result to a workflow.

            Presentation and other evidence-driven policies must never infer
            provenance from an executor's hidden/raw payload.  The callback
            therefore receives the normalized result that is published to the
            model, and may return URL evidence for the next preflight.
            """

            if self._workflow is None or result.status != "succeeded":
                return
            view = _WorkflowToolResultView(result)
            content = str(result.content or "")
            record_visible = getattr(
                self._workflow, "record_visible_tool_result", None
            )
            if callable(record_visible):
                try:
                    value = record_visible(
                        call.tool_name,
                        dict(call.arguments),
                        view,
                        content,
                    )
                    if inspect.isawaitable(value):
                        await value
                except Exception:
                    # One observer must not prevent another provenance
                    # callback from receiving the same normalized result.
                    pass
            evidence_urls = getattr(self._workflow, "evidence_urls", None)
            values: Any = ()
            if callable(evidence_urls):
                try:
                    values = evidence_urls(
                        call.tool_name,
                        dict(call.arguments),
                        view,
                    )
                    if inspect.isawaitable(values):
                        values = await values
                except Exception:
                    values = ()
            elif bool(
                getattr(self._workflow, "is_direct_evidence_read_tool", lambda _name: False)(
                    call.tool_name
                )
            ):
                direct_url = getattr(
                    self._workflow, "direct_evidence_url", None
                )
                if callable(direct_url):
                    try:
                        values = direct_url(
                            call.tool_name,
                            dict(call.arguments),
                            view,
                        )
                        if inspect.isawaitable(values):
                            values = await values
                    except Exception:
                        values = ()
            if isinstance(values, str):
                values = (values,)
            if isinstance(values, (list, tuple, set, frozenset)):
                verified_evidence_urls.update(
                    value.strip()
                    for value in values
                    if isinstance(value, str) and value.strip()
                )

        async def workflow_continuation_for_turn(
            *, stop_reason: str, final_content: str, step: int
        ) -> WorkflowContinuation | None:
            """Resolve a typed continuation without embedding workflow rules."""

            if self._workflow is None:
                return None
            # A continuation can only make progress if the run still has a
            # budgeted tool call available.  Without this generic guard a
            # completion-gate plugin could keep injecting nudges after
            # ``max_tool_calls`` was exhausted, forcing the model through
            # synthetic tool failures until ``max_steps``.
            if max_tool_calls is not None and tool_calls_used >= max_tool_calls:
                return None
            method = getattr(self._workflow, "next_continuation", None)
            if not callable(method):
                return None
            try:
                value = method(
                    stop_reason=stop_reason,
                    final_content=final_content,
                    step=step,
                )
                if inspect.isawaitable(value):
                    value = await value
            except Exception:
                # A faulty continuation plugin must not suppress the normal
                # terminal result or corrupt the loop's core invariants.
                return None
            return value if isinstance(value, WorkflowContinuation) else None

        async def record_workflow_result(
            call: ToolCallRequest,
            result: ToolCallResult,
            *,
            executed: bool = True,
        ) -> None:
            if self._workflow is None:
                return
            record = getattr(self._workflow, "record_tool_result", None)
            if not callable(record):
                return
            try:
                value = record(
                    call.tool_name,
                    dict(call.arguments),
                    _WorkflowToolResultView(result),
                    executed=executed,
                )
                if inspect.isawaitable(value):
                    await value
            except Exception:
                # A workflow is an extension; a faulty policy must not corrupt
                # the durable run or suppress the tool result.
                return

        async def decorate_workflow_result(
            call: ToolCallRequest,
            result: ToolCallResult,
        ) -> ToolCallResult:
            """Apply optional workflow-owned output decoration at the event edge."""

            if self._workflow is None:
                return result
            decorate = getattr(self._workflow, "result_output", None)
            if not callable(decorate):
                return result
            try:
                value = decorate(
                    call.tool_name,
                    dict(call.arguments),
                    _WorkflowToolResultView(result),
                )
                if inspect.isawaitable(value):
                    value = await value
            except Exception:
                return result
            if not isinstance(value, Mapping):
                return result
            return ToolCallResult(
                call_id=result.call_id,
                status=result.status,
                content=result.content,
                output=dict(value),
                error=result.error,
                permission_request=result.permission_request,
                permission_decision=result.permission_decision,
            )

        async def workflow_pause_after_tool(
            call: ToolCallRequest,
            result: ToolCallResult,
        ) -> str | None:
            """Resolve an optional workflow pause after a completed tool call."""

            if self._workflow is None:
                return None
            pause = getattr(self._workflow, "pause_after_tool", None)
            if not callable(pause):
                return None
            try:
                value = pause(call.tool_name, _WorkflowToolResultView(result))
                if inspect.isawaitable(value):
                    value = await value
            except Exception:
                return None
            return value if isinstance(value, str) and value.strip() else None

        async def workflow_terminal_metadata(
            *,
            stop_reason: str,
            final_content: str,
        ) -> dict[str, Any]:
            """Collect optional workflow-owned terminal facts for replay/hosts."""

            if self._workflow is None:
                return {}
            metadata = getattr(self._workflow, "terminal_metadata", None)
            if not callable(metadata):
                return {}
            try:
                value = metadata(stop_reason, final_content)
                if inspect.isawaitable(value):
                    value = await value
            except Exception:
                return {}
            return dict(value) if isinstance(value, Mapping) else {}

        async def workflow_terminal_message(
            *,
            stop_reason: str,
            final_content: str,
        ) -> str | None:
            """Resolve an optional workflow-owned terminal boundary message."""

            if self._workflow is None:
                return None
            message = getattr(self._workflow, "terminal_message", None)
            if not callable(message):
                return None
            try:
                value = message(stop_reason, final_content)
                if inspect.isawaitable(value):
                    value = await value
            except Exception:
                return None
            return value if isinstance(value, str) and value.strip() else None

        async def cancelled_result() -> RunResult:
            """Publish a cancellation boundary with workflow terminal facts."""

            metadata = await workflow_terminal_metadata(
                stop_reason=cancel_reason,
                final_content="",
            )
            await publish("run.cancelled", {
                "stop_reason": cancel_reason,
                "metadata": metadata,
            })
            return RunResult(
                status="cancelled",
                stop_reason=cancel_reason,
                usage=usage,
                artifacts=tuple(artifacts),
                metadata=metadata,
            )

        async def refresh_workflow_context(step: int) -> None:
            """Replace workflow-owned context before each model request.

            A workflow may change state while handling a tool call.  Refresh
            only items explicitly marked ``workflow_context`` so host and
            provider history remains untouched and stale instructions cannot
            accumulate across steps.
            """

            if self._workflow is None:
                return
            builder = getattr(self._workflow, "context_items", None)
            try:
                checkpoint_text: str | None = None
                build_checkpoint = getattr(
                    self._workflow,
                    "build_checkpoint",
                    None,
                )
                if callable(build_checkpoint):
                    checkpoint_value = build_checkpoint()
                    if inspect.isawaitable(checkpoint_value):
                        checkpoint_value = await checkpoint_value
                    if checkpoint_value is not None:
                        checkpoint_text = str(checkpoint_value)
                        update_checkpoint = getattr(
                            self._workflow,
                            "update_checkpoint",
                            None,
                        )
                        if callable(update_checkpoint):
                            checkpoint_update = update_checkpoint(checkpoint_text)
                            if inspect.isawaitable(checkpoint_update):
                                checkpoint_update = await checkpoint_update
                            updated_text = getattr(checkpoint_update, "text", None)
                            if isinstance(updated_text, str):
                                checkpoint_text = updated_text
                            recovered_urls = getattr(
                                checkpoint_update,
                                "recovered_evidence_urls",
                                (),
                            )
                            verified_evidence_urls.update(
                                str(url).strip()
                                for url in (recovered_urls or ())
                                if isinstance(url, str) and url.strip()
                            )
                workflow_context = {
                    "phase": "before_model",
                    "step": step,
                    "session_id": request.session_id,
                    "run_id": run_id,
                    "turn_id": request.turn_id,
                    "checkpoint_text": checkpoint_text,
                }
                if callable(builder):
                    value = builder(workflow_context)
                    if inspect.isawaitable(value):
                        value = await value
                else:
                    # Older WorkflowPolicy implementations expose only a
                    # human-readable checkpoint. Keep them usable on the
                    # native Kernel while they migrate to context_items().
                    value = checkpoint_text
                    if value:
                        value = (
                            ContextItem(
                                item_id=(
                                    f"workflow:{getattr(self._workflow, 'kind', 'workflow')}"
                                ),
                                kind="workflow",
                                content=str(value),
                                priority=900,
                                pinned=True,
                                metadata={
                                    "role": "system",
                                    "workflow_context": True,
                                    "workflow": str(
                                        getattr(self._workflow, "kind", "workflow")
                                    ),
                                },
                            ),
                        )
            except Exception:
                return
            context_items[:] = [
                item
                for item in context_items
                if not (
                    isinstance(getattr(item, "metadata", None), Mapping)
                    and bool(item.metadata.get("workflow_context"))
                )
            ]
            if value is None:
                return
            values = value if isinstance(value, (list, tuple)) else (value,)
            contributions = [
                item
                for item in values
                if item is not None
                and hasattr(item, "item_id")
                and hasattr(item, "content")
            ]
            if not contributions:
                return
            # Keep workflow instructions adjacent to the system prompt and
            # before the current user turn, matching the legacy Goal context
            # ordering while remaining a generic policy contribution.
            insert_at = 1 if context_items and context_items[0].kind == "system" else 0
            context_items[insert_at:insert_at] = contributions

        if controls is not None:
            control_task = asyncio.create_task(watch_controls())
            # Give a pre-populated control stream one scheduling turn so its
            # commands participate in the first context boundary.
            await asyncio.sleep(0)
        if request.options.deadline_ms is not None:
            deadline_task = asyncio.create_task(watch_deadline())

        run_id = request.metadata.get("run_id", "") or request.request_id
        try:
            await drain_controls()
            if _cancelled(cancel_event):
                return await cancelled_result()

            await publish(
                "run.started",
                {
                    "request_id": request.request_id,
                    "user_input": request.user_input.to_dict(),
                    "max_steps": max_steps,
                    "tools": {
                        str(schema["name"]): dict(schema)
                        for schema in self._tool_schemas()
                        if isinstance(schema, Mapping) and schema.get("name")
                    },
                    "correlation": {
                        "session_id": str(
                            request.metadata.get("correlation_session_id", "")
                            or request.session_id
                        ),
                        "turn_id": str(
                            request.metadata.get("correlation_turn_id", "")
                            or request.turn_id
                        ),
                        "task_id": str(
                            request.metadata.get("task_id", "") or request.turn_id
                        ),
                    },
                },
            )
            await publish_workflow_initial_events()
            await _restore_context_resources(self._context, recovery, context_items)
            if self._memory is not None:
                memory_text = _text_content(request.user_input.content)
                try:
                    recall = (
                        await self._memory.recall(MemoryQuery(memory_text, limit=10))
                        if memory_text
                        else None
                    )
                except Exception as exc:
                    recall = None
                    await publish(
                        "memory.error",
                        {
                            "phase": "recall",
                            "message": f"{type(exc).__name__}: {exc}",
                        },
                    )
                await publish(
                    "memory.recalled",
                    {
                        "entry_ids": [entry.entry_id for entry in recall.entries]
                        if recall is not None
                        else [],
                        "entries": [entry.to_dict() for entry in recall.entries]
                        if recall is not None
                        else [],
                    },
                )
                context_items.extend(
                    ContextItem(
                        item_id=f"memory:{entry.entry_id}",
                        kind="memory",
                        content=entry.text,
                        priority=max(1, int(entry.score * 100)),
                        metadata={"role": "user", "entry_id": entry.entry_id},
                    )
                    for entry in (recall.entries if recall is not None else ())
                )

                # Hosts may provide explicit durable observations for this
                # turn.  Writes are opt-in and remain behind the MemoryEngine
                # port so the Kernel never knows a concrete memory backend.
                raw_writes = request.metadata.get("memory_writes", ())
                if isinstance(raw_writes, Mapping):
                    raw_writes = (raw_writes,)
                if isinstance(raw_writes, (list, tuple)):
                    already_written = {
                        str(event.payload.get("entry_id", ""))
                        for event in recovery_events
                        if event.type == "memory.written"
                    }
                    for raw_entry in raw_writes:
                        if not isinstance(raw_entry, Mapping):
                            continue
                        try:
                            entry = MemoryEntry(
                                entry_id=str(raw_entry.get("entry_id", "")),
                                text=str(raw_entry.get("text", "")),
                                kind=str(raw_entry.get("kind", "fact")),
                                metadata=dict(raw_entry.get("metadata", {})),
                            )
                        except Exception as exc:
                            await publish(
                                "memory.error",
                                {
                                    "phase": "write.validate",
                                    "message": f"{type(exc).__name__}: {exc}",
                                },
                            )
                            continue
                        if entry.entry_id in already_written:
                            # The memory write committed before a worker crash;
                            # replay must not issue it a second time.
                            continue
                        await publish(
                            "memory.write.requested",
                            {
                                "entry_id": entry.entry_id,
                                "kind": entry.kind,
                            },
                        )
                        try:
                            stored = await self._memory.write(entry)
                        except Exception as exc:
                            await publish(
                                "memory.error",
                                {
                                    "phase": "write",
                                    "entry_id": entry.entry_id,
                                    "message": f"{type(exc).__name__}: {exc}",
                                },
                            )
                        else:
                            already_written.add(stored.entry_id)
                            await publish(
                                "memory.written",
                                {
                                    "entry_id": stored.entry_id,
                                    "kind": stored.kind,
                                    "text": stored.text,
                                    "entry": stored.to_dict(),
                                },
                            )

            max_tool_calls = request.options.max_tool_calls
            if max_tool_calls is None and self._workflow is not None:
                # Match the legacy CompletionGate contract: a workflow may
                # provide a default cap, while an explicit RunOptions value
                # remains the stricter host-owned override.
                candidate_budget = getattr(self._workflow, "max_tool_calls", None)
                if isinstance(candidate_budget, int) and not isinstance(
                    candidate_budget, bool
                ) and candidate_budget > 0:
                    max_tool_calls = candidate_budget
            last_response = next(
                (
                    event
                    for event in reversed(recovery_events)
                    if event.type == "model.response.completed"
                ),
                None,
            )
            continuation_after_response = False
            model_recovery_after_response = False
            if last_response is not None:
                response_position = next(
                    (
                        index
                        for index, candidate in enumerate(recovery_events)
                        if candidate.event_id == last_response.event_id
                    ),
                    -1,
                )
                continuation_after_response = any(
                    event.type == "workflow.continuation.requested"
                    for event in recovery_events[response_position + 1 :]
                )
                model_recovery_after_response = any(
                    event.type == "model.recovery.requested"
                    for event in recovery_events[response_position + 1 :]
                )
            if (
                last_response is not None
                and not continuation_after_response
                and not model_recovery_after_response
                and not (
                last_response.payload.get("tool_call_ids") or ()
                )
            ):
                # A worker may crash after the provider response but before
                # memory flush/workflow checkpoint/terminal publication. Do
                # not treat the response as a completed Run until the
                # post-response lifecycle boundaries are replayed. Each
                # boundary is idempotent and the corresponding event fence
                # prevents a duplicate flush after a clean restart.
                response_index = next(
                    (
                        index
                        for index, candidate in enumerate(recovery_events)
                        if candidate.event_id == last_response.event_id
                    ),
                    -1,
                )
                post_response_events = (
                    recovery_events[response_index + 1 :]
                    if response_index >= 0
                    else ()
                )
                if self._memory is not None and not any(
                    event.type == "memory.flushed" for event in post_response_events
                ):
                    try:
                        await self._memory.flush()
                    except Exception as exc:
                        await publish(
                            "memory.error",
                            {
                                "phase": "flush",
                                "message": f"{type(exc).__name__}: {exc}",
                            },
                        )
                    else:
                        await publish("memory.flushed", {"status": "completed"})
                if not any(
                    event.type == "step.completed" for event in post_response_events
                ):
                    await publish(
                        "step.completed",
                        step_completed_payload(
                            step=max(1, recovered_steps),
                            recovered=True,
                        ),
                    )
                final_content = str(last_response.payload.get("content", "") or "")
                recovered_stop_reason = str(
                    last_response.payload.get("finish_reason", "recovered")
                )
                continuation = await workflow_continuation_for_turn(
                    stop_reason=recovered_stop_reason,
                    final_content=final_content,
                    step=max(1, recovered_steps),
                )
                # ``next_continuation`` may update workflow-owned budgets and
                # progress guards. Persist that state before publishing the
                # continuation fact so a crash at this exact boundary cannot
                # replay with stale counters.
                await self._publish_workflow_checkpoint(publish)
                if continuation is None:
                    terminal_content = final_content
                    terminal_stop_reason = recovered_stop_reason
                    boundary_message = await workflow_terminal_message(
                        stop_reason=recovered_stop_reason,
                        final_content=final_content,
                    )
                    if boundary_message is not None:
                        terminal_content = boundary_message
                        terminal_stop_reason = "checkpoint_paused"
                    terminal_metadata = await workflow_terminal_metadata(
                        stop_reason=terminal_stop_reason,
                        final_content=terminal_content,
                    )
                    await publish(
                        "run.completed",
                        {
                            "stop_reason": terminal_stop_reason,
                            "final_content": terminal_content,
                            "metadata": terminal_metadata,
                        },
                    )
                    return RunResult(
                        status="completed",
                        stop_reason=terminal_stop_reason,
                        final_message=terminal_content,
                        usage=usage,
                        artifacts=tuple(artifacts),
                        metadata=terminal_metadata,
                    )
                await publish(
                    "workflow.continuation.requested",
                    {
                        "continuation_id": continuation.continuation_id,
                        "message": continuation.message.to_dict(),
                        "reason": continuation.reason,
                        "metadata": dict(continuation.metadata),
                    },
                )
                context_items.append(
                    ContextItem(
                        item_id=f"workflow:continuation:{continuation.continuation_id}",
                        kind="user",
                        content=continuation.message.content,
                        priority=100,
                        metadata={
                            **dict(continuation.message.metadata),
                            "role": continuation.message.role,
                            "workflow_continuation": True,
                            "continuation_id": continuation.continuation_id,
                        },
                    )
                )
            for step in range(recovered_steps, max_steps):
                await drain_controls()
                if _cancelled(cancel_event):
                    return await cancelled_result()

                step_started_at = asyncio.get_running_loop().time()
                await publish(
                    "step.started",
                    {"step": step + 1, "max_steps": max_steps},
                )
                await refresh_workflow_context(step + 1)
                workflow_call = await workflow_action_for_step(step + 1)
                context = _ensure_context_manifest(
                    await self._context.assemble(
                        ContextBuildRequest(
                            items=tuple(context_items),
                            token_budget=request.options.context_budget
                            or self._default_token_budget,
                            session_id=request.session_id,
                            run_id=run_id,
                            metadata=dict(request.metadata),
                        )
                    )
                )
                await publish(
                    "context.assembled",
                    {
                        "estimated_tokens": context.estimated_tokens,
                        "compacted": context.compacted,
                        "removed_item_ids": list(context.removed_item_ids),
                        "manifest": context.manifest.to_dict()
                        if context.manifest is not None
                        else None,
                        "metadata": dict(context.metadata),
                        "host_projections": [
                            projection.to_dict()
                            for projection in context.host_projections
                        ],
                    },
                )
                if context.compacted:
                    # Compaction is a state transition, not merely an
                    # implementation detail.  Persist a dedicated fact so a
                    # replacement worker can explain why the reconstructed
                    # context differs from the original transcript.
                    await publish(
                        "context.compacted",
                        {
                            "estimated_tokens": context.estimated_tokens,
                            "token_budget": request.options.context_budget
                            or self._default_token_budget,
                            "removed_item_ids": list(context.removed_item_ids),
                            "manifest": context.manifest.to_dict()
                            if context.manifest is not None
                            else None,
                            "metadata": dict(context.metadata),
                            "host_projections": [
                                projection.to_dict()
                                for projection in context.host_projections
                            ],
                        },
                    )
                llm_request = LLMRequest(
                    messages=tuple(_context_message(item) for item in context.items),
                    tools=self._tool_schemas_for_workflow(),
                    session_id=request.session_id,
                    run_id=run_id,
                    turn_id=request.turn_id,
                    metadata={
                        # Preserve host/provider routing metadata while
                        # excluding process-local recovery snapshots.
                        **{
                            str(key): value
                            for key, value in request.metadata.items()
                            if not str(key).startswith("_")
                        },
                        "thinking_enabled": request.options.thinking_enabled,
                        "workflow_id": request.options.workflow_id,
                    },
                )
                chunks: list[ModelChunk] = []
                if workflow_call is not None:
                    # A deterministic workflow action is represented as a
                    # synthetic model chunk so it follows the exact same
                    # permission, effect, artifact, and event path as a
                    # model-issued call.  No provider request is made.
                    chunks.append(
                        ModelChunk(
                            tool_calls=(workflow_call,),
                            finish_reason="tool_calls",
                        )
                    )
                else:
                    await publish(
                        "model.requested",
                        {
                            "step": step + 1,
                            "message_count": len(llm_request.messages),
                            "tool_count": len(llm_request.tools),
                            # Persist the exact provider-neutral request and
                            # context fence so a replacement worker can audit
                            # or reproduce the boundary instead of relying on
                            # a reconstructed prompt alone.
                            "request": llm_request.to_dict(),
                            "context_digest": (
                                context.manifest.content_hash
                                if context.manifest is not None
                                else None
                            ),
                        },
                    )
                    try:
                        async for raw_chunk in stream_with_liveness(
                            self._llm.stream(llm_request),
                            stale_seconds=request.options.provider_stale_seconds,
                        ):
                            chunk = _as_model_chunk(raw_chunk)
                            chunks.append(chunk)
                            if chunk.activity:
                                await publish("model.activity", dict(chunk.activity))
                            if chunk.content:
                                await publish("model.content.delta", {"content": chunk.content})
                            if chunk.thinking:
                                await publish("model.thinking.delta", {"thinking": chunk.thinking})
                    except Exception as exc:
                        error = ErrorInfo(
                            code=ErrorCode.MODEL_PROVIDER_ERROR,
                            category="model",
                            message=f"model provider failed: {type(exc).__name__}: {exc}",
                            retryable=True,
                            details={"exception_type": type(exc).__name__},
                        )
                        await publish("run.failed", {"stop_reason": "model_error", "error": error.to_dict()})
                        return RunResult(
                            status="failed",
                            stop_reason="model_error",
                            usage=usage,
                            artifacts=tuple(artifacts),
                            error=error,
                        )

                if control_task is not None:
                    # Controls can be produced by the provider task that just
                    # yielded. Let the watcher transfer them before deciding
                    # this is a terminal turn.
                    await asyncio.sleep(0)
                injected_after_model = await drain_controls()
                if _cancelled(cancel_event):
                    return await cancelled_result()

                content = "".join(chunk.content for chunk in chunks)
                thinking = "".join(chunk.thinking for chunk in chunks)
                calls: list[ToolCallRequest] = []
                seen_call_ids: set[str] = set()
                call_usage: Usage | None = None
                for chunk in chunks:
                    if chunk.usage is not None:
                        call_usage = chunk.usage
                    for call in chunk.tool_calls:
                        if call.call_id not in seen_call_ids:
                            calls.append(call)
                            seen_call_ids.add(call.call_id)
                finish_reason = next(
                    (chunk.finish_reason for chunk in reversed(chunks) if chunk.finish_reason),
                    "stop",
                )
                await publish(
                    "model.response.completed",
                    {
                        "content": content,
                        "thinking": thinking,
                        "finish_reason": finish_reason,
                        "tool_call_ids": [call.call_id for call in calls],
                        "tool_calls": [call.to_dict() for call in calls],
                        "usage": call_usage.to_dict() if call_usage else None,
                        "provider_response_id": next(
                            (
                                chunk.provider_response_id
                                for chunk in reversed(chunks)
                                if chunk.provider_response_id
                            ),
                            None,
                        ),
                        "provider_request_id": next(
                            (
                                chunk.provider_request_id
                                for chunk in reversed(chunks)
                                if chunk.provider_request_id
                            ),
                            None,
                        ),
                        "truncated_tool_calls": [
                            item for chunk in chunks for item in chunk.truncated_tool_calls
                        ],
                        "raw_finish_reason": next(
                            (
                                chunk.raw_finish_reason
                                for chunk in reversed(chunks)
                                if chunk.raw_finish_reason
                            ),
                            None,
                        ),
                        "stream_dropped_mid_tool": any(
                            chunk.stream_dropped_mid_tool for chunk in chunks
                        ),
                        "oversized_tool_calls": [
                            item for chunk in chunks for item in chunk.oversized_tool_calls
                        ],
                    },
                )
                if call_usage is not None:
                    usage = _add_usage(usage, call_usage)
                    # Keep a dedicated usage fact for metering consumers while
                    # also embedding usage in the response event for replay.
                    await publish("model.usage", call_usage.to_dict())

                if self._workflow is not None:
                    record_model = getattr(
                        self._workflow, "record_model_response", None
                    )
                    if callable(record_model):
                        try:
                            recorded = record_model(
                                content=content,
                                finish_reason=finish_reason,
                                usage=(
                                    call_usage.to_dict()
                                    if call_usage is not None
                                    else None
                                ),
                            )
                            if inspect.isawaitable(recorded):
                                await recorded
                        except Exception:
                            # Workflow analysis must not corrupt the model
                            # boundary or suppress a valid terminal response.
                            pass

                truncated_tool_calls = tuple(
                    item for chunk in chunks for item in chunk.truncated_tool_calls
                )
                oversized_tool_calls = tuple(
                    item for chunk in chunks for item in chunk.oversized_tool_calls
                )
                repair = decide_model_recovery(
                    finish_reason=finish_reason,
                    tool_names=tuple(
                        {
                            *(call.tool_name for call in calls),
                            *(
                                str(item.get("name", "") or "")
                                for item in truncated_tool_calls
                            ),
                            *(
                                str(item.get("name", "") or "")
                                for item in oversized_tool_calls
                            ),
                        }
                    ),
                    truncated_tool_calls=truncated_tool_calls,
                    oversized_tool_calls=oversized_tool_calls,
                    attempts=model_recovery_attempts,
                    partial_content=content,
                    max_output_continuations=(
                        request.options.max_truncation_continuations
                        if request.options.max_truncation_continuations is not None
                        else 3
                    ),
                    max_tool_repair_attempts=(
                        request.options.max_truncated_tool_call_retries
                        if request.options.max_truncated_tool_call_retries is not None
                        else 3
                    ),
                )
                if repair is not None:
                    # A provider-declared incomplete tool call is never an
                    # executable request.  Persist the repair instruction as
                    # a first-class event before the next provider request so
                    # a replacement worker resumes at the exact same boundary.
                    if repair.exhausted:
                        terminal_stop_reason = (
                            "max_tokens"
                            if repair.reason
                            in {"truncated_tool_call", "truncated_output"}
                            else repair.reason
                        )
                        error = ErrorInfo(
                            code=ErrorCode.MODEL_PROVIDER_ERROR,
                            category="model",
                            message=repair.terminal_message,
                            retryable=True,
                            details={
                                **dict(repair.details),
                                "reason": repair.reason,
                                "repair_attempts": repair.max_attempts,
                            },
                        )
                        await publish(
                            "step.completed",
                            step_completed_payload(
                                step=step + 1,
                                step_started_at=step_started_at,
                            ),
                        )
                        await publish(
                            "run.failed",
                            {
                                "stop_reason": terminal_stop_reason,
                                "error": error.to_dict(),
                            },
                        )
                        return RunResult(
                            status="failed",
                            stop_reason=terminal_stop_reason,
                            usage=usage,
                            artifacts=tuple(artifacts),
                            error=error,
                        )
                    model_recovery_attempts[repair.reason] = repair.attempt
                    if content.strip():
                        context_items.append(
                            ContextItem(
                                item_id=f"model:{run_id}:{step}:discarded-tool-attempt",
                                kind="assistant",
                                content=content,
                                priority=30,
                                metadata={
                                    "role": "assistant",
                                    "thinking": thinking,
                                    "incomplete_model_attempt": True,
                                },
                            )
                        )
                    await publish(
                        "model.recovery.requested",
                        {
                            "reason": repair.reason,
                            "attempt": repair.attempt,
                            "max_attempts": repair.max_attempts,
                            "message": repair.message,
                            **dict(repair.details),
                        },
                    )
                    context_items.append(
                        ContextItem(
                            item_id=f"model:recovery:{run_id}:{step}:{repair.attempt}",
                            kind="user",
                            content=repair.message or "",
                            priority=100,
                            metadata={
                                "role": "user",
                                "model_recovery": True,
                                "reason": repair.reason,
                                "attempt": repair.attempt,
                            },
                        )
                    )
                    await publish(
                        "step.completed",
                        step_completed_payload(
                            step=step + 1,
                            step_started_at=step_started_at,
                        ),
                    )
                    continue
                model_recovery_attempts.clear()
                requested_tool_names = {
                    call.call_id: call.tool_name for call in calls
                }
                canonicalize = getattr(self._tools, "canonicalize_call", None)
                if callable(canonicalize):
                    canonical_calls: list[ToolCallRequest] = []
                    for call in calls:
                        value = canonicalize(call)
                        if inspect.isawaitable(value):
                            value = await value
                        canonical_calls.append(
                            value if isinstance(value, ToolCallRequest) else call
                        )
                    calls = canonical_calls
                has_tool_catalog = bool(self._tool_schemas())
                offered_schemas = self._tool_schemas_for_workflow()
                offered_names = {
                    _schema_tool_name(schema)
                    for schema in offered_schemas
                }
                unoffered_call_ids = {
                    call.call_id
                    for call in calls
                    if has_tool_catalog and call.tool_name not in offered_names
                }
                duplicate_sources: dict[
                    str, tuple[ToolCallRequest, str]
                ] = {}
                unique_calls: list[ToolCallRequest] = []
                seen_call_signatures: dict[str, str] = {}
                for call in calls:
                    signature = json.dumps(
                        [call.tool_name, dict(call.arguments)],
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        default=str,
                    )
                    source_id = seen_call_signatures.get(signature)
                    if source_id is not None:
                        duplicate_sources[call.call_id] = (call, source_id)
                        continue
                    seen_call_signatures[signature] = call.call_id
                    unique_calls.append(call)
                calls = unique_calls
                current_empty_call_signature = (
                    calls[0].tool_name
                    if len(calls) == 1 and not calls[0].arguments
                    else None
                )
                if current_empty_call_signature is not None:
                    if previous_empty_call_signature == current_empty_call_signature:
                        message = (
                            f"Tool '{current_empty_call_signature}' was requested with empty "
                            "arguments 2x in a row; the run stopped to prevent a "
                            "no-progress loop."
                        )
                        error = ErrorInfo(
                            code=ErrorCode.TOOL_INVALID_ARGUMENTS,
                            category="tool",
                            message=message,
                        )
                        await publish(
                            "step.completed",
                            step_completed_payload(
                                step=step + 1,
                                step_started_at=step_started_at,
                            ),
                        )
                        await publish(
                            "run.failed",
                            {"stop_reason": "tool_loop", "error": error.to_dict()},
                        )
                        return RunResult(
                            status="failed",
                            stop_reason="tool_loop",
                            usage=usage,
                            artifacts=tuple(artifacts),
                            error=error,
                        )
                # Keep the assistant turn that produced tool calls in the
                # next model request.  Without this fact, a provider sees a
                # tool result with no corresponding assistant invocation and
                # cannot reliably continue the conversation after step one.
                if calls:
                    assistant_content = content or json.dumps(
                        {
                            "tool_calls": [call.to_dict() for call in calls],
                        },
                        sort_keys=True,
                    )
                    context_items.append(
                        ContextItem(
                            item_id=f"model:{run_id}:{step}",
                            kind="assistant",
                            content=assistant_content,
                            priority=40,
                            metadata={
                                "role": "assistant",
                                "thinking": thinking,
                                "tool_call_ids": [call.call_id for call in calls],
                                "tool_calls": [call.to_dict() for call in calls],
                            },
                        )
                    )
                if not calls:
                    if injected_after_model and step + 1 < max_steps:
                        if content or thinking:
                            context_items.append(
                                ContextItem(
                                    item_id=f"model:{run_id}:{step}:before-injection",
                                    kind="assistant",
                                    content=content,
                                    priority=40,
                                    metadata={
                                        "role": "assistant",
                                        "thinking": thinking,
                                        "injection_continuation": True,
                                    },
                                )
                            )
                        injected_items = [
                            item
                            for item in context_items
                            if item.metadata.get("injected")
                        ]
                        context_items[:] = [
                            item
                            for item in context_items
                            if not item.metadata.get("injected")
                        ]
                        context_items.extend(injected_items)
                        await publish(
                            "step.completed",
                            step_completed_payload(
                                step=step + 1,
                                step_started_at=step_started_at,
                            ),
                        )
                        continue
                    if self._memory is not None:
                        try:
                            await self._memory.flush()
                        except Exception as exc:
                            await publish(
                                "memory.error",
                                {
                                    "phase": "flush",
                                    "message": f"{type(exc).__name__}: {exc}",
                                },
                            )
                        else:
                            await publish("memory.flushed", {"status": "completed"})
                    continuation = await workflow_continuation_for_turn(
                        stop_reason=finish_reason,
                        final_content=content,
                        step=step + 1,
                    )
                    # Workflow continuation decisions mutate durable policy
                    # state (for example Goal Autopilot's continuation and
                    # no-progress counters). Checkpoint after the decision and
                    # before its event becomes externally observable.
                    await self._publish_workflow_checkpoint(publish)
                    if continuation is not None:
                        await publish(
                            "workflow.continuation.requested",
                            {
                                "continuation_id": continuation.continuation_id,
                                "message": continuation.message.to_dict(),
                                "reason": continuation.reason,
                                "metadata": dict(continuation.metadata),
                            },
                        )
                        # Preserve both sides of the synthetic turn in the
                        # next model request.  The event is the durable source
                        # of truth; these in-memory items only avoid a replay
                        # round-trip during the current Run.
                        if content or thinking:
                            context_items.append(
                                ContextItem(
                                    item_id=f"model:{run_id}:{step}:continuation",
                                    kind="assistant",
                                    content=content,
                                    priority=40,
                                    metadata={
                                        "role": "assistant",
                                        "thinking": thinking,
                                        "workflow_continuation": True,
                                    },
                                )
                            )
                        context_items.append(
                            ContextItem(
                                item_id=f"workflow:continuation:{continuation.continuation_id}",
                                kind="user",
                                content=continuation.message.content,
                                priority=100,
                                metadata={
                                    **dict(continuation.message.metadata),
                                    "role": continuation.message.role,
                                    "workflow_continuation": True,
                                    "continuation_id": continuation.continuation_id,
                                },
                            )
                        )
                    if (
                        continuation is None
                        and tool_calls_used > 0
                        and not content.strip()
                    ):
                        attempt = model_recovery_attempts.get(
                            "empty_final_answer", 0
                        )
                        if attempt < 1 and step + 1 < max_steps:
                            attempt += 1
                            model_recovery_attempts["empty_final_answer"] = attempt
                            retry_text = _empty_final_answer_retry_text(
                                tool_calls_used
                            )
                            await publish(
                                "model.recovery.requested",
                                {
                                    "reason": "empty_final_answer",
                                    "attempt": attempt,
                                    "max_attempts": 1,
                                    "message": retry_text,
                                    "tool_call_count": tool_calls_used,
                                },
                            )
                            context_items.append(
                                ContextItem(
                                    item_id=(
                                        f"model:recovery:{run_id}:{step}:"
                                        "empty-final"
                                    ),
                                    kind="user",
                                    content=retry_text,
                                    priority=100,
                                    metadata={
                                        "role": "user",
                                        "model_recovery": True,
                                        "reason": "empty_final_answer",
                                        "attempt": attempt,
                                    },
                                )
                            )
                            await publish(
                                "step.completed",
                                step_completed_payload(
                                    step=step + 1,
                                    step_started_at=step_started_at,
                                ),
                            )
                            continue
                        error = ErrorInfo(
                            code=ErrorCode.MODEL_PROVIDER_ERROR,
                            category="model",
                            message=_EMPTY_FINAL_ANSWER_ERROR,
                            retryable=True,
                            details={"tool_call_count": tool_calls_used},
                        )
                        await publish(
                            "step.completed",
                            step_completed_payload(
                                step=step + 1,
                                step_started_at=step_started_at,
                            ),
                        )
                        await publish(
                            "run.failed",
                            {
                                "stop_reason": "empty_final_answer",
                                "error": error.to_dict(),
                            },
                        )
                        return RunResult(
                            status="failed",
                            stop_reason="empty_final_answer",
                            usage=usage,
                            artifacts=tuple(artifacts),
                            error=error,
                        )
                    await publish(
                        "step.completed",
                        step_completed_payload(
                            step=step + 1,
                            step_started_at=step_started_at,
                        ),
                    )
                    if continuation is not None:
                        continue
                    terminal_content = content
                    terminal_stop_reason = finish_reason
                    boundary_message = await workflow_terminal_message(
                        stop_reason=finish_reason,
                        final_content=content,
                    )
                    if boundary_message is not None:
                        terminal_content = boundary_message
                        terminal_stop_reason = "checkpoint_paused"
                    terminal_metadata = await workflow_terminal_metadata(
                        stop_reason=terminal_stop_reason,
                        final_content=terminal_content,
                    )
                    await publish(
                        "run.completed",
                        {
                            "stop_reason": terminal_stop_reason,
                            "final_content": terminal_content,
                            "metadata": terminal_metadata,
                        },
                    )
                    return RunResult(
                        status="completed",
                        stop_reason=terminal_stop_reason,
                        final_message=terminal_content,
                        usage=usage,
                        artifacts=tuple(artifacts),
                        metadata=terminal_metadata,
                    )

                parallelism = request.options.max_parallel_tools or 1
                begin_tool_decision = getattr(
                    self._workflow, "begin_tool_decision", None
                ) if self._workflow is not None else None
                if callable(begin_tool_decision):
                    try:
                        value = begin_tool_decision(step + 1)
                        if inspect.isawaitable(value):
                            await value
                    except Exception:
                        pass
                evidence_reads_this_step = 0
                pause_message: str | None = None
                results_by_call_id: dict[str, ToolCallResult] = {}
                for offset in range(0, len(calls), parallelism):
                    batch = list(calls[offset : offset + parallelism])

                    prepared_batch: list[
                        tuple[
                            ToolCallRequest,
                            ToolCallResult | None,
                            ToolExecutionContext,
                        ]
                    ] = []
                    for original_call in batch:
                        unoffered = original_call.call_id in unoffered_call_ids
                        policy_error = await workflow_preflight(
                            original_call,
                            parallel=len(batch) > 1,
                        )
                        unknown_tool = unoffered and policy_error is None
                        if unknown_tool:
                            requested_name = requested_tool_names.get(
                                original_call.call_id, original_call.tool_name
                            )
                            explain_unoffered = getattr(
                                self._tools, "unoffered_call_error", None
                            )
                            if callable(explain_unoffered):
                                try:
                                    policy_error = explain_unoffered(requested_name)
                                    if inspect.isawaitable(policy_error):
                                        policy_error = await policy_error
                                except Exception:
                                    policy_error = None
                            if (
                                not isinstance(policy_error, str)
                                or not policy_error.strip()
                            ):
                                policy_error = f"Unknown tool: {requested_name}"
                        budget_exempt = await workflow_exempts_tool_budget(
                            original_call.tool_name
                        )
                        budget_exceeded = False
                        if (
                            policy_error is None
                            and max_tool_calls is not None
                            and not budget_exempt
                            and tool_calls_used >= max_tool_calls
                        ):
                            # Keep the call in the transcript as a synthetic
                            # tool failure so the provider can close the turn
                            # from the evidence it already has.  Workflow
                            # exempt calls remain executable in the same batch.
                            policy_error = "maximum tool calls exceeded"
                            budget_exceeded = True
                        elif policy_error is None and not budget_exempt:
                            tool_calls_used += 1
                        if (
                            policy_error is None
                            and self._workflow is not None
                        ):
                            uses_evidence = getattr(
                                self._workflow,
                                "uses_evidence_read_budget",
                                None,
                            )
                            batch_size = int(
                                getattr(
                                    self._workflow,
                                    "evidence_read_batch_size",
                                    0,
                                )
                                or 0
                            )
                            if callable(uses_evidence) and batch_size > 0:
                                try:
                                    is_evidence = uses_evidence(
                                        original_call.tool_name
                                    )
                                    if inspect.isawaitable(is_evidence):
                                        is_evidence = await is_evidence
                                except Exception:
                                    is_evidence = False
                                if is_evidence:
                                    if evidence_reads_this_step >= batch_size:
                                        policy_error = (
                                            "Public-source page read deferred by "
                                            f"runtime batching (batch size {batch_size})."
                                        )
                                    else:
                                        evidence_reads_this_step += 1
                        prepared_call = original_call
                        if unknown_tool:
                            prepared_call = ToolCallRequest(
                                call_id=original_call.call_id,
                                tool_name=requested_tool_names.get(
                                    original_call.call_id,
                                    original_call.tool_name,
                                ),
                                arguments=original_call.arguments,
                                metadata=original_call.metadata,
                            )
                        early_result: ToolCallResult | None = None
                        if policy_error is not None:
                            early_result = ToolCallResult(
                                call_id=original_call.call_id,
                                status="failed",
                                error=ErrorInfo(
                                    code=(
                                        ErrorCode.TOOL_NOT_FOUND
                                        if unknown_tool
                                        else
                                        ErrorCode.TOOL_TIMEOUT
                                        if budget_exceeded
                                        else ErrorCode.PERMISSION_DENIED
                                    ),
                                    category=(
                                        "tool"
                                        if budget_exceeded or unknown_tool
                                        else "workflow"
                                    ),
                                    message=policy_error,
                                ),
                                permission_decision={
                                    "granted": False,
                                    "reason": policy_error,
                                    "metadata": {
                                        "source": (
                                            "tool_budget"
                                            if budget_exceeded
                                            else "workflow"
                                        )
                                    },
                                },
                            )
                        else:
                            prepared_call = self._bind_tool_call_identity(
                                original_call,
                                run_id=run_id,
                                session_id=request.session_id,
                                runtime_metadata={
                                    "active_skill_hashes": _active_skill_hashes(
                                        context_items
                                    )
                                },
                            )
                            execution_context = ToolExecutionContext(
                                session_id=request.session_id,
                                run_id=run_id,
                                turn_id=request.turn_id,
                                call_id=prepared_call.call_id,
                                tool_name=prepared_call.tool_name,
                                model_context=context,
                                model=str(getattr(self._llm, "model", "") or ""),
                                max_output_tokens=int(
                                    getattr(self._llm, "max_output_tokens", 0) or 0
                                ),
                                metadata=request.metadata,
                                _publish=publish,
                            )
                            prepare = getattr(self._tools, "prepare_call", None)
                            if callable(prepare):
                                try:
                                    value = prepare(
                                        prepared_call,
                                        context=execution_context,
                                    )
                                    if inspect.isawaitable(value):
                                        value = await value
                                    if (
                                        isinstance(value, tuple)
                                        and len(value) == 2
                                        and isinstance(value[0], ToolCallRequest)
                                        and (
                                            value[1] is None
                                            or isinstance(value[1], ToolCallResult)
                                        )
                                    ):
                                        prepared_call, early_result = value
                                    else:
                                        raise TypeError(
                                            "Tool Engine prepare_call must return "
                                            "(ToolCallRequest, ToolCallResult | None)"
                                        )
                                except Exception as exc:
                                    early_result = ToolCallResult(
                                        call_id=prepared_call.call_id,
                                        status="failed",
                                        error=ErrorInfo(
                                            code=ErrorCode.INTERNAL_ERROR,
                                            category="tool",
                                            message=(
                                                "tool preparation failed: "
                                                f"{type(exc).__name__}: {exc}"
                                            ),
                                            details={
                                                "exception_type": type(exc).__name__
                                            },
                                        ),
                                    )
                                except BaseException:
                                    # A process-level termination can happen
                                    # while a permission provider is waiting.
                                    # Preserve the requested call fact before
                                    # propagating the termination so recovery
                                    # can observe the last safe boundary. No
                                    # executor has been entered and no effect
                                    # fence is staged until preparation passes.
                                    prepared_batch.append(
                                        (prepared_call, None, execution_context)
                                    )
                                    await publish(
                                        "tool.call.requested",
                                        prepared_call.to_dict(),
                                    )
                                    raise
                        execution_context = ToolExecutionContext(
                            session_id=request.session_id,
                            run_id=run_id,
                            turn_id=request.turn_id,
                            call_id=prepared_call.call_id,
                            tool_name=prepared_call.tool_name,
                            model_context=context,
                            model=str(getattr(self._llm, "model", "") or ""),
                            max_output_tokens=int(
                                getattr(self._llm, "max_output_tokens", 0) or 0
                            ),
                            metadata=request.metadata,
                            _publish=publish,
                        )
                        prepared_batch.append(
                            (prepared_call, early_result, execution_context)
                        )
                        # Publish only after validation, workflow policy,
                        # permission preflight, hooks, and the initial effect
                        # fence have all completed. The Service commits this
                        # event and staged ``running`` effect in one boundary.
                        await publish("tool.call.requested", prepared_call.to_dict())

                    async def execute_prepared(
                        item: tuple[
                            ToolCallRequest,
                            ToolCallResult | None,
                            ToolExecutionContext,
                        ],
                    ) -> ToolCallResult:
                        call, early_result, execution_context = item
                        if early_result is not None:
                            await record_workflow_result(
                                call, early_result, executed=False
                            )
                            return early_result
                        result = await self._execute_tool(call, execution_context)
                        result = await decorate_workflow_result(call, result)
                        await record_workflow_result(call, result)
                        return result

                    results = await asyncio.gather(
                        *(execute_prepared(item) for item in prepared_batch)
                    )
                    for (call, _early_result, _execution_context), result in zip(
                        prepared_batch, results, strict=True
                    ):
                        results_by_call_id[call.call_id] = result
                        observed_permission_events = permission_event_state.get(
                            call.call_id, set()
                        )
                        if (
                            result.status == "permission_required"
                            and "permission.requested"
                            not in observed_permission_events
                        ):
                            await publish(
                                "permission.requested",
                                {
                                    "call_id": call.call_id,
                                    **dict(result.permission_request or {}),
                                },
                            )
                        if (
                            result.permission_decision is not None
                            and "permission.resolved"
                            not in observed_permission_events
                        ):
                            await publish(
                                "permission.resolved",
                                {
                                    "call_id": call.call_id,
                                    **dict(result.permission_decision),
                                },
                            )
                        completed_payload = result.to_dict()
                        # Tool identity is part of the durable event contract;
                        # ``ToolCallResult`` intentionally only owns the
                        # result envelope.  Persisting these fields here lets
                        # workflow plugins rehydrate evidence and approval
                        # state without importing a provider-specific event.
                        completed_payload.update(
                            {
                                "tool_name": call.tool_name,
                                "arguments": dict(call.arguments),
                                "metadata": dict(call.metadata),
                            }
                        )
                        await record_visible_workflow_evidence(call, result)
                        await publish("tool.call.completed", completed_payload)
                        if pause_message is None:
                            pause_message = await workflow_pause_after_tool(call, result)
                        for artifact in _artifact_payloads(result.output):
                            artifact_payload = dict(artifact)
                            if self._artifact_processor is not None:
                                process_artifact = getattr(
                                    self._artifact_processor,
                                    "process",
                                    None,
                                )
                                if not callable(process_artifact):
                                    raise TypeError(
                                        "artifact processor must provide process(request)"
                                    )
                                artifact_payload = process_artifact(
                                    ArtifactPublishRequest(
                                        session_id=request.session_id,
                                        run_id=run_id,
                                        turn_id=request.turn_id,
                                        call_id=call.call_id,
                                        artifact=artifact_payload,
                                        metadata=request.metadata,
                                    )
                                )
                                if inspect.isawaitable(artifact_payload):
                                    artifact_payload = await artifact_payload
                                if not isinstance(artifact_payload, Mapping):
                                    raise TypeError(
                                        "artifact processor must return a mapping"
                                    )
                                artifact_payload = dict(artifact_payload)
                            normalized_artifact = _artifact_from_payload(artifact_payload)
                            if normalized_artifact is not None:
                                artifacts.append(normalized_artifact)
                            await publish(
                                "artifact.created",
                                {"call_id": call.call_id, **artifact_payload},
                            )
                        context_items.append(
                            ContextItem(
                                item_id=f"tool:{call.call_id}",
                                kind="tool",
                                content=result.content
                                or (result.error.message if result.error else ""),
                                priority=20,
                                metadata={
                                    "role": "tool",
                                    "tool_call_id": call.call_id,
                                    "name": call.tool_name,
                                },
                            )
                        )

                    if pause_message is not None:
                        # A workflow pause is a durable boundary.  Do not ask
                        # the provider for another turn until the host sends a
                        # control command and starts a fresh Run.
                        break

                for duplicate_id, (duplicate_call, source_id) in duplicate_sources.items():
                    source_result = results_by_call_id.get(source_id)
                    if source_result is None:
                        continue
                    duplicate_request = ToolCallRequest(
                        call_id=duplicate_id,
                        tool_name=duplicate_call.tool_name,
                        arguments=duplicate_call.arguments,
                        metadata={
                            **dict(duplicate_call.metadata),
                            "duplicate_of": source_id,
                            "user_visible": False,
                        },
                    )
                    await publish("tool.call.requested", duplicate_request.to_dict())
                    duplicate_payload = source_result.to_dict()
                    duplicate_payload.update(
                        {
                            "call_id": duplicate_id,
                            "tool_name": duplicate_call.tool_name,
                            "arguments": dict(duplicate_call.arguments),
                            "duplicate_of": source_id,
                            "user_visible": False,
                        }
                    )
                    await publish("tool.call.completed", duplicate_payload)
                    context_items.append(
                        ContextItem(
                            item_id=f"tool:{duplicate_id}",
                            kind="tool",
                            content=source_result.content
                            or (
                                source_result.error.message
                                if source_result.error
                                else ""
                            ),
                            priority=20,
                            metadata={
                                "role": "tool",
                                "tool_call_id": duplicate_id,
                                "name": duplicate_call.tool_name,
                                "duplicate_of": source_id,
                            },
                        )
                    )

                if current_empty_call_signature is not None and calls:
                    empty_result = results_by_call_id.get(calls[0].call_id)
                    previous_empty_call_signature = (
                        current_empty_call_signature
                        if empty_result is not None
                        and empty_result.status != "succeeded"
                        else None
                    )
                else:
                    previous_empty_call_signature = None

                await drain_controls()
                await publish(
                    "step.completed",
                    step_completed_payload(
                        step=step + 1,
                        step_started_at=step_started_at,
                    ),
                )
                if pause_message is not None:
                    await self._publish_workflow_checkpoint(publish)
                    terminal_metadata = await workflow_terminal_metadata(
                        stop_reason="checkpoint_paused",
                        final_content=pause_message,
                    )
                    await publish(
                        "run.completed",
                        {
                            "stop_reason": "checkpoint_paused",
                            "final_content": pause_message,
                            "metadata": terminal_metadata,
                        },
                    )
                    return RunResult(
                        status="completed",
                        stop_reason="checkpoint_paused",
                        final_message=pause_message,
                        usage=usage,
                        artifacts=tuple(artifacts),
                        metadata=terminal_metadata,
                    )

            error = ErrorInfo(
                code=ErrorCode.INTERNAL_ERROR,
                category="loop",
                message="maximum loop steps exceeded",
            )
            await publish("run.failed", {"error": error.to_dict()})
            return RunResult(
                status="failed",
                stop_reason="max_steps",
                usage=usage,
                artifacts=tuple(artifacts),
                error=error,
            )
        except Exception as exc:
            error = ErrorInfo(
                code=ErrorCode.INTERNAL_ERROR,
                category="loop",
                message=f"agent loop failed: {type(exc).__name__}: {exc}",
                details={"exception_type": type(exc).__name__},
            )
            await publish("run.failed", {"error": error.to_dict()})
            return RunResult(
                status="failed",
                stop_reason="error",
                usage=usage,
                artifacts=tuple(artifacts),
                error=error,
            )
        finally:
            end_run = getattr(self._tools, "end_run", None)
            if callable(end_run):
                try:
                    cleanup = end_run(
                        session_id=request.session_id,
                        run_id=run_id,
                        metadata=dict(request.metadata),
                    )
                    if inspect.isawaitable(cleanup):
                        await cleanup
                except Exception:
                    # The terminal fact may already be durable. Cleanup must
                    # still be attempted on every path, while a cleanup fault
                    # must not rewrite a completed user result after the fact.
                    logging.getLogger(__name__).exception(
                        "tool run cleanup failed: session=%s run=%s",
                        request.session_id,
                        run_id,
                    )
            if control_task is not None:
                control_task.cancel()
                await asyncio.gather(control_task, return_exceptions=True)
            if deadline_task is not None:
                deadline_task.cancel()
                await asyncio.gather(deadline_task, return_exceptions=True)

    def _tool_schemas(self) -> tuple[Mapping[str, Any], ...]:
        if self._tools is None:
            return ()
        schemas = getattr(self._tools, "schemas", None)
        if callable(schemas):
            return tuple(schemas())
        return ()

    def _tool_schemas_for_workflow(self) -> tuple[Mapping[str, Any], ...]:
        """Apply optional workflow-owned tool visibility to model schemas.

        The Kernel never names a concrete workflow.  A policy may hide tools
        for a stage (for example, presentation research) or provide a generic
        ``filter_tool_schemas`` hook for a vendor-specific catalog.  Invalid
        filters fail open to the unmodified schema list so an extension cannot
        corrupt the loop's protocol.
        """

        schemas = self._tool_schemas()
        policy = self._workflow
        if policy is None:
            return schemas
        filter_tools = getattr(policy, "filter_tool_schemas", None)
        if callable(filter_tools):
            try:
                value = filter_tools(schemas)
                if inspect.isawaitable(value):
                    # Schema collection is synchronous by contract.  Async
                    # policies can still use ``hidden_tool_names`` below.
                    value = None
                if isinstance(value, (list, tuple)) and all(
                    isinstance(item, Mapping) for item in value
                ):
                    schemas = tuple(dict(item) for item in value)
            except Exception:
                pass
        hidden_method = getattr(policy, "hidden_tool_names", None)
        if callable(hidden_method):
            try:
                hidden = hidden_method()
                if inspect.isawaitable(hidden):
                    hidden = ()
                hidden_names = {
                    str(name)
                    for name in (hidden or ())
                    if isinstance(name, str) and name.strip()
                }
            except Exception:
                hidden_names = set()
            if hidden_names:
                def schema_name(schema: Mapping[str, Any]) -> str:
                    name = schema.get("name")
                    if isinstance(name, str) and name:
                        return name
                    function = schema.get("function")
                    if isinstance(function, Mapping):
                        return str(function.get("name", "") or "")
                    return ""

                schemas = tuple(
                    schema
                    for schema in schemas
                    if schema_name(schema) not in hidden_names
                )
        required_method = getattr(policy, "required_tool_names", None)
        if callable(required_method):
            try:
                required = required_method()
                if inspect.isawaitable(required):
                    required = ()
                required_names = {
                    str(name)
                    for name in (required or ())
                    if isinstance(name, str) and name.strip()
                }
                restrict = bool(
                    getattr(policy, "restrict_tools_until_required_succeed", False)
                )
                if restrict and required_names:
                    passthrough_names: set[str] = set()
                    passthrough_method = getattr(
                        self._tools,
                        "restricted_passthrough_tool_names",
                        None,
                    )
                    if callable(passthrough_method):
                        try:
                            passthrough = passthrough_method()
                            if not inspect.isawaitable(passthrough):
                                passthrough_names = {
                                    str(name)
                                    for name in (passthrough or ())
                                    if isinstance(name, str) and name.strip()
                                }
                        except Exception:
                            passthrough_names = set()
                    allowed = required_names | {"tool_search"} | passthrough_names
                    schemas = tuple(
                        schema
                        for schema in schemas
                        if (
                            str(schema.get("name", "") or "")
                            or (
                                str(schema.get("function", {}).get("name", "") or "")
                                if isinstance(schema.get("function"), Mapping)
                                else ""
                            )
                        ) in allowed
                    )
            except Exception:
                pass
        return schemas

    async def _execute_tool(
        self,
        call: ToolCallRequest,
        context: ToolExecutionContext,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> ToolCallResult:
        if self._tools is None:
            return ToolCallResult(
                call_id=call.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.TOOL_NOT_FOUND,
                    category="tool",
                    message="no Tool Engine is configured",
                ),
            )
        execute = getattr(self._tools, "execute", None)
        if not callable(execute):
            raise TypeError("tool_engine must provide execute(call, context=...)")
        bound_session_id = session_id or str(call.metadata.get("session_id", ""))
        if run_id or bound_session_id:
            call = self._bind_tool_call_identity(
                call,
                run_id=run_id or str(call.metadata.get("run_id", "")),
                session_id=bound_session_id,
            )
        result = execute(call, context=context)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ToolCallResult):
            raise TypeError("Tool Engine must return api.ToolCallResult")
        return result

    @staticmethod
    def _bind_tool_call_identity(
        call: ToolCallRequest,
        *,
        run_id: str,
        session_id: str,
        runtime_metadata: Mapping[str, Any] | None = None,
    ) -> ToolCallRequest:
        """Attach deterministic runtime/effect identity without overwriting host keys."""

        metadata = {
            **dict(call.metadata),
            **dict(runtime_metadata or {}),
            "session_id": session_id,
            "run_id": run_id,
            "effect_id": f"{run_id}:{call.call_id}",
            "idempotency_key": f"{call.tool_name}:{call.call_id}",
        }
        return ToolCallRequest(
            call_id=call.call_id,
            tool_name=call.tool_name,
            arguments=call.arguments,
            metadata=metadata,
        )
    async def _publish_workflow_checkpoint(
        self,
        publish: Callable[[str, Mapping[str, Any]], Awaitable[None]],
    ) -> None:
        """Expose an optional workflow checkpoint without importing workflows.

        Concrete workflow plugins may implement ``build_checkpoint``.  The
        method is deliberately duck-typed so the kernel remains independent of
        the legacy workflow classes and their filesystem stores.
        """

        if self._workflow is None:
            return
        build_checkpoint = getattr(self._workflow, "build_checkpoint", None)
        if not callable(build_checkpoint):
            return
        try:
            checkpoint = build_checkpoint()
            if inspect.isawaitable(checkpoint):
                checkpoint = await checkpoint
        except Exception:
            return
        if checkpoint is None:
            return
        payload: dict[str, Any] = {"text": str(checkpoint)}
        build_payload = getattr(self._workflow, "build_checkpoint_payload", None)
        if callable(build_payload):
            try:
                state = build_payload()
                if inspect.isawaitable(state):
                    state = await state
                if isinstance(state, Mapping):
                    payload["workflow_state"] = dict(state)
            except Exception:
                # Structured state is an optional persistence aid.  A faulty
                # serializer must never suppress the human-readable checkpoint.
                pass
        await publish("workflow.checkpoint", payload)

    async def workflow_checkpoint_payload(self) -> Mapping[str, Any] | None:
        """Return the current workflow snapshot for a durable boundary.

        ``KernelAgentService`` invokes this optional hook while constructing a
        checkpoint.  Keeping it on the generic Kernel avoids reaching into a
        concrete Goal/Plan implementation, and means a crash immediately
        after any workflow tool result can still recover the latest state.
        """

        if self._workflow is None:
            return None
        build_payload = getattr(self._workflow, "build_checkpoint_payload", None)
        if not callable(build_payload):
            return None
        value = build_payload()
        if inspect.isawaitable(value):
            value = await value
        return dict(value) if isinstance(value, Mapping) else None

    def pending_effects_for_event(
        self, event: AgentEvent
    ) -> tuple[tuple[str, str, str, dict[str, Any] | None, str, str, str], ...]:
        """Expose staged Tool Engine effect transitions to the Service.

        The Kernel does not know a concrete persistence implementation.  It
        only forwards an optional, data-only transition from the Tool Engine
        so ``KernelAgentService`` can commit it with the event boundary.
        """

        if event.type not in {"tool.call.requested", "tool.call.completed"}:
            return ()
        call_id = str(event.payload.get("call_id", ""))
        if not call_id or self._tools is None:
            return ()
        provider = getattr(self._tools, "pending_effects_for_call", None)
        if not callable(provider):
            return ()
        value = provider(call_id)
        if inspect.isawaitable(value):
            # This hook is intentionally synchronous so the Service can build
            # a transaction payload before entering its async commit call.
            # Async providers should expose a synchronous snapshot instead.
            return ()
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(
            item
            for item in value
            if isinstance(item, (list, tuple)) and len(item) == 7
        )

    def acknowledge_effect_boundary(self, event: AgentEvent) -> None:
        """Clear staged effect state after its durable event commits."""

        if event.type not in {"tool.call.requested", "tool.call.completed"} or self._tools is None:
            return
        call_id = str(event.payload.get("call_id", ""))
        acknowledge = getattr(self._tools, "acknowledge_effect_boundary", None)
        if call_id and callable(acknowledge):
            try:
                acknowledge(call_id, event_type=event.type)
            except TypeError as exc:
                if "event_type" not in str(exc):
                    raise
                acknowledge(call_id)

    async def finalize_effect_boundary(self, event: AgentEvent) -> None:
        """Finalize a staged effect when using a legacy EventLog port."""

        if event.type not in {"tool.call.requested", "tool.call.completed"} or self._tools is None:
            return
        call_id = str(event.payload.get("call_id", ""))
        finalize = getattr(self._tools, "finalize_effect_boundary", None)
        if call_id and callable(finalize):
            try:
                value = finalize(call_id, event_type=event.type)
            except TypeError as exc:
                if "event_type" not in str(exc):
                    raise
                value = finalize(call_id)
            if inspect.isawaitable(value):
                await value


def _artifact_payloads(output: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    """Extract stable artifact descriptors from a tool result, if present."""

    if not isinstance(output, Mapping):
        return ()
    value = output.get("artifacts", output.get("artifact"))
    if value is None:
        return ()
    values = value if isinstance(value, (list, tuple)) else (value,)
    return tuple(dict(item) for item in values if isinstance(item, Mapping))


def _artifact_from_payload(payload: Mapping[str, Any]) -> Artifact | None:
    uri = str(payload.get("uri", payload.get("abs_path", "")) or "")
    if not uri:
        return None
    return Artifact(
        artifact_id=str(payload.get("artifact_id", payload.get("filename", uri))),
        kind=str(payload.get("kind", "file") or "file"),
        uri=uri,
        mime=str(payload.get("mime", "application/octet-stream")),
        size=int(payload.get("size", -1) or -1),
        sha256=str(payload.get("sha256", "") or ""),
    )


__all__ = ["AgentLoopKernel"]
