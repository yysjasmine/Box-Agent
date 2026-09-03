"""Behavioral contract tests for the stable Agent API layer."""

from __future__ import annotations

import pytest
import box_agent.api as agent_api

from box_agent.api import (
    AgentEvent,
    AttachmentRef,
    CommandAck,
    ControlCommand,
    ErrorCode,
    ErrorInfo,
    Message,
    RunOptions,
    RunRequest,
)


def test_run_request_contains_data_only() -> None:
    request = RunRequest(
        request_id="req-1",
        session_id="session-1",
        turn_id="turn-1",
        user_input=Message.user("hello"),
    )

    payload = request.to_dict()

    assert payload["request_id"] == "req-1"
    assert payload["user_input"] == {
        "role": "user",
        "content": "hello",
        "name": None,
        "metadata": {},
    }
    assert payload["options"] == RunOptions().to_dict()
    assert "llm" not in payload
    assert "tools" not in payload
    assert "permission_gateway" not in payload


def test_run_request_serializes_typed_input_attachments() -> None:
    request = RunRequest(
        request_id="req-attachment",
        session_id="session-1",
        turn_id="turn-1",
        user_input=Message.user("describe this image"),
        attachments=(
            AttachmentRef(
                attachment_id="image-1",
                kind="image",
                uri="file:///D:/workspace/image.png",
                mime_type="image/png",
                name="image.png",
            ),
        ),
    )

    assert request.to_dict()["attachments"] == [
        {
            "attachment_id": "image-1",
            "kind": "image",
            "uri": "file:///D:/workspace/image.png",
            "mime_type": "image/png",
            "name": "image.png",
            "metadata": {},
        }
    ]


@pytest.mark.parametrize(
    "values",
    [
        {"attachment_id": "", "kind": "image", "uri": "file:///a.png"},
        {"attachment_id": "a", "kind": "", "uri": "file:///a.png"},
        {"attachment_id": "a", "kind": "image", "uri": ""},
    ],
)
def test_attachment_ref_rejects_missing_identity(values) -> None:
    with pytest.raises(ValueError):
        AttachmentRef(**values)


def test_run_options_reject_non_positive_limits() -> None:
    with pytest.raises(ValueError, match="max_steps"):
        RunOptions(max_steps=0)

    with pytest.raises(ValueError, match="deadline_ms"):
        RunOptions(deadline_ms=-1)

    with pytest.raises(ValueError, match="provider_stale_seconds"):
        RunOptions(provider_stale_seconds=0)

    with pytest.raises(ValueError, match="provider_stale_seconds"):
        RunOptions(provider_stale_seconds=float("inf"))

    with pytest.raises(ValueError, match="max_truncation_continuations"):
        RunOptions(max_truncation_continuations=-1)


def test_run_options_serializes_run_local_component_selection() -> None:
    options = RunOptions(component_keys={"context": "vendor.context", "tools": "vendor.tools"})

    assert options.to_dict()["component_keys"] == {
        "context": "vendor.context",
        "tools": "vendor.tools",
    }


@pytest.mark.parametrize("value", [{"context": None}, {"": "vendor"}, {1: "vendor"}])
def test_run_options_rejects_malformed_component_selection(value) -> None:
    with pytest.raises(ValueError):
        RunOptions(component_keys=value)


def test_event_sequence_and_control_identity_are_explicit() -> None:
    event = AgentEvent(
        event_id="event-1",
        sequence=3,
        session_id="session-1",
        run_id="run-1",
        type="run.started",
        payload={},
    )
    command = ControlCommand(
        command_id="command-1",
        session_id="session-1",
        run_id="run-1",
        kind="run.cancel",
        payload={},
        source="test",
    )

    assert event.to_dict()["sequence"] == 3
    assert event.to_dict()["event_id"] == "event-1"
    assert command.to_dict()["command_id"] == "command-1"
    assert command.to_dict()["kind"] == "run.cancel"


def test_injection_control_has_one_canonical_payload_across_protocol_spellings() -> None:
    command = ControlCommand(
        command_id="command-1",
        session_id="session-1",
        run_id="run-1",
        kind="run.inject",
        payload={"message": "focus"},
        source="sdk.v1",
    )

    canonical = command.canonical()

    assert canonical.payload == {
        "injection_id": "command-1",
        "text": "focus",
    }
    assert canonical.canonical() is canonical


def test_command_ack_serializes_structured_error() -> None:
    error = ErrorInfo(
        code=ErrorCode.PERMISSION_DENIED,
        category="permission",
        message="network access denied",
        retryable=False,
        details={"capability": "network.web"},
    )
    ack = CommandAck(
        command_id="command-1",
        accepted=False,
        status="rejected",
        error=error,
    )

    assert ack.to_dict()["error"] == {
        "code": "PERMISSION_DENIED",
        "category": "permission",
        "message": "network access denied",
        "retryable": False,
        "details": {"capability": "network.web"},
    }


def test_message_preserves_serializable_multimodal_blocks() -> None:
    message = Message(
        role="user",
        content=(
            {"type": "text", "text": "inspect this"},
            {
                "type": "input_image",
                "source": {"type": "url", "url": "https://example.test/a.png"},
            },
        ),
    )

    assert message.to_dict()["content"][1]["type"] == "input_image"


def test_plan_snapshot_protocol_is_shared_by_host_adapters() -> None:
    """Plan start/approval payloads have one stable host-facing contract."""
    assert hasattr(agent_api, "plan_start_payload")
    assert hasattr(agent_api, "plan_approval_is_approved")

    payload = agent_api.plan_start_payload(
        request_id="plan-request-1",
        approval_required=True,
    )

    assert payload["type"] == "plan_snapshot"
    assert payload["action"] == "start"
    assert payload["plan"]["id"] == "pending"
    assert payload["plan"]["status"] == "draft"
    assert payload["summary"] == {
        "steps": 0,
        "verification": 0,
        "risks": 0,
        "assumptions": 0,
    }
    assert payload["approval"] == {
        "required": True,
        "state": "drafting",
        "request_id": "plan-request-1",
        "plan_id": "pending",
    }
    assert agent_api.plan_approval_is_approved({"decision": "approved"}) is True
    assert agent_api.plan_approval_is_approved({"decision": "later"}) is False
