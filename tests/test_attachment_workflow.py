"""Input attachments are a typed request capability owned by a Workflow plugin."""

from __future__ import annotations

import asyncio

import pytest

from box_agent.api import (
    AttachmentRef,
    Message,
    ModelChunk,
    RunOptions,
    RunRequest,
    ToolCallResult,
)
from box_agent.kernel import AgentLoopKernel
from box_agent.workflows import AttachmentInspectionPolicy


def _request() -> RunRequest:
    return RunRequest(
        request_id="attachment-request",
        session_id="session-1",
        turn_id="turn-1",
        user_input=Message.user("What is visible?"),
        attachments=(
            AttachmentRef(
                attachment_id="image-1",
                kind="image",
                uri="file:///D:/workspace/one.png",
                mime_type="image/png",
            ),
            AttachmentRef(
                attachment_id="ignored-file",
                kind="file",
                uri="file:///D:/workspace/notes.txt",
                mime_type="text/plain",
            ),
        ),
        options=RunOptions(max_steps=2),
    )


def test_attachment_policy_builds_one_deterministic_image_inspection_action() -> None:
    policy = AttachmentInspectionPolicy().for_run(_request())

    action = policy.next_deterministic_action()

    assert action is not None
    assert action.tool_name == "inspect_images"
    assert action.arguments["image_paths"] == ["D:\\workspace\\one.png"]
    assert "What is visible?" in action.arguments["instruction"]
    assert policy.next_deterministic_action() is None


@pytest.mark.asyncio
async def test_attachment_result_enters_model_context_through_workflow_policy() -> None:
    class ToolEngine:
        def schemas(self):
            return (
                {
                    "name": "inspect_images",
                    "description": "inspect",
                    "input_schema": {"type": "object"},
                },
            )

        def supports_workflow_action(self, tool_name, capability):
            return (
                tool_name == "inspect_images"
                and capability == "attachment.inspect.image"
            )

        async def execute(self, call, *, context=None):
            return ToolCallResult(
                call_id=call.call_id,
                status="succeeded",
                content="A whiteboard with three architecture layers.",
            )

    class LLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ModelChunk(content="It shows a three-layer architecture.", finish_reason="stop")

    llm = LLM()
    events = []
    result = await AgentLoopKernel(
        llm=llm,
        tool_engine=ToolEngine(),
        workflow_policy=AttachmentInspectionPolicy().for_run(_request()),
    ).run(
        _request(),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert len(llm.requests) == 1
    visible_context = "\n".join(str(message.content) for message in llm.requests[0].messages)
    assert "untrusted visual evidence" in visible_context
    assert "A whiteboard with three architecture layers." in visible_context
    event_types = [event.type for event in events]
    assert event_types.index("tool.call.requested") < event_types.index(
        "tool.call.completed"
    ) < event_types.index("model.requested")
