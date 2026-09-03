"""Behavior tests for the new plugin-composed Agent Loop Kernel."""

from __future__ import annotations

import asyncio

import pytest

from box_agent.api import (
    ControlCommand,
    AgentEvent,
    ContextBuildRequest,
    ContextBuildResult,
    ContextItem,
    MemoryEntry,
    MemoryQuery,
    MemoryRecall,
    ModelChunk,
    RunOptions,
    RunRequest,
    ToolCallRequest,
    ToolCallResult,
    Usage,
    Message,
    ControlCommand,
)
from box_agent.kernel import AgentLoopKernel
from box_agent.workflows.goal import GoalStore, GoalWorkflowPolicy
from box_agent.workflows.contract import WorkflowAction, WorkflowCheckpointUpdate


class FakeContext:
    def __init__(self) -> None:
        self.requests: list[ContextBuildRequest] = []

    async def assemble(self, request: ContextBuildRequest) -> ContextBuildResult:
        self.requests.append(request)
        return ContextBuildResult(
            items=request.items,
            estimated_tokens=sum(item.estimated_tokens for item in request.items),
        )

    async def restore(self, *, resource_id, content_version=None):
        return None


class FakeMemory:
    def __init__(self) -> None:
        self.queries: list[MemoryQuery] = []
        self.flushed = False

    async def recall(self, query: MemoryQuery) -> MemoryRecall:
        self.queries.append(query)
        return MemoryRecall(
            entries=(MemoryEntry("memory-1", "remember python"),),
            query=query,
        )

    async def write(self, entry):
        return entry

    async def flush(self) -> None:
        self.flushed = True


class FakeLLM:
    def __init__(self) -> None:
        self.requests = []
        self.calls = 0

    async def stream(self, request):
        self.requests.append(request)
        self.calls += 1
        if self.calls == 1:
            yield ModelChunk(
                tool_calls=(
                    ToolCallRequest(
                        call_id="call-1",
                        tool_name="echo",
                        arguments={"text": "hello"},
                    ),
                ),
                finish_reason="tool_calls",
            )
            return
        yield ModelChunk(
            content="final answer",
            finish_reason="stop",
            usage=Usage(input_tokens=2, output_tokens=3, total_tokens=5),
        )


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[ToolCallRequest] = []

    async def execute(self, call, *, context=None):
        self.calls.append(call)
        return ToolCallResult(call_id=call.call_id, status="succeeded", content="echoed")

    def supports_workflow_action(self, tool_name, capability):
        return tool_name == "echo" and capability == "ordered.verify"


