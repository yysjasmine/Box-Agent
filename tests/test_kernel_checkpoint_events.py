"""Regression coverage for Kernel event boundaries and Service checkpoints."""

from __future__ import annotations

import asyncio

from box_agent.api import (
    AgentEvent,
    ContextBuildResult,
    Message,
    ModelChunk,
    RunRequest,
    RunResult,
    SessionOpenRequest,
    ToolCallRequest,
)
from box_agent.kernel import AgentLoopKernel
from box_agent.persistence import SQLiteEventLog
from box_agent.persistence import RecoveryBundle, RunCheckpoint
from box_agent.services.kernel import KernelAgentService, _validate_recovery_bundle
from box_agent.tools.engine import RegistryToolEngine
from box_agent.plugins import TypedRegistry
from box_agent.workflows import GoalStore, GoalWorkflowPolicy, build_goal_tools


class _BoundaryContext:
    async def assemble(self, request):
        return ContextBuildResult(
            items=request.items,
            estimated_tokens=1,
            compacted=True,
            removed_item_ids=("old:item",),
        )


class _BoundaryMemory:
    def __init__(self) -> None:
        self.flushed = False

    async def recall(self, query):
        from box_agent.api import MemoryRecall

        return MemoryRecall(entries=(), query=query)

    async def flush(self) -> None:
        self.flushed = True

    async def write(self, entry):
        return entry


class _BoundaryLLM:
    async def stream(self, request):
        yield ModelChunk(content="done", finish_reason="stop")


def test_kernel_emits_model_context_and_memory_boundaries() -> None:
    async def scenario() -> None:
        emitted: list[AgentEvent] = []
        memory = _BoundaryMemory()
        result = await AgentLoopKernel(
            llm=_BoundaryLLM(),
            context_engine=_BoundaryContext(),
            memory_engine=memory,
        ).run(
            RunRequest(
                request_id="request-boundary",
                session_id="session-boundary",
                turn_id="turn-boundary",
                user_input=Message.user("hello"),
            ),
            emit=emitted.append,
            cancel_event=asyncio.Event(),
        )

        assert result.status == "completed"
        assert memory.flushed is True
        event_types = [event.type for event in emitted]
        assert "context.compacted" in event_types
        assert "model.requested" in event_types
        model_request = next(event for event in emitted if event.type == "model.requested")
        assert model_request.payload["request"]["messages"]
        assert model_request.payload["context_digest"]
        assert "memory.flushed" in event_types
        assert event_types[-1] == "run.completed"

    asyncio.run(scenario())


def test_service_checkpoints_transition_events_for_recovery(tmp_path) -> None:
    class FakeKernel:
        async def run(self, request, *, emit, cancel_event, controls=None):
            run_id = request.metadata["run_id"]
            for sequence, event_type in enumerate(
                ("run.started", "step.completed", "run.completed"),
                start=1,
            ):
                await emit(
                    AgentEvent(
                        event_id=f"checkpoint-event-{sequence}",
                        sequence=sequence,
                        session_id=request.session_id,
                        run_id=run_id,
                        turn_id=request.turn_id,
                        type=event_type,
                        payload={
                            "stop_reason": "test",
                            "final_content": "done",
                        },
                    )
                )
            return RunResult(
                status="completed",
                stop_reason="test",
                final_message="done",
            )

    async def scenario() -> None:
        log = SQLiteEventLog(tmp_path / "checkpoint.sqlite3")
        service = KernelAgentService(
            kernel_factory=FakeKernel,
            event_log=log,
            plugin_lock={"llm:default": "sha256:test"},
        )
        await service.open_session(SessionOpenRequest(session_id="session-checkpoint"))
        handle = await service.start(
            RunRequest(
                request_id="request-checkpoint",
                session_id="session-checkpoint",
                turn_id="turn-checkpoint",
                user_input=Message.user("checkpoint me"),
            )
        )
        events = [event async for event in handle.events()]
        await handle.wait()
        bundle = await log.load_recovery_bundle(handle.run_id)

        assert [event.sequence for event in events] == [1, 2, 3]
        assert bundle.checkpoint is not None
        assert bundle.checkpoint.run_id == handle.run_id
        assert bundle.checkpoint.sequence == 3
        assert bundle.checkpoint.state["last_event_type"] == "run.completed"
        assert bundle.checkpoint.plugin_lock == {"llm:default": "sha256:test"}
        assert bundle.checkpoint.event_hash
        log.close()

    asyncio.run(scenario())


