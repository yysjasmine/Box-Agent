"""ACP host projections derived only from stable Agent events."""

from box_agent.adapters.acp_projection import ACPEventProjection
from box_agent.api import AgentEvent, Usage


def _event(sequence: int, event_type: str, payload: dict) -> AgentEvent:
    return AgentEvent(
        event_id=f"event-{sequence}",
        sequence=sequence,
        session_id="acp-session",
        run_id="run-1",
        turn_id="turn-1",
        type=event_type,
        payload=payload,
    )


def test_acp_projection_accumulates_tools_mcp_skills_and_model_usage() -> None:
    projection = ACPEventProjection(
        acp_session_id="acp-session",
        correlation_session_id="billing-session",
        task_id="task-1",
        turn_id="turn-1",
    )

    mcp_payload = projection.observe(
        _event(
            1,
            "tool.call.requested",
            {
                "call_id": "mcp-1",
                "tool_name": "search",
                "arguments": {"q": "architecture"},
                "metadata": {
                    "registration_source": "mcp.server:research",
                    "mcp_server": "research",
                },
            },
        )
    )
    projection.observe(
        _event(
            2,
            "tool.call.requested",
            {
                "call_id": "skill-1",
                "tool_name": "get_skill",
                "arguments": {"skill_name": "pptx"},
                "metadata": {"registration_source": "box_agent.tools"},
            },
        )
    )
    skill_payload = projection.observe(
        _event(
            3,
            "tool.call.completed",
            {
                "call_id": "skill-1",
                "tool_name": "get_skill",
                "status": "succeeded",
            },
        )
    )
    projection.observe(
        _event(
            4,
            "model.usage",
            {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        )
    )
    usage_payload = projection.observe(
        _event(
            5,
            "model.usage",
            {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        )
    )

    assert mcp_payload["current"] == {
        "type": "mcp",
        "name": "research.search",
        "server": "research",
        "tool": "search",
    }
    assert skill_payload["skills"] == ["pptx"]
    assert skill_payload["skillInvocations"][0]["activationSource"] == "get_skill"
    assert usage_payload["sessionId"] == "billing-session"
    assert usage_payload["acpSessionId"] == "acp-session"
    assert usage_payload["mcp"] == [
        {
            "server": "research",
            "tool": "search",
            "name": "research.search",
            "count": 1,
        }
    ]
    assert usage_payload["tools"] == [{"name": "get_skill", "count": 1}]
    assert usage_payload["tokenUsage"] == {
        "promptTokens": 6,
        "completionTokens": 3,
        "totalTokens": 9,
        "calls": 2,
    }


def test_acp_projection_final_usage_is_authoritative_without_double_counting() -> None:
    projection = ACPEventProjection(
        acp_session_id="acp-session",
        correlation_session_id="billing-session",
        task_id="task-1",
        turn_id="turn-1",
    )
    projection.observe(
        _event(
            1,
            "model.usage",
            {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        )
    )

    payload = projection.finalize(
        Usage(input_tokens=6, output_tokens=3, total_tokens=9)
    )

    assert payload["tokenUsage"] == {
        "promptTokens": 6,
        "completionTokens": 3,
        "totalTokens": 9,
        "calls": 1,
    }