@pytest.mark.asyncio
async def test_kernel_closes_tool_run_after_terminal_result() -> None:
    class FinalLLM:
        async def stream(self, request):
            yield ModelChunk(content="done", finish_reason="stop")

    class LifecycleTools:
        def __init__(self):
            self.ended = []

        def schemas(self):
            return ()

        async def execute(self, call, *, context=None):
            raise AssertionError("no tool call expected")

        async def end_run(self, *, session_id, run_id, metadata):
            self.ended.append((session_id, run_id, dict(metadata)))

    tools = LifecycleTools()
    kernel = AgentLoopKernel(llm=FinalLLM(), tool_engine=tools)
    request = RunRequest(
        request_id="request-cleanup",
        session_id="session-cleanup",
        turn_id="turn-cleanup",
        user_input=Message.user("hello"),
        metadata={"workspace_dir": "."},
    )

    result = await kernel.run(
        request,
        emit=lambda event: None,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert len(tools.ended) == 1
    assert tools.ended[0][0] == "session-cleanup"
    assert tools.ended[0][2] == {"workspace_dir": "."}


@pytest.mark.asyncio
async def test_kernel_refreshes_workflow_checkpoint_before_deterministic_action() -> None:
    class Policy:
        kind = "ordered"
        checkpoint_injection_id = "workflow:ordered"

        def __init__(self) -> None:
            self.ready = False
            self.dispatched = False

        def build_checkpoint(self):
            return "stage=ready"

        def update_checkpoint(self, text):
            assert text == "stage=ready"
            self.ready = True
            return WorkflowCheckpointUpdate(text=text, changed=True)

        def context_items(self, context):
            assert context["checkpoint_text"] == "stage=ready"
            return ()

        def next_deterministic_action(self):
            assert self.ready is True
            if self.dispatched:
                return None
            self.dispatched = True
            return WorkflowAction(
                action_id="ordered:verify",
                capability="ordered.verify",
                tool_name="echo",
                arguments={"text": "verified"},
            )

        def build_checkpoint_payload(self):
            return {self.kind: {"ready": self.ready, "dispatched": self.dispatched}}

    class LLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            del request
            self.calls += 1
            yield ModelChunk(content="done", finish_reason="stop")

    policy = Policy()
    llm = LLM()
    tools = FakeTools()
    events = []
    result = await AgentLoopKernel(
        llm=llm,
        tool_engine=tools,
        workflow_policy=policy,
    ).run(
        RunRequest(
            request_id="ordered-request",
            session_id="ordered-session",
            turn_id="ordered-turn",
            user_input=Message.user("run ordered workflow"),
            options=RunOptions(max_steps=2),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert llm.calls == 1
    assert [call.tool_name for call in tools.calls] == ["echo"]
    first_tool = next(index for index, event in enumerate(events) if event.type == "tool.call.requested")
    first_model = next(index for index, event in enumerate(events) if event.type == "model.requested")
    assert first_tool < first_model


@pytest.mark.asyncio
async def test_kernel_composes_context_memory_llm_and_tools() -> None:
    context = FakeContext()
    memory = FakeMemory()
    llm = FakeLLM()
    tools = FakeTools()
    kernel = AgentLoopKernel(
        llm=llm,
        context_engine=context,
        memory_engine=memory,
        tool_engine=tools,
    )
    request = RunRequest(
        request_id="request-1",
        session_id="session-1",
        turn_id="turn-1",
        user_input=Message.user("write python"),
        options=RunOptions(max_steps=3),
    )
    emitted: list[AgentEvent] = []

    result = await kernel.run(
        request,
        emit=emitted.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert result.final_message == "final answer"
    assert result.usage == Usage(input_tokens=2, output_tokens=3, total_tokens=5)
    assert memory.queries[0].text == "write python"
    assert memory.flushed is True
    assert tools.calls[0].tool_name == "echo"
    assert tools.calls[0].metadata["run_id"]
    assert tools.calls[0].metadata["effect_id"].endswith(":call-1")
    assert tools.calls[0].metadata["idempotency_key"] == "echo:call-1"
    assert any(item.content == "remember python" for item in context.requests[0].items)
    assert any(
        item.metadata.get("tool_call_ids") == ["call-1"]
        for item in context.requests[1].items
    )
    assert any(item.metadata.get("role") == "tool" for item in context.requests[1].items)
    assert [event.sequence for event in emitted] == list(range(1, len(emitted) + 1))
    event_types = [event.type for event in emitted]
    assert event_types.index("tool.call.requested") < event_types.index("tool.call.completed")
    assert "model.usage" in event_types
    assert event_types[-1] == "run.completed"


@pytest.mark.asyncio
async def test_kernel_accumulates_usage_across_model_tool_cycles() -> None:
    class MultiCallLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            del request
            self.calls += 1
            if self.calls == 1:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id="usage-call",
                            tool_name="echo",
                            arguments={"text": "first"},
                        ),
                    ),
                    finish_reason="tool_calls",
                    usage=Usage(input_tokens=2, output_tokens=1, total_tokens=3),
                )
                return
            yield ModelChunk(
                content="done",
                finish_reason="stop",
                usage=Usage(input_tokens=4, output_tokens=2, total_tokens=6),
            )

    events = []
    result = await AgentLoopKernel(
        llm=MultiCallLLM(),
        tool_engine=FakeTools(),
    ).run(
        RunRequest(
            request_id="usage-request",
            session_id="usage-session",
            turn_id="usage-turn",
            user_input=Message.user("measure all model calls"),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    usage_events = [event for event in events if event.type == "model.usage"]
    assert [event.payload["total_tokens"] for event in usage_events] == [3, 6]
    assert result.usage == Usage(input_tokens=6, output_tokens=3, total_tokens=9)


@pytest.mark.asyncio
async def test_kernel_retries_then_rejects_empty_final_answer_after_tools() -> None:
    class EmptyFinalLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id="echo-1",
                            tool_name="echo",
                            arguments={"text": "evidence"},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
                return
            yield ModelChunk(content="", finish_reason="stop")

    llm = EmptyFinalLLM()
    emitted = []
    result = await AgentLoopKernel(llm=llm, tool_engine=FakeTools()).run(
        RunRequest(
            request_id="empty-final-request",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("use the tool and summarize"),
            options=RunOptions(max_steps=3),
        ),
        emit=emitted.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "failed"
    assert result.stop_reason == "empty_final_answer"
    assert result.error is not None
    assert "未生成最终答复" in result.error.message
    assert llm.calls == 3
    recovery = [
        event
        for event in emitted
        if event.type == "model.recovery.requested"
        and event.payload.get("reason") == "empty_final_answer"
    ]
    assert len(recovery) == 1
    assert emitted[-1].type == "run.failed"


@pytest.mark.asyncio
async def test_kernel_rehydrates_prior_session_turns_as_context() -> None:
    class CaptureLLM:
        def __init__(self) -> None:
            self.messages = ()
            self.metadata = {}

        async def stream(self, request):
            self.messages = request.messages
            self.metadata = request.metadata
            yield ModelChunk(content="answer", finish_reason="stop")

    llm = CaptureLLM()
    kernel = AgentLoopKernel(llm=llm)
    result = await kernel.run(
        RunRequest(
            request_id="request-2",
            session_id="session-1",
            turn_id="turn-2",
            user_input=Message.user("current question"),
            metadata={
                "run_id": "run-2",
                "system_prompt": "You are Box-Agent.",
                "model_profile": "fast",
                "_session_events": {
                    "events": [
                        {
                            "event_id": "old-start",
                            "sequence": 1,
                            "session_id": "session-1",
                            "run_id": "run-1",
                            "turn_id": "turn-1",
                            "type": "run.started",
                            "payload": {
                                "user_input": {
                                    "role": "user",
                                    "content": "previous question",
                                }
                            },
                        },
                        {
                            "event_id": "old-answer",
                            "sequence": 2,
                            "session_id": "session-1",
                            "run_id": "run-1",
                            "turn_id": "turn-1",
                            "type": "model.response.completed",
                            "payload": {
                                "content": "previous answer",
                                "tool_call_ids": [],
                            },
                        },
                    ]
                },
            },
        ),
        emit=lambda event: None,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert [message.content for message in llm.messages] == [
        "You are Box-Agent.",
        "current question",
        "previous question",
        "previous answer",
    ]
    assert llm.metadata["model_profile"] == "fast"
    assert "_session_events" not in llm.metadata


@pytest.mark.asyncio
async def test_kernel_emits_thinking_deltas_as_events() -> None:
    class ThinkingLLM:
        async def stream(self, request):
            yield ModelChunk(thinking="internal", content="answer", finish_reason="stop")

    emitted: list[AgentEvent] = []
    result = await AgentLoopKernel(llm=ThinkingLLM()).run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        ),
        emit=emitted.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert any(
        event.type == "model.thinking.delta" and event.payload["thinking"] == "internal"
        for event in emitted
    )


@pytest.mark.asyncio
async def test_kernel_classifies_provider_failure_with_stable_error() -> None:
    class BrokenLLM:
        async def stream(self, request):
            raise RuntimeError("provider unavailable")
            yield  # pragma: no cover

    emitted: list[AgentEvent] = []
    result = await AgentLoopKernel(llm=BrokenLLM()).run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        ),
        emit=emitted.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "MODEL_PROVIDER_ERROR"
    assert emitted[-1].type == "run.failed"


@pytest.mark.asyncio
async def test_kernel_isolates_memory_plugin_failures() -> None:
    class BrokenMemory:
        async def recall(self, query):
            raise RuntimeError("memory backend unavailable")

        async def flush(self):
            raise RuntimeError("memory backend unavailable")

    class AnswerLLM:
        async def stream(self, request):
            yield ModelChunk(content="answer", finish_reason="stop")

    emitted: list[AgentEvent] = []
    result = await AgentLoopKernel(llm=AnswerLLM(), memory_engine=BrokenMemory()).run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        ),
        emit=emitted.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert [event.type for event in emitted].count("memory.error") == 2


@pytest.mark.asyncio
async def test_kernel_honors_cancellation_at_step_boundary() -> None:
    cancel_event = asyncio.Event()
    cancel_event.set()
    emitted: list[AgentEvent] = []
    kernel = AgentLoopKernel(llm=FakeLLM())

    result = await kernel.run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("stop"),
        ),
        emit=emitted.append,
        cancel_event=cancel_event,
    )

    assert result.status == "cancelled"
    assert [event.type for event in emitted] == ["run.cancelled"]


