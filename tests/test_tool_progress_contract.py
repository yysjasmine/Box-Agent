"""Tool progress is a stable Kernel event, not a host-specific side channel."""

from __future__ import annotations

import asyncio

import pytest

from box_agent.api import (
    Message,
    ModelChunk,
    PermissionDecision,
    RunOptions,
    RunRequest,
    ToolCallRequest,
    ToolExecutionContext,
)
from box_agent.kernel import AgentLoopKernel
from box_agent.plugins import PluginHost
from box_agent.tools.base import ToolResult
from box_agent.tools.engine import RegistryToolEngine


class _ProgressTool:
    name = "progress_probe"
    description = "emit one progress fact"
    parameters = {"type": "object", "properties": {}}

    def validate(self, arguments):
        return None

    async def preflight(self, arguments, *, context=None):
        return None

    async def invoke(self, arguments, *, context=None):
        assert isinstance(context, ToolExecutionContext)
        assert context.call_id == "call-1"
        assert context.tool_name == self.name
        await context.publish_progress("probe.phase", {"percent": 50})
        return ToolResult(success=True, content="probe complete")


class _LLM:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request):
        self.calls += 1
        if self.calls == 1:
            yield ModelChunk(
                tool_calls=(
                    ToolCallRequest(
                        call_id="call-1",
                        tool_name="progress_probe",
                        arguments={},
                    ),
                ),
                finish_reason="tool_calls",
            )
            return
        yield ModelChunk(content="done", finish_reason="stop")


@pytest.mark.asyncio
async def test_kernel_orders_tool_progress_inside_the_tool_call_boundary() -> None:
    host = PluginHost()
    host.registries["tools"].register(
        "progress_probe", _ProgressTool(), source="test"
    )
    events = []

    result = await AgentLoopKernel(
        llm=_LLM(),
        tool_engine=RegistryToolEngine(host.registries["tools"]),
    ).run(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("run the probe"),
            options=RunOptions(max_steps=2),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    event_types = [event.type for event in events]
    requested = event_types.index("tool.call.requested")
    progress = event_types.index("tool.progress")
    completed = event_types.index("tool.call.completed")
    assert requested < progress < completed
    assert events[progress].payload == {
        "call_id": "call-1",
        "tool_name": "progress_probe",
        "kind": "probe.phase",
        "data": {"percent": 50},
    }
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))


class _PermissionProbeTool:
    name = "permission_probe"
    description = "request permission before execution"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.approved = False
        self.executor_entered = False

    def validate(self, arguments):
        return None

    async def preflight(self, arguments, *, context=None):
        if self.approved:
            return None
        return ToolResult(
            success=False,
            permission_request={
                "scope": "filesystem",
                "reason": "read attached image",
                "path": "image.png",
            },
        )

    def approve_permission_request(self, permission_request):
        self.approved = True

    async def invoke(self, arguments, *, context=None):
        self.executor_entered = True
        return ToolResult(success=True, content="inspected")


class _PermissionLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request):
        self.calls += 1
        if self.calls == 1:
            yield ModelChunk(
                tool_calls=(
                    ToolCallRequest(
                        call_id="permission-1",
                        tool_name="permission_probe",
                        arguments={},
                    ),
                ),
                finish_reason="tool_calls",
            )
            return
        yield ModelChunk(content="done", finish_reason="stop")


@pytest.mark.asyncio
async def test_kernel_publishes_complete_permission_boundary_before_executor() -> None:
    tool = _PermissionProbeTool()

    class Gateway:
        async def decide(self, request):
            assert tool.executor_entered is False
            return PermissionDecision(granted=True, reason="approved")

    host = PluginHost()
    host.registries["tools"].register(tool.name, tool, source="test")
    events = []

    result = await AgentLoopKernel(
        llm=_PermissionLLM(),
        tool_engine=RegistryToolEngine(
            host.registries["tools"], permission_gateway=Gateway()
        ),
    ).run(
        RunRequest(
            request_id="permission-request",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("inspect the image"),
            options=RunOptions(max_steps=2),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert tool.executor_entered is True
    event_types = [event.type for event in events]
    assert event_types.index("permission.requested") < event_types.index(
        "permission.resolved"
    ) < event_types.index("tool.call.requested") < event_types.index(
        "tool.call.completed"
    )
    requested = next(event for event in events if event.type == "permission.requested")
    resolved = next(event for event in events if event.type == "permission.resolved")
    assert requested.payload["call_id"] == "permission-1"
    assert requested.payload["resource"] == "image.png"
    assert resolved.payload == {
        "call_id": "permission-1",
        "granted": True,
        "reason": "approved",
        "metadata": {},
    }