def test_workflow_state_is_checkpointed_after_tool_mutation(tmp_path) -> None:
    class RecordingLog(SQLiteEventLog):
        def __init__(self, path):
            super().__init__(path)
            self.checkpoints = []

        async def commit_checkpoint(self, checkpoint, *, events=(), outbox=()):
            self.checkpoints.append(checkpoint)
            return await super().commit_checkpoint(
                checkpoint, events=events, outbox=outbox
            )

    class LLM:
        def __init__(self):
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id="goal-write",
                            tool_name="goal_write",
                            arguments={"action": "set", "objective": "recover me"},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield ModelChunk(content="done", finish_reason="stop")

    async def scenario() -> None:
        log = RecordingLog(tmp_path / "workflow-state.sqlite3")
        goals = GoalStore()
        registry = TypedRegistry("tools")
        for tool in build_goal_tools(goals):
            registry.register(tool.name, tool, source="test")
        workflow = GoalWorkflowPolicy(goals, session_id="session-workflow")
        kernel = AgentLoopKernel(
            llm=LLM(),
            tool_engine=RegistryToolEngine(registry),
            workflow_policy=workflow,
        )
        service = KernelAgentService(kernel=kernel, event_log=log)
        await service.open_session(SessionOpenRequest(session_id="session-workflow"))
        handle = await service.start(
            RunRequest(
                request_id="request-workflow-state",
                session_id="session-workflow",
                turn_id="turn-workflow-state",
                user_input=Message.user("set it"),
            )
        )
        await handle.wait()

        tool_checkpoint = next(
            checkpoint
            for checkpoint in log.checkpoints
            if checkpoint.state["last_event_type"] == "tool.call.completed"
        )
        assert tool_checkpoint.state["workflow_state"]["goal"]["goal"]["objective"] == "recover me"
        log.close()

    asyncio.run(scenario())


def test_recovery_rejects_checkpoint_hash_mismatch() -> None:
    event = AgentEvent(
        event_id="event-1",
        sequence=1,
        session_id="session-1",
        run_id="run-1",
        turn_id="turn-1",
        type="run.started",
        payload={},
    )
    checkpoint = RunCheckpoint(
        checkpoint_id="run-1:1",
        session_id="session-1",
        run_id="run-1",
        sequence=1,
        state={"state": "running"},
        event_hash="tampered",
    )

    try:
        _validate_recovery_bundle(
            "run-1",
            RecoveryBundle(checkpoint=checkpoint, events=(event,)),
        )
    except Exception as exc:
        assert "event hash" in str(exc)
    else:  # pragma: no cover - defensive assertion for a failed safety gate
        raise AssertionError("tampered checkpoint must be rejected")


def test_recovery_rejects_schema_plugin_and_context_mismatch() -> None:
    event = AgentEvent(
        event_id="event-1",
        sequence=1,
        session_id="session-1",
        run_id="run-1",
        turn_id="turn-1",
        type="context.assembled",
        payload={"manifest": {"content_hash": "sha256:actual"}},
    )
    checkpoint = RunCheckpoint(
        checkpoint_id="run-1:1",
        session_id="session-1",
        run_id="run-1",
        sequence=1,
        state={
            "context_manifest": {"content_hash": "sha256:actual"},
            "context_digest": "sha256:tampered",
        },
        event_hash="unused",
        schema_version="2",
        plugin_lock={"vendor.plugin": "1.0.0"},
        plugin_snapshot={
            "vendor.plugin": {
                "version": "2.0.0",
                "schema_version": "state.v1",
                "state": {},
            }
        },
    )
    try:
        _validate_recovery_bundle(
            "run-1", RecoveryBundle(checkpoint=checkpoint, events=(event,))
        )
    except Exception as exc:
        assert "schema" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unsupported checkpoint schema must be rejected")

    valid_schema = RunCheckpoint(
        checkpoint_id="run-1:1",
        session_id="session-1",
        run_id="run-1",
        sequence=1,
        state={
            "context_manifest": {"content_hash": "sha256:actual"},
            "context_digest": "sha256:tampered",
        },
        event_hash="unused",
    )
    try:
        _validate_recovery_bundle(
            "run-1", RecoveryBundle(checkpoint=valid_schema, events=(event,))
        )
    except Exception as exc:
        assert "context digest" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("context digest mismatch must be rejected")