@pytest.mark.asyncio
async def test_kernel_cancellation_preserves_workflow_terminal_snapshot() -> None:
    cancel_event = asyncio.Event()
    cancel_event.set()
    goals = GoalStore()
    goals.set("session-1", "Ship native")
    emitted: list[AgentEvent] = []

    result = await AgentLoopKernel(
        llm=FakeLLM(),
        workflow_policy=GoalWorkflowPolicy(goals, session_id="session-1"),
    ).run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("stop"),
        ),
        emit=emitted.append,
        cancel_event=cancel_event,
    )

    assert result.status == "cancelled"
    assert result.metadata["goal"]["objective"] == "Ship native"
    assert emitted[0].payload["metadata"]["goal"]["objective"] == "Ship native"


@pytest.mark.asyncio
async def test_kernel_promotes_tool_artifacts_to_stable_result_and_event() -> None:
    class ArtifactLLM:
        async def stream(self, request):
            if not any(message.role == "tool" for message in request.messages):
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(call_id="call-1", tool_name="make", arguments={}),
                    ),
                    finish_reason="tool_calls",
                )
                return
            yield ModelChunk(content="finished", finish_reason="stop")

    class ArtifactTool:
        async def execute(self, call, *, context=None):
            return ToolCallResult(
                call_id=call.call_id,
                status="succeeded",
                content="created",
                output={
                    "artifact": {
                        "artifact_id": "artifact-1",
                        "kind": "image",
                        "uri": "/workspace/chart.png",
                        "mime": "image/png",
                    }
                },
            )

        def schemas(self):
            return ({"name": "make", "input_schema": {"type": "object"}},)

    emitted: list[AgentEvent] = []
    result = await AgentLoopKernel(llm=ArtifactLLM(), tool_engine=ArtifactTool()).run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("make chart"),
        ),
        emit=emitted.append,
        cancel_event=asyncio.Event(),
    )

    assert result.artifacts[0].artifact_id == "artifact-1"
    assert any(event.type == "artifact.created" for event in emitted)


