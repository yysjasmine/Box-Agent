"""Regression tests for the durable event/checkpoint/outbox boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from box_agent.api import (
    AgentEvent,
    Message,
    ModelChunk,
    RunRequest,
    RunResult,
    SessionOpenRequest,
    ToolCallRequest,
)
from box_agent.persistence import (
    EffectStatus,
    PersistenceConflictError,
    SQLiteEffectLedger,
    SQLiteEventLog,
)
from box_agent.plugins import PluginHost
from box_agent.services.kernel import KernelAgentService
from box_agent.tools.base import Tool, ToolResult


def _request() -> RunRequest:
    return RunRequest(
        request_id="durable-request",
        session_id="durable-session",
        turn_id="durable-turn",
        user_input=Message.user("hello"),
    )


class _CompletedKernel:
    async def run(self, request, *, emit, cancel_event, controls=None):
        await emit(
            AgentEvent(
                event_id="durable-done",
                sequence=1,
                session_id=request.session_id,
                run_id=request.metadata["run_id"],
                turn_id=request.turn_id,
                type="run.completed",
                payload={"stop_reason": "done", "final_content": "ok"},
            )
        )
        return RunResult(status="completed", stop_reason="done", final_message="ok")


def test_event_checkpoint_and_outbox_commit_as_one_durable_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        log = SQLiteEventLog(tmp_path / "durable.sqlite3")
        service = KernelAgentService(kernel_factory=_CompletedKernel, event_log=log)
        await service.open_session(SessionOpenRequest(session_id="durable-session"))
        handle = await service.start(_request())
        await handle.wait()

        bundle = await log.load_recovery_bundle(handle.run_id)
        assert bundle.checkpoint is not None
        assert bundle.checkpoint.sequence == 1
        outbox = await log.outbox_after(handle.run_id, sequence=0)
        assert [message.event_id for message in outbox] == ["durable-done"]
        assert outbox[0].status == "pending"
        log.close()

    asyncio.run(scenario())


def test_failed_boundary_commit_does_not_leave_an_observable_event(tmp_path: Path) -> None:
    class FailingBoundaryLog(SQLiteEventLog):
        async def commit_checkpoint(self, checkpoint, *, events=(), outbox=()):
            raise RuntimeError("simulated transaction failure")

    async def scenario() -> None:
        log = FailingBoundaryLog(tmp_path / "durable-failure.sqlite3")
        service = KernelAgentService(kernel_factory=_CompletedKernel, event_log=log)
        await service.open_session(SessionOpenRequest(session_id="durable-session"))
        handle = await service.start(_request())
        await handle.wait()

        # The service may expose a failed terminal result, but the transition
        # event must not be visible in the durable log after the atomic commit
        # failed.
        bundle = await log.load_recovery_bundle(handle.run_id)
        assert [event.type for event in bundle.events] == ["run.failed"]
        assert all(event.type != "run.completed" for event in bundle.events)
        assert [message.event.type for message in await log.outbox_after(handle.run_id)] == [
            "run.failed"
        ]
        log.close()

    asyncio.run(scenario())


def test_non_checkpoint_events_are_also_recoverable_from_outbox(tmp_path: Path) -> None:
    class StreamingKernel:
        async def run(self, request, *, emit, cancel_event, controls=None):
            run_id = request.metadata["run_id"]
            await emit(
                AgentEvent(
                    event_id="stream-delta",
                    sequence=1,
                    session_id=request.session_id,
                    run_id=run_id,
                    turn_id=request.turn_id,
                    type="model.content.delta",
                    payload={"content": "partial"},
                )
            )
            await emit(
                AgentEvent(
                    event_id="stream-done",
                    sequence=2,
                    session_id=request.session_id,
                    run_id=run_id,
                    turn_id=request.turn_id,
                    type="run.completed",
                    payload={"stop_reason": "done", "final_content": "partial"},
                )
            )
            return RunResult(status="completed", stop_reason="done", final_message="partial")

    async def scenario() -> None:
        log = SQLiteEventLog(tmp_path / "durable-stream.sqlite3")
        service = KernelAgentService(kernel_factory=StreamingKernel, event_log=log)
        await service.open_session(SessionOpenRequest(session_id="durable-session"))
        handle = await service.start(_request())
        await handle.wait()

        outbox = await log.outbox_after(handle.run_id)
        assert [message.event.type for message in outbox] == [
            "model.content.delta",
            "run.completed",
        ]
        published = await log.mark_outbox_published(outbox[0].outbox_id)
        assert published is not None and published.status == "published"
        await log.append_with_outbox(outbox[0].event, outbox=outbox[0])
        assert (await log.outbox_after(handle.run_id))[0].status == "published"
        assert [event.type for event in (await log.load_recovery_bundle(handle.run_id)).events] == [
            "model.content.delta",
            "run.completed",
        ]
        log.close()

    asyncio.run(scenario())


def test_outbox_id_reuse_with_different_event_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        from box_agent.persistence import OutboxRecord

        log = SQLiteEventLog(tmp_path / "outbox-conflict.sqlite3")
        first = AgentEvent(
            event_id="event-one",
            sequence=1,
            session_id="session",
            run_id="run",
            turn_id="turn",
            type="model.content.delta",
            payload={"content": "one"},
        )
        second = AgentEvent(
            event_id="event-two",
            sequence=2,
            session_id="session",
            run_id="run",
            turn_id="turn",
            type="model.content.delta",
            payload={"content": "two"},
        )
        await log.append_with_outbox(
            first, outbox=OutboxRecord("shared-outbox", first)
        )
        with pytest.raises(PersistenceConflictError):
            await log.append_with_outbox(
                second, outbox=OutboxRecord("shared-outbox", second)
            )
        log.close()

    asyncio.run(scenario())


def test_sqlite_checkpoint_transaction_rolls_back_event_when_outbox_fails(
    tmp_path: Path,
) -> None:
    class FailingOutboxLog(SQLiteEventLog):
        def _insert_outbox_locked(self, message):
            raise RuntimeError("simulated outbox failure")

    async def scenario() -> None:
        log = FailingOutboxLog(tmp_path / "durable-rollback.sqlite3")
        event = AgentEvent(
            event_id="rollback-event",
            sequence=1,
            session_id="session-rollback",
            run_id="run-rollback",
            turn_id="turn-rollback",
            type="run.completed",
            payload={"stop_reason": "done"},
        )
        from box_agent.services.kernel import _event_hash
        from box_agent.persistence import RunCheckpoint

        checkpoint = RunCheckpoint(
            checkpoint_id="run-rollback:1",
            session_id="session-rollback",
            run_id="run-rollback",
            sequence=1,
            state={"terminal": True},
            event_hash=_event_hash(event),
        )
        try:
            await log.commit_checkpoint(
                checkpoint,
                events=(event,),
                outbox=(OutboxRecord("run-rollback:1", event),),
            )
        except RuntimeError:
            pass
        else:  # pragma: no cover
            raise AssertionError("outbox failure must abort checkpoint transaction")
        bundle = await log.load_recovery_bundle("run-rollback")
        assert bundle.events == ()
        assert bundle.checkpoint is None
        assert await log.outbox_after("run-rollback") == ()
        log.close()

    from box_agent.persistence import OutboxRecord

    asyncio.run(scenario())


def test_effect_terminal_transition_commits_with_tool_completion_boundary(
    tmp_path: Path,
) -> None:
    """A completed side effect and its Tool event must become visible together."""

    class LLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id="effect-call",
                            tool_name="echo",
                            arguments={"text": "ok"},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield ModelChunk(content="finished", finish_reason="stop")

    class EchoTool(Tool):
        @property
        def name(self) -> str:
            return "echo"

        @property
        def description(self) -> str:
            return "echo"

        @property
        def parameters(self) -> dict:
            return {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            }

        async def execute(self, *, text: str) -> ToolResult:
            return ToolResult(success=True, content=text)

    async def scenario() -> None:
        db_path = tmp_path / "effect-boundary.sqlite3"
        event_log = SQLiteEventLog(db_path)
        effect_ledger = SQLiteEffectLedger(db_path)
        host = PluginHost()
        host.registries["llm.providers"].register("default", LLM(), source="test.llm")
        host.registries["tools.executors"].register(
            "echo", EchoTool(), source="test.echo"
        )
        service = KernelAgentService.from_plugin_host(
            host,
            event_log=event_log,
            effect_ledger=effect_ledger,
        )
        await service.open_session(SessionOpenRequest(session_id="effect-session"))
        handle = await service.start(
            RunRequest(
                request_id="effect-request",
                session_id="effect-session",
                turn_id="effect-turn",
                user_input=Message.user("run effect"),
            )
        )
        await handle.wait()

        bundle = await event_log.load_recovery_bundle(handle.run_id)
        effect = await effect_ledger.reconcile(f"{handle.run_id}:effect-call")
        assert effect is not None
        assert effect.status is EffectStatus.SUCCEEDED
        assert any(event.type == "tool.call.completed" for event in bundle.events)
        assert bundle.checkpoint is not None
        assert bundle.checkpoint.state["last_event_type"] == "run.completed"
        event_log.close()
        effect_ledger.close()

    asyncio.run(scenario())


def test_effect_boundary_failure_rolls_back_tool_event_and_terminal_effect(
    tmp_path: Path,
) -> None:
    """A failed effect write cannot leave a durable Tool completion behind."""

    class LLM:
        async def stream(self, request):
            yield ModelChunk(
                tool_calls=(
                    ToolCallRequest(
                        call_id="effect-fail-call",
                        tool_name="echo",
                        arguments={"text": "ok"},
                    ),
                ),
                finish_reason="tool_calls",
            )

    class EchoTool(Tool):
        @property
        def name(self) -> str:
            return "echo"

        @property
        def description(self) -> str:
            return "echo"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {"text": {"type": "string"}}}

        async def execute(self, *, text: str) -> ToolResult:
            return ToolResult(success=True, content=text)

    class FailingEffectLog(SQLiteEventLog):
        def _upsert_effect_locked(self, effect):
            raise RuntimeError("simulated effect transaction failure")

    async def scenario() -> None:
        db_path = tmp_path / "effect-rollback.sqlite3"
        event_log = FailingEffectLog(db_path)
        effect_ledger = SQLiteEffectLedger(db_path)
        host = PluginHost()
        host.registries["llm.providers"].register("default", LLM(), source="test.llm")
        host.registries["tools.executors"].register(
            "echo", EchoTool(), source="test.echo"
        )
        service = KernelAgentService.from_plugin_host(
            host,
            event_log=event_log,
            effect_ledger=effect_ledger,
        )
        await service.open_session(SessionOpenRequest(session_id="effect-fail-session"))
        handle = await service.start(
            RunRequest(
                request_id="effect-fail-request",
                session_id="effect-fail-session",
                turn_id="effect-fail-turn",
                user_input=Message.user("run effect"),
            )
        )
        result = await handle.wait()
        bundle = await event_log.load_recovery_bundle(handle.run_id)
        effect = await effect_ledger.reconcile(f"{handle.run_id}:effect-fail-call")

        assert result.status == "failed"
        # The initial requested/effect fence is one transaction now. A failed
        # effect write therefore rolls back both the requested event and the
        # effect row; no executor or terminal Tool event can become visible.
        assert effect is None
        assert not any(event.type == "tool.call.requested" for event in bundle.events)
        assert not any(event.type == "tool.call.completed" for event in bundle.events)
        assert all(message.event.type not in {"tool.call.requested", "tool.call.completed"} for message in await event_log.outbox_after(handle.run_id))
        event_log.close()
        effect_ledger.close()

    asyncio.run(scenario())


def test_effect_terminal_failure_preserves_committed_requested_fence(
    tmp_path: Path,
) -> None:
    """A later completion failure keeps the earlier running fence durable."""

    class LLM:
        async def stream(self, request):
            del request
            yield ModelChunk(
                tool_calls=(
                    ToolCallRequest(
                        call_id="effect-two-phase-call",
                        tool_name="echo",
                        arguments={"text": "ok"},
                    ),
                ),
                finish_reason="tool_calls",
            )

    class EchoTool(Tool):
        @property
        def name(self) -> str:
            return "echo"

        @property
        def description(self) -> str:
            return "echo"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {"text": {"type": "string"}}}

        async def execute(self, *, text: str) -> ToolResult:
            return ToolResult(success=True, content=text)

    class FailTerminalLog(SQLiteEventLog):
        def __init__(self, path):
            super().__init__(path)
            self.effect_writes = 0

        def _upsert_effect_locked(self, effect):
            self.effect_writes += 1
            if self.effect_writes > 1:
                raise RuntimeError("simulated terminal effect failure")
            return super()._upsert_effect_locked(effect)

    async def scenario() -> None:
        db_path = tmp_path / "effect-two-phase.sqlite3"
        event_log = FailTerminalLog(db_path)
        effect_ledger = SQLiteEffectLedger(db_path)
        host = PluginHost()
        host.registries["llm.providers"].register("default", LLM(), source="test.llm")
        host.registries["tools.executors"].register(
            "echo", EchoTool(), source="test.echo"
        )
        service = KernelAgentService.from_plugin_host(
            host,
            event_log=event_log,
            effect_ledger=effect_ledger,
        )
        await service.open_session(SessionOpenRequest(session_id="effect-two-phase-session"))
        handle = await service.start(
            RunRequest(
                request_id="effect-two-phase-request",
                session_id="effect-two-phase-session",
                turn_id="effect-two-phase-turn",
                user_input=Message.user("run effect"),
            )
        )
        result = await handle.wait()
        bundle = await event_log.load_recovery_bundle(handle.run_id)
        effect = await effect_ledger.reconcile(f"{handle.run_id}:effect-two-phase-call")

        assert result.status == "failed"
        assert effect is not None and effect.status is EffectStatus.RUNNING
        assert any(event.type == "tool.call.requested" for event in bundle.events)
        assert not any(event.type == "tool.call.completed" for event in bundle.events)
        event_log.close()
        effect_ledger.close()

    asyncio.run(scenario())
