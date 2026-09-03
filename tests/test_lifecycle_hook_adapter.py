"""Configured lifecycle hooks run from the stable Kernel event protocol."""

from __future__ import annotations

import asyncio

import pytest

from box_agent.api import Message, ModelChunk, RunOptions, RunRequest
from box_agent.kernel import AgentLoopKernel
from box_agent.plugins.hooks import LifecycleHookAdapter
from box_agent.events import StopReason


@pytest.mark.asyncio
async def test_lifecycle_hook_adapter_maps_complete_kernel_run() -> None:
    calls = []

    class Hook:
        async def on_agent_start(self, *, messages, tools, max_steps):
            calls.append(("start", messages[-1].content, tuple(tools), max_steps))

        async def on_step_start(self, *, step, max_steps):
            calls.append(("step_start", step, max_steps))

        async def on_llm_response(self, *, response):
            calls.append(("llm", response.content, response.finish_reason))

        async def on_step_end(self, *, step, elapsed_seconds, total_elapsed_seconds):
            calls.append(("step_end", step, elapsed_seconds, total_elapsed_seconds))

        async def on_done(self, *, stop_reason, final_content):
            calls.append(("done", stop_reason, final_content))

    class LLM:
        async def stream(self, request):
            yield ModelChunk(content="answer", finish_reason="stop")

    result = await AgentLoopKernel(
        llm=LLM(),
        hooks=(LifecycleHookAdapter(Hook()),),
    ).run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
            options=RunOptions(max_steps=2),
        ),
        emit=lambda _event: None,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert calls[0] == ("start", "hello", (), 2)
    assert calls[1] == ("step_start", 1, 2)
    assert calls[2] == ("llm", "answer", "stop")
    assert calls[3][0:2] == ("step_end", 1)
    assert calls[3][2] >= 0
    assert calls[3][3] >= calls[3][2]
    assert calls[4] == ("done", StopReason.END_TURN, "answer")


@pytest.mark.asyncio
async def test_lifecycle_hook_adapter_maps_failed_kernel_run() -> None:
    calls = []

    class Hook:
        async def on_error(self, *, message, is_fatal, exception):
            calls.append(("error", message, is_fatal, exception))

        async def on_done(self, *, stop_reason, final_content):
            calls.append(("done", stop_reason, final_content))

    class LLM:
        async def stream(self, request):
            raise RuntimeError("provider down")
            yield

    result = await AgentLoopKernel(
        llm=LLM(),
        hooks=(LifecycleHookAdapter(Hook()),),
    ).run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        ),
        emit=lambda _event: None,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "failed"
    assert calls[0][0] == "error"
    assert calls[0][2] is True
    assert calls[1][0:2] == ("done", StopReason.ERROR)


@pytest.mark.asyncio
async def test_lifecycle_hook_adapter_preserves_tool_interceptors() -> None:
    class Hook:
        async def on_tool_start(self, **kwargs):
            return {**kwargs["arguments"], "approved": True}

        async def on_tool_result(self, **kwargs):
            return (f"filtered:{kwargs['content']}", None)

    adapter = LifecycleHookAdapter(Hook())

    assert await adapter.on_tool_start(
        tool_call_id="call-1",
        tool_name="echo",
        arguments={"text": "hello"},
    ) == {"text": "hello", "approved": True}
    assert await adapter.on_tool_result(
        tool_call_id="call-1",
        tool_name="echo",
        success=True,
        content="hello",
        error=None,
    ) == ("filtered:hello", None)