@pytest.mark.asyncio
async def test_kernel_persists_and_enriches_artifact_before_publication() -> None:
    calls: list[object] = []

    class Processor:
        async def process(self, request):
            calls.append(request)
            return {**request.artifact, "artifact_id": "durable-artifact"}

    class LLM:
        async def stream(self, request):
            if not any(message.role == "tool" for message in request.messages):
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(call_id="call-1", tool_name="make", arguments={}),
                    ),
                    finish_reason="tool_calls",
                )
                return
            yield ModelChunk(content="finished", finish_reason="stop")

    class Tools:
        async def execute(self, call, *, context=None):
            return ToolCallResult(
                call_id=call.call_id,
                status="succeeded",
                output={"artifact": {"kind": "document", "uri": "file:///report.md"}},
            )

        def schemas(self):
            return ({"name": "make", "input_schema": {"type": "object"}},)

    events: list[AgentEvent] = []
    result = await AgentLoopKernel(
        llm=LLM(),
        tool_engine=Tools(),
        artifact_processor=Processor(),
    ).run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("make"),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    artifact_event = next(event for event in events if event.type == "artifact.created")
    assert len(calls) == 1
    assert artifact_event.payload["artifact_id"] == "durable-artifact"
    assert result.artifacts[0].artifact_id == "durable-artifact"


