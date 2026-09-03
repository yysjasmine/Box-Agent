"""Event-driven bridge for the existing conversation memory extractor."""

from __future__ import annotations

import pytest

from box_agent.api import AgentEvent
from box_agent.memory_engine import ConversationMemoryExtractionHook


def _event(
    sequence: int,
    event_type: str,
    payload: dict,
    *,
    session_id: str = "session-1",
    run_id: str = "run-1",
    turn_id: str = "turn-1",
) -> AgentEvent:
    return AgentEvent(
        event_id=f"event-{sequence}",
        sequence=sequence,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        type=event_type,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_memory_extraction_hook_builds_transcript_from_kernel_events() -> None:
    extractors = {}

    class Extractor:
        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            self.calls = []

        async def maybe_extract(self, messages, trigger, *, turn_id=None):
            self.calls.append(
                (trigger, turn_id, [(message.role, message.content) for message in messages])
            )
            return True

    def factory(session_id: str):
        extractor = Extractor(session_id)
        extractors[session_id] = extractor
        return extractor

    hook = ConversationMemoryExtractionHook(factory)
    await hook.on_event(
        _event(1, "run.started", {"user_input": {"role": "user", "content": "I prefer concise replies"}})
    )
    await hook.on_event(
        _event(
            2,
            "model.requested",
            {
                "request": {
                    "messages": [
                        {"role": "system", "content": "system"},
                        {"role": "user", "content": "I prefer concise replies"},
                    ]
                }
            },
        )
    )
    await hook.on_event(
        _event(3, "model.response.completed", {"content": "Understood."})
    )
    await hook.on_event(_event(4, "run.completed", {"stop_reason": "stop"}))

    assert extractors["session-1"].calls == [
        (
            "loop_end",
            "turn-1",
            [
                ("system", "system"),
                ("user", "I prefer concise replies"),
                ("assistant", "Understood."),
            ],
        )
    ]


@pytest.mark.asyncio
async def test_memory_extraction_hook_maps_step_and_compaction_boundaries() -> None:
    calls = []

    class Extractor:
        async def maybe_extract(self, messages, trigger, *, turn_id=None):
            calls.append((trigger, turn_id, len(messages)))
            return True

    hook = ConversationMemoryExtractionHook(lambda _session_id: Extractor())
    await hook.on_event(
        _event(1, "run.started", {"user_input": {"role": "user", "content": "remember me"}})
    )
    await hook.on_event(_event(2, "context.compacted", {}))
    await hook.on_event(_event(3, "step.completed", {"step": 1}))
    await hook.on_event(_event(4, "run.failed", {"error": {"message": "failed"}}))

    assert [trigger for trigger, _turn_id, _count in calls] == [
        "pre_summarize",
        "step_interval",
        "loop_end",
    ]
    assert all(turn_id == "turn-1" for _trigger, turn_id, _count in calls)


@pytest.mark.asyncio
async def test_memory_extraction_hook_isolates_session_extractors() -> None:
    created = []

    class Extractor:
        async def maybe_extract(self, messages, trigger, *, turn_id=None):
            return True

    def factory(session_id: str):
        created.append(session_id)
        return Extractor()

    hook = ConversationMemoryExtractionHook(factory)
    await hook.on_event(
        _event(1, "run.started", {"user_input": {"role": "user", "content": "one"}})
    )
    await hook.on_event(
        _event(
            1,
            "run.started",
            {"user_input": {"role": "user", "content": "two"}},
            session_id="session-2",
            run_id="run-2",
            turn_id="turn-2",
        )
    )

    assert created == ["session-1", "session-2"]
