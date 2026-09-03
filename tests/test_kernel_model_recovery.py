"""Provider-output repair invariants owned by the native Agent Loop Kernel."""

from __future__ import annotations

import asyncio

import pytest

from box_agent.api import AgentEvent, Message, ModelChunk, RunOptions, RunRequest
from box_agent.kernel import AgentLoopKernel


def _request(*, max_steps: int = 6) -> RunRequest:
    return RunRequest(
        request_id="model-recovery-request",
        session_id="model-recovery-session",
        turn_id="model-recovery-turn",
        user_input=Message.user("Create the requested artifact."),
        options=RunOptions(max_steps=max_steps),
    )


class _NoTools:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, call, *, context=None):
        del context
        self.calls.append(call)
        raise AssertionError("incomplete provider tool calls must never execute")


@pytest.mark.asyncio
async def test_kernel_discards_truncated_tool_attempt_and_injects_bounded_repair() -> None:
    class LLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ModelChunk(
                    content="partial",
                    finish_reason="max_tokens",
                    truncated_tool_calls=(
                        {"name": "write_file", "arguments_len": 4090},
                    ),
                    stream_dropped_mid_tool=True,
                )
                return
            yield ModelChunk(content="recovered", finish_reason="stop")

    llm = LLM()
    tools = _NoTools()
    events: list[AgentEvent] = []
    result = await AgentLoopKernel(llm=llm, tool_engine=tools).run(
        _request(),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert result.final_message == "recovered"
    assert tools.calls == []
    assert len(llm.requests) == 2
    assert any(
        "None of the tool calls" in str(message.content)
        and "next_chunk_index" in str(message.content)
        for message in llm.requests[1].messages
    )
    recovery = next(event for event in events if event.type == "model.recovery.requested")
    assert recovery.payload["reason"] == "truncated_tool_call"
    assert recovery.payload["attempt"] == 1
    assert recovery.payload["max_attempts"] == 3


@pytest.mark.asyncio
async def test_kernel_stops_after_truncated_tool_repair_budget_is_exhausted() -> None:
    class LLM:
        async def stream(self, request):
            del request
            yield ModelChunk(
                finish_reason="length",
                truncated_tool_calls=({"name": "bash", "arguments_len": 42},),
            )

    events: list[AgentEvent] = []
    result = await AgentLoopKernel(llm=LLM(), tool_engine=_NoTools()).run(
        _request(max_steps=8),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    repairs = [event for event in events if event.type == "model.recovery.requested"]
    assert [event.payload["attempt"] for event in repairs] == [1, 2, 3]
    assert result.status == "failed"
    assert result.stop_reason == "max_tokens"
    assert result.error is not None
    assert result.error.details["repair_attempts"] == 3
    assert events[-1].type == "run.failed"


@pytest.mark.asyncio
async def test_kernel_repairs_oversized_tool_arguments_once_then_fails_closed() -> None:
    class LLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ModelChunk(
                finish_reason="tool_argument_limit",
                oversized_tool_calls=(
                    {
                        "name": "write_file",
                        "arguments_len": 9000,
                        "limit": 8000,
                    },
                ),
            )

    llm = LLM()
    events: list[AgentEvent] = []
    result = await AgentLoopKernel(llm=llm, tool_engine=_NoTools()).run(
        _request(max_steps=4),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    repairs = [event for event in events if event.type == "model.recovery.requested"]
    assert len(repairs) == 1
    assert repairs[0].payload["reason"] == "tool_argument_limit"
    assert repairs[0].payload["max_attempts"] == 1
    assert any(
        "ordered chunks" in str(message.content)
        for message in llm.requests[1].messages
    )
    assert result.status == "failed"
    assert result.stop_reason == "tool_argument_limit"


@pytest.mark.asyncio
async def test_kernel_retries_provider_stale_without_treating_partial_output_as_success() -> None:
    class LLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ModelChunk(content="partial", finish_reason="provider_stale")
                return
            yield ModelChunk(content="recovered", finish_reason="stop")

    llm = LLM()
    events: list[AgentEvent] = []
    result = await AgentLoopKernel(llm=llm, tool_engine=_NoTools()).run(
        _request(),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert result.final_message == "recovered"
    assert len(llm.requests) == 2
    assert any(
        "stopped producing data" in str(message.content)
        for message in llm.requests[1].messages
    )
    recovery = next(event for event in events if event.type == "model.recovery.requested")
    assert recovery.payload["reason"] == "provider_stale"


@pytest.mark.asyncio
async def test_kernel_resumes_after_crash_at_persisted_model_recovery_boundary() -> None:
    class TruncatedLLM:
        async def stream(self, request):
            del request
            yield ModelChunk(
                finish_reason="max_tokens",
                truncated_tool_calls=({"name": "bash", "arguments_len": 64},),
            )

    persisted: list[AgentEvent] = []

    def crash_after_persist(event: AgentEvent) -> None:
        persisted.append(event)
        if event.type == "model.recovery.requested":
            raise SystemExit("simulated worker crash")

    with pytest.raises(SystemExit, match="simulated worker crash"):
        await AgentLoopKernel(llm=TruncatedLLM(), tool_engine=_NoTools()).run(
            _request(),
            emit=crash_after_persist,
            cancel_event=asyncio.Event(),
        )

    class RecoveredLLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ModelChunk(content="resumed safely", finish_reason="stop")

    llm = RecoveredLLM()
    resumed_events: list[AgentEvent] = []
    request = _request()
    request = RunRequest(
        request_id=request.request_id,
        session_id=request.session_id,
        turn_id=request.turn_id,
        user_input=request.user_input,
        options=request.options,
        metadata={"_recovery_bundle": {"events": tuple(persisted)}},
    )
    result = await AgentLoopKernel(llm=llm, tool_engine=_NoTools()).run(
        request,
        emit=resumed_events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert result.final_message == "resumed safely"
    assert len(llm.requests) == 1
    assert any(
        "None of its tool calls were executed" in str(message.content)
        for message in llm.requests[0].messages
    )