@pytest.mark.asyncio
async def test_artifact_registry_failure_cannot_publish_success() -> None:
    class Processor:
        def begin_run(self, request):
            return None

        def end_run(self, request, *, event_type, payload):
            if event_type == "run.completed":
                raise OSError("registry unavailable")

    class LLM:
        async def stream(self, request):
            yield ModelChunk(content="done", finish_reason="stop")

    events: list[AgentEvent] = []
    result = await AgentLoopKernel(
        llm=LLM(), artifact_processor=Processor()
    ).run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "failed"
    assert not any(event.type == "run.completed" for event in events)
    assert events[-1].type == "run.failed"
    assert "registry unavailable" in result.error.message


@pytest.mark.asyncio
async def test_kernel_watches_control_stream_and_cancels_provider_wait() -> None:
    provider_started = asyncio.Event()
    provider_release = asyncio.Event()
    commands: asyncio.Queue[ControlCommand] = asyncio.Queue()

    class BlockingLLM:
        async def stream(self, request):
            provider_started.set()
            await provider_release.wait()
            yield ModelChunk(content="late", finish_reason="stop")

    async def controls():
        while True:
            yield await commands.get()

    emitted: list[AgentEvent] = []
    task = asyncio.create_task(
        AgentLoopKernel(llm=BlockingLLM()).run(
            RunRequest(
                request_id="request-1",
                session_id="session-1",
                turn_id="turn-1",
                user_input=Message.user("cancel me"),
            ),
            emit=emitted.append,
            cancel_event=asyncio.Event(),
            controls=controls(),
        )
    )
    await provider_started.wait()
    commands.put_nowait(
        ControlCommand(
            command_id="command-1",
            session_id="session-1",
            run_id="run-1",
            kind="run.cancel",
            payload={},
            source="test",
        )
    )
    await asyncio.sleep(0)
    provider_release.set()

    result = await task

    assert result.status == "cancelled"
    assert emitted[-1].type == "run.cancelled"


@pytest.mark.asyncio
async def test_kernel_applies_workflow_controls_at_safe_boundary() -> None:
    provider_started = asyncio.Event()
    provider_release = asyncio.Event()
    commands: asyncio.Queue[ControlCommand] = asyncio.Queue()

    class BlockingLLM:
        async def stream(self, request):
            provider_started.set()
            await provider_release.wait()
            yield ModelChunk(content="done", finish_reason="stop")

    class Policy:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def handle_control(self, command):
            self.seen.append(command.kind)
            return {"accepted": True}

    async def controls():
        while True:
            yield await commands.get()

    policy = Policy()
    emitted: list[AgentEvent] = []
    task = asyncio.create_task(
        AgentLoopKernel(llm=BlockingLLM(), workflow_policy=policy).run(
            RunRequest(
                request_id="request-control",
                session_id="session-1",
                turn_id="turn-1",
                user_input=Message.user("hello"),
            ),
            emit=emitted.append,
            cancel_event=asyncio.Event(),
            controls=controls(),
        )
    )
    await provider_started.wait()
    commands.put_nowait(
        ControlCommand(
            command_id="control-1",
            session_id="session-1",
            run_id="request-control",
            kind="workflow.test.control",
            payload={},
            source="test",
        )
    )
    await asyncio.sleep(0)
    provider_release.set()

    result = await task

    assert result.status == "completed"
    assert policy.seen == ["workflow.test.control"]
    assert any(event.type == "workflow.control.applied" for event in emitted)


