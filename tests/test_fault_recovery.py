"""Process-level fault injection tests for resumable native Kernel runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from box_agent.api import (
    ContextBuildResult,
    MemoryRecall,
    Message,
    ModelChunk,
    PermissionDecision,
    PermissionRequest,
    RunRequest,
    SessionOpenRequest,
    ToolCallRequest,
    WorkflowContinuation,
)
from box_agent.kernel import AgentLoopKernel
from box_agent.persistence import EffectStatus, SQLiteEffectLedger, SQLiteEventLog
from box_agent.plugins import TypedRegistry
from box_agent.services.kernel import KernelAgentService
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.engine import RegistryToolEngine


class Crash(BaseException):
    """Simulate a process terminating outside normal exception handling."""


def _request(session_id: str, request_id: str) -> RunRequest:
    return RunRequest(
        request_id=request_id,
        session_id=session_id,
        turn_id=f"{request_id}-turn",
        user_input=Message.user("continue after crash"),
    )


async def _start(service: KernelAgentService, request: RunRequest):
    await service.open_session(SessionOpenRequest(session_id=request.session_id))
    return await service.start(request)


def test_model_request_crash_resumes_from_last_checkpoint(tmp_path: Path) -> None:
    state = {"crashed": False}

    class LLM:
        async def stream(self, request):
            if not state["crashed"]:
                state["crashed"] = True
                raise Crash("model worker terminated")
            yield ModelChunk(content="resumed", finish_reason="stop")

    def factory(request, bundle=None):
        del request, bundle
        return AgentLoopKernel(llm=LLM())

    async def scenario() -> None:
        db_path = tmp_path / "model-crash.sqlite3"
        log = SQLiteEventLog(db_path)
        service = KernelAgentService(kernel_factory=factory, event_log=log)
        handle = await _start(service, _request("model-session", "model-request"))
        with pytest.raises(Crash):
            await handle.wait()
        crashed_events = await log.load_recovery_bundle(handle.run_id)
        assert any(event.type == "model.requested" for event in crashed_events.events)
        assert not any(
            event.type == "model.response.completed" for event in crashed_events.events
        )
        log.close()

        resumed_log = SQLiteEventLog(db_path)
        resumed_service = KernelAgentService(
            kernel_factory=factory,
            event_log=resumed_log,
        )
        resumed = await resumed_service.resume(handle.run_id)
        result = await resumed.wait()
        assert result.status == "completed"
        assert result.final_message == "resumed"
        resumed_log.close()

    import asyncio

    asyncio.run(scenario())


def test_context_assembly_crash_resumes_same_turn(tmp_path: Path) -> None:
    state = {"crashed": False}

    class Context:
        async def assemble(self, request):
            if not state["crashed"]:
                state["crashed"] = True
                raise Crash("context worker terminated")
            return ContextBuildResult(
                items=request.items,
                estimated_tokens=sum(item.estimated_tokens for item in request.items),
            )

        async def restore(self, *, resource_id: str, content_version: str | None = None):
            del resource_id, content_version
            return None

    class LLM:
        async def stream(self, request):
            yield ModelChunk(content="context resumed", finish_reason="stop")

    def factory(request, bundle=None):
        del request, bundle
        return AgentLoopKernel(llm=LLM(), context_engine=Context())

    async def scenario() -> None:
        db_path = tmp_path / "context-crash.sqlite3"
        log = SQLiteEventLog(db_path)
        service = KernelAgentService(kernel_factory=factory, event_log=log)
        handle = await _start(service, _request("context-session", "context-request"))
        with pytest.raises(Crash):
            await handle.wait()
        crashed_events = await log.load_recovery_bundle(handle.run_id)
        assert any(event.type == "step.started" for event in crashed_events.events)
        assert not any(event.type == "context.assembled" for event in crashed_events.events)
        log.close()

        resumed_log = SQLiteEventLog(db_path)
        resumed_service = KernelAgentService(
            kernel_factory=factory,
            event_log=resumed_log,
        )
        resumed = await resumed_service.resume(handle.run_id)
        assert (await resumed.wait()).status == "completed"
        resumed_log.close()

    import asyncio

    asyncio.run(scenario())


def test_permission_wait_crash_retries_without_executing_early(tmp_path: Path) -> None:
    state = {"crashed": False, "llm_calls": 0, "executions": 0}

    class ProtectedTool(Tool):
        @property
        def name(self) -> str:
            return "protected"

        @property
        def description(self) -> str:
            return "protected"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def preflight(self, arguments, *, context=None):
            del arguments, context
            return ToolResult(
                success=False,
                permission_request={"scope": "test", "reason": "approve"},
            )

        async def execute(self) -> ToolResult:
            state["executions"] += 1
            return ToolResult(success=True, content="authorized")

    class Gateway:
        async def decide(self, request: PermissionRequest) -> PermissionDecision:
            del request
            if not state["crashed"]:
                state["crashed"] = True
                raise Crash("permission worker terminated")
            return PermissionDecision(granted=True, reason="approved")

    class LLM:
        async def stream(self, request):
            del request
            state["llm_calls"] += 1
            if state["llm_calls"] <= 2:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id="permission-call",
                            tool_name="protected",
                            arguments={},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield ModelChunk(content="permission resumed", finish_reason="stop")

    def factory(request, bundle=None):
        del request, bundle
        registry = TypedRegistry("tools")
        registry.register("protected", ProtectedTool(), source="test")
        return AgentLoopKernel(
            llm=LLM(),
            tool_engine=RegistryToolEngine(registry, permission_gateway=Gateway()),
        )

    async def scenario() -> None:
        db_path = tmp_path / "permission-crash.sqlite3"
        log = SQLiteEventLog(db_path)
        service = KernelAgentService(kernel_factory=factory, event_log=log)
        handle = await _start(service, _request("permission-session", "permission-request"))
        with pytest.raises(Crash):
            await handle.wait()
        crashed_events = await log.load_recovery_bundle(handle.run_id)
        assert any(event.type == "tool.call.requested" for event in crashed_events.events)
        assert not any(event.type == "permission.resolved" for event in crashed_events.events)
        assert state["executions"] == 0
        log.close()

        resumed_log = SQLiteEventLog(db_path)
        resumed_service = KernelAgentService(
            kernel_factory=factory,
            event_log=resumed_log,
        )
        resumed = await resumed_service.resume(handle.run_id)
        result = await resumed.wait()
        assert result.status == "completed"
        assert state["executions"] == 1
        assert any(
            event.type == "permission.resolved"
            for event in (await resumed_log.load_recovery_bundle(handle.run_id)).events
        )
        resumed_log.close()

    import asyncio

    asyncio.run(scenario())


def test_tool_crash_effect_fence_prevents_duplicate_execution(tmp_path: Path) -> None:
    state = {"crashed": False, "llm_calls": 0, "executions": 0}
    active_ledger: SQLiteEffectLedger | None = None

    class EffectTool(Tool):
        @property
        def name(self) -> str:
            return "effect_tool"

        @property
        def description(self) -> str:
            return "effect tool"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self) -> ToolResult:
            state["executions"] += 1
            if not state["crashed"]:
                state["crashed"] = True
                raise Crash("tool worker terminated")
            return ToolResult(success=True, content="effect done")

    class LLM:
        async def stream(self, request):
            del request
            state["llm_calls"] += 1
            if state["llm_calls"] <= 2:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id="effect-call",
                            tool_name="effect_tool",
                            arguments={},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield ModelChunk(content="effect resumed", finish_reason="stop")

    def factory(request, bundle=None):
        del request, bundle
        registry = TypedRegistry("tools")
        registry.register("effect_tool", EffectTool(), source="test")
        return AgentLoopKernel(
            llm=LLM(),
            tool_engine=RegistryToolEngine(
                registry,
                effect_ledger=active_ledger,
                defer_effect_completion=True,
            ),
        )

    async def scenario() -> None:
        nonlocal active_ledger
        db_path = tmp_path / "tool-crash.sqlite3"
        log = SQLiteEventLog(db_path)
        active_ledger = SQLiteEffectLedger(db_path)
        service = KernelAgentService(kernel_factory=factory, event_log=log)
        handle = await _start(service, _request("tool-session", "tool-request"))
        with pytest.raises(Crash):
            await handle.wait()
        effect_id = f"{handle.run_id}:effect-call"
        effect = await active_ledger.reconcile(effect_id)
        assert effect is not None and effect.status is EffectStatus.RUNNING
        log.close()
        active_ledger.close()

        resumed_log = SQLiteEventLog(db_path)
        active_ledger = SQLiteEffectLedger(db_path)
        resumed_service = KernelAgentService(
            kernel_factory=factory,
            event_log=resumed_log,
        )
        resumed = await resumed_service.resume(handle.run_id)
        result = await resumed.wait()
        assert result.status == "completed"
        # The second model call reissues the same idempotency identity, but the
        # unresolved running effect is rejected before the executor is entered.
        assert state["executions"] == 1
        assert any(
            event.payload.get("error", {}).get("code")
            == "EFFECT_REQUIRES_RECONCILIATION"
            for event in (await resumed_log.load_recovery_bundle(handle.run_id)).events
            if event.type == "tool.call.completed"
        )
        resumed_log.close()
        active_ledger.close()

    import asyncio

    asyncio.run(scenario())


def test_memory_flush_crash_retries_post_response_lifecycle(tmp_path: Path) -> None:
    state = {"crashed": False, "flushes": 0}

    class Memory:
        async def recall(self, query):
            return MemoryRecall(entries=(), query=query)

        async def write(self, entry):
            return entry

        async def flush(self):
            state["flushes"] += 1
            if not state["crashed"]:
                state["crashed"] = True
                raise Crash("memory worker terminated")

    class LLM:
        async def stream(self, request):
            yield ModelChunk(content="memory resumed", finish_reason="stop")

    def factory(request, bundle=None):
        del request, bundle
        return AgentLoopKernel(llm=LLM(), memory_engine=Memory())

    async def scenario() -> None:
        db_path = tmp_path / "memory-crash.sqlite3"
        log = SQLiteEventLog(db_path)
        service = KernelAgentService(kernel_factory=factory, event_log=log)
        handle = await _start(service, _request("memory-session", "memory-request"))
        with pytest.raises(Crash):
            await handle.wait()
        crashed_events = await log.load_recovery_bundle(handle.run_id)
        assert any(
            event.type == "model.response.completed" for event in crashed_events.events
        )
        assert not any(event.type == "memory.flushed" for event in crashed_events.events)
        log.close()

        resumed_log = SQLiteEventLog(db_path)
        resumed_service = KernelAgentService(
            kernel_factory=factory,
            event_log=resumed_log,
        )
        resumed = await resumed_service.resume(handle.run_id)
        result = await resumed.wait()
        assert result.status == "completed"
        assert state["flushes"] == 2
        assert any(
            event.type == "memory.flushed"
            for event in (await resumed_log.load_recovery_bundle(handle.run_id)).events
        )
        resumed_log.close()

    import asyncio

    asyncio.run(scenario())


def test_continuation_boundary_crash_resumes_next_step_without_repeating_turn(
    tmp_path: Path,
) -> None:
    state = {"llm_calls": 0, "continuation_calls": 0, "crashed": False}

    class CrashAfterContinuation(SQLiteEventLog):
        async def commit_checkpoint(self, checkpoint, *, events=(), outbox=None, effects=()):
            if (
                checkpoint.state.get("last_event_type") == "step.completed"
                and not state["crashed"]
            ):
                state["crashed"] = True
                raise Crash("step boundary worker terminated")
            return await super().commit_checkpoint(
                checkpoint,
                events=events,
                outbox=outbox,
                effects=effects,
            )

    class LLM:
        async def stream(self, request):
            del request
            state["llm_calls"] += 1
            yield ModelChunk(
                content="first" if state["llm_calls"] == 1 else "resumed",
                finish_reason="end_turn",
            )

    class Policy:
        def next_continuation(self, *, stop_reason, final_content, step):
            del final_content, step
            if stop_reason != "end_turn" or state["continuation_calls"]:
                return None
            state["continuation_calls"] += 1
            return WorkflowContinuation(
                continuation_id="crash:1",
                message=Message.user("resume the workflow"),
                reason="test",
            )

    def factory(request, bundle=None):
        del request, bundle
        return AgentLoopKernel(llm=LLM(), workflow_policy=Policy())

    async def scenario() -> None:
        db_path = tmp_path / "continuation-crash.sqlite3"
        log = CrashAfterContinuation(db_path)
        service = KernelAgentService(kernel_factory=factory, event_log=log)
        handle = await _start(service, _request("continuation-session", "continuation-request"))
        with pytest.raises(Crash):
            await handle.wait()
        crashed = await log.load_recovery_bundle(handle.run_id)
        assert any(
            event.type == "workflow.continuation.requested" for event in crashed.events
        )
        assert not any(event.type == "step.completed" for event in crashed.events)
        log.close()

        resumed_log = SQLiteEventLog(db_path)
        resumed_service = KernelAgentService(
            kernel_factory=factory,
            event_log=resumed_log,
        )
        resumed = await resumed_service.resume(handle.run_id)
        result = await resumed.wait()
        assert result.status == "completed"
        assert result.final_message == "resumed"
        assert state["llm_calls"] == 2
        resumed_log.close()

    import asyncio

    asyncio.run(scenario())