@pytest.mark.asyncio
async def test_kernel_applies_run_injection_to_next_model_boundary() -> None:
    provider_started = asyncio.Event()
    provider_release = asyncio.Event()
    commands: asyncio.Queue[ControlCommand] = asyncio.Queue()

    class LLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                provider_started.set()
                await provider_release.wait()
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id="echo-1",
                            tool_name="echo",
                            arguments={},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
                return
            yield ModelChunk(content="done", finish_reason="stop")

    class Tools:
        def schemas(self):
            return ({"name": "echo", "description": "echo", "input_schema": {}},)

        async def execute(self, call, *, context=None):
            del context
            return ToolCallResult(call_id=call.call_id, status="succeeded", content="ok")

    async def controls():
        while True:
            yield await commands.get()

    llm = LLM()
    emitted = []
    task = asyncio.create_task(
        AgentLoopKernel(llm=llm, tool_engine=Tools()).run(
            RunRequest(
                request_id="request-inject",
                session_id="session-1",
                turn_id="turn-1",
                user_input=Message.user("start"),
            ),
            emit=emitted.append,
            cancel_event=asyncio.Event(),
            controls=controls(),
        )
    )
    await provider_started.wait()
    commands.put_nowait(
        ControlCommand(
            command_id="inject-1",
            session_id="session-1",
            run_id="request-inject",
            kind="run.inject",
            payload={"injection_id": "note-1", "text": "Use approved source"},
            source="test",
        )
    )
    await asyncio.sleep(0)
    provider_release.set()

    result = await task

    assert result.status == "completed"
    assert any(
        "Use approved source" in str(message.content)
        for message in llm.requests[1].messages
    )
    assert any(event.type == "run.injected" for event in emitted)


@pytest.mark.asyncio
async def test_kernel_honors_parallel_tool_limit() -> None:
    active = 0
    peak = 0

    class ParallelLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id="a", tool_name="echo", arguments={"value": "a"}
                        ),
                        ToolCallRequest(
                            call_id="b", tool_name="echo", arguments={"value": "b"}
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield ModelChunk(content="done", finish_reason="stop")

    class ParallelTools:
        async def execute(self, call, *, context=None):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return ToolCallResult(call_id=call.call_id, status="succeeded", content=call.call_id)

    result = await AgentLoopKernel(llm=ParallelLLM(), tool_engine=ParallelTools()).run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("parallel"),
            options=RunOptions(max_steps=2, max_parallel_tools=2),
        ),
        emit=lambda event: None,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert peak == 2


@pytest.mark.asyncio
async def test_kernel_allows_workflow_budget_exempt_calls() -> None:
    class Workflow:
        def exempts_tool_budget(self, tool_name):
            return tool_name == "free"

    class LLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(call_id="free-1", tool_name="free", arguments={}),
                        ToolCallRequest(call_id="paid-1", tool_name="paid", arguments={}),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield ModelChunk(content="done", finish_reason="stop")

    class Tools:
        def __init__(self) -> None:
            self.calls = []

        async def execute(self, call, *, context=None):
            self.calls.append(call.tool_name)
            return ToolCallResult(
                call_id=call.call_id,
                status="succeeded",
                content=call.tool_name,
            )

    tools = Tools()
    result = await AgentLoopKernel(
        llm=LLM(),
        tool_engine=tools,
        workflow_policy=Workflow(),
    ).run(
        RunRequest(
            request_id="budget-exempt",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("run tools"),
            options=RunOptions(max_steps=2, max_tool_calls=1, max_parallel_tools=2),
        ),
        emit=lambda event: None,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert tools.calls == ["free", "paid"]


@pytest.mark.asyncio
async def test_kernel_deadline_cancels_a_slow_provider() -> None:
    release = asyncio.Event()

    class SlowLLM:
        async def stream(self, request):
            await release.wait()
            yield ModelChunk(content="late", finish_reason="stop")

    task = asyncio.create_task(
        AgentLoopKernel(llm=SlowLLM()).run(
            RunRequest(
                request_id="request-1",
                session_id="session-1",
                turn_id="turn-1",
                user_input=Message.user("timeout"),
                options=RunOptions(deadline_ms=1),
            ),
            emit=lambda event: None,
            cancel_event=asyncio.Event(),
        )
    )
    await asyncio.sleep(0.05)
    release.set()
    result = await task

    assert result.status == "cancelled"
    assert result.stop_reason == "deadline"


@pytest.mark.asyncio
async def test_kernel_routes_trusted_workflow_actions_through_normal_tool_path() -> None:
    class Workflow:
        def __init__(self) -> None:
            self.dispatched = False
            self.results = []

        def next_deterministic_action(self):
            if self.dispatched:
                return None
            self.dispatched = True
            return {
                "action_id": "workflow:prepare",
                "capability": "workflow.prepare",
                "tool_name": "prepare",
                "arguments": {"value": "ok"},
            }

        def record_tool_result(self, tool_name, arguments, result, *, executed=True):
            self.results.append((tool_name, arguments, result.success, executed))

    class LLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            yield ModelChunk(content="done", finish_reason="stop")

    class Tools:
        def __init__(self) -> None:
            self.calls = []

        def schemas(self):
            return ({"name": "prepare", "input_schema": {"type": "object"}},)

        def supports_workflow_action(self, tool_name, capability):
            return tool_name == "prepare" and capability == "workflow.prepare"

        async def execute(self, call, *, context=None):
            self.calls.append(call)
            return ToolCallResult(call_id=call.call_id, status="succeeded", content="prepared")

    workflow = Workflow()
    llm = LLM()
    tools = Tools()
    result = await AgentLoopKernel(
        llm=llm,
        tool_engine=tools,
        workflow_policy=workflow,
    ).run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("prepare and answer"),
            options=RunOptions(max_steps=2),
        ),
        emit=lambda event: None,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert llm.calls == 1
    assert [call.tool_name for call in tools.calls] == ["prepare"]
    assert workflow.results == [("prepare", {"value": "ok"}, True, True)]


@pytest.mark.asyncio
async def test_kernel_applies_workflow_tool_catalog_visibility() -> None:
    class Workflow:
        restrict_tools_until_required_succeed = True

        def hidden_tool_names(self):
            return {"hidden"}

        def required_tool_names(self):
            return {"required"}

    class LLM:
        def __init__(self) -> None:
            self.tools = ()

        async def stream(self, request):
            self.tools = request.tools
            yield ModelChunk(content="done", finish_reason="stop")

    class Tools:
        def schemas(self):
            return (
                {"name": "required", "input_schema": {"type": "object"}},
                {"name": "hidden", "input_schema": {"type": "object"}},
                {"name": "other", "input_schema": {"type": "object"}},
                {"name": "tool_search", "input_schema": {"type": "object"}},
            )

    llm = LLM()
    result = await AgentLoopKernel(
        llm=llm,
        tool_engine=Tools(),
        workflow_policy=Workflow(),
    ).run(
        RunRequest(
            request_id="catalog-visibility",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("check catalog"),
        ),
        emit=lambda event: None,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert [schema["name"] for schema in llm.tools] == ["required", "tool_search"]


@pytest.mark.asyncio
async def test_restrictive_workflow_preserves_dynamically_activated_tools() -> None:
    class Workflow:
        restrict_tools_until_required_succeed = True

        def required_tool_names(self):
            return {"generate_image"}

    class LLM:
        def __init__(self) -> None:
            self.tools = ()

        async def stream(self, request):
            self.tools = request.tools
            yield ModelChunk(content="done", finish_reason="stop")

    class Tools:
        def schemas(self):
            return (
                {"name": "generate_image", "input_schema": {"type": "object"}},
                {"name": "tool_search", "input_schema": {"type": "object"}},
                {"name": "lookup", "input_schema": {"type": "object"}},
                {"name": "fallback", "input_schema": {"type": "object"}},
            )

        def restricted_passthrough_tool_names(self):
            return frozenset({"lookup"})

    llm = LLM()
    result = await AgentLoopKernel(
        llm=llm,
        tool_engine=Tools(),
        workflow_policy=Workflow(),
    ).run(
        RunRequest(
            request_id="dynamic-catalog-visibility",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("create an image using discovered data"),
        ),
        emit=lambda event: None,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert [schema["name"] for schema in llm.tools] == [
        "generate_image",
        "tool_search",
        "lookup",
    ]
