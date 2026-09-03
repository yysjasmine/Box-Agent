"""Post-run follow-up suggestions use a typed, non-blocking host projection."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from box_agent.adapters import (
    KernelACPAgent,
    PostRunProjectionManager,
    PostRunProjectionRequest,
)
from box_agent.api import AgentEvent, Message, RunResult, SessionInfo
from box_agent.services.follow_up_suggestions import (
    FollowUpSuggestionsProjection,
    build_follow_up_suggestions_system_prompt,
    normalize_follow_up_suggestions,
    parse_follow_up_suggestions_response,
)


class _UtilityPrompt:
    def __init__(self, *, blocked: bool = False) -> None:
        self.calls = []
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def prompt(self, params):
        self.calls.append(dict(params))
        await self.release.wait()
        return {
            "text": (
                '{"suggestions":["核对项目当前的 React 版本",'
                '"查看 React 19.2 的新增内容"]}'
            )
        }


def _request(
    *,
    turn_id: str = "office-turn-1",
    stop_reason: str = "stop",
    metadata: dict | None = None,
) -> PostRunProjectionRequest:
    return PostRunProjectionRequest(
        session_id="office-session-1",
        run_id=f"run-{turn_id}",
        turn_id=turn_id,
        user_input=Message.user("React 最新版是多少"),
        result=RunResult(
            status="completed",
            stop_reason=stop_reason,
            final_message="React 当前 npm 稳定最新版是 19.2.7。",
            metadata=metadata or {},
        ),
        metadata={
            "follow_up_suggestions": True,
            "title": "React 版本查询",
        },
    )


def test_normalize_follow_up_suggestions_limits_and_deduplicates() -> None:
    assert normalize_follow_up_suggestions(
        ["  整理成待办  ", "整理成待办", "再生成风险清单", "补充负责人", "第四条"]
    ) == ["整理成待办", "再生成风险清单", "补充负责人"]


def test_parser_accepts_json_and_accidental_fences() -> None:
    assert parse_follow_up_suggestions_response(
        '```json\n{"suggestions":["整理迁移步骤"]}\n```'
    ) == ["整理迁移步骤"]
    assert parse_follow_up_suggestions_response("不是 JSON") == []
    assert '"suggestions"' in build_follow_up_suggestions_system_prompt()


@pytest.mark.asyncio
async def test_projection_uses_dedicated_utility_model_and_preserves_identity() -> None:
    utility = _UtilityPrompt()
    values = await FollowUpSuggestionsProjection(utility).project(_request())

    assert len(values) == 1
    assert values[0].payload == {
        "type": "follow_up_suggestions",
        "turn_id": "office-turn-1",
        "suggestions": [
            "核对项目当前的 React 版本",
            "查看 React 19.2 的新增内容",
        ],
    }
    meta = utility.calls[0]["_meta"]
    assert meta["session_id"] == "office-session-1"
    assert meta["turn_id"] == "office-turn-1"
    assert meta["title"] == "React 版本查询"


@pytest.mark.asyncio
async def test_projection_manager_cancels_stale_turn_without_blocking_new_turn() -> None:
    utility = _UtilityPrompt(blocked=True)
    manager = PostRunProjectionManager(
        lambda: (FollowUpSuggestionsProjection(utility),)
    )
    outputs = []

    first = manager.schedule(_request(turn_id="turn-1"), outputs.append)
    await asyncio.sleep(0)
    assert not first.done()
    await manager.begin_turn("office-session-1")
    assert first.cancelled()

    second = manager.schedule(_request(turn_id="turn-2"), outputs.append)
    await asyncio.sleep(0)
    assert not second.done()
    utility.release.set()
    await second

    assert [value.payload["turn_id"] for value in outputs] == ["turn-2"]


@pytest.mark.asyncio
async def test_projection_is_suppressed_at_user_input_boundary() -> None:
    utility = _UtilityPrompt()
    plugin = FollowUpSuggestionsProjection(utility)

    assert await plugin.project(_request(stop_reason="checkpoint_paused")) == ()
    assert await plugin.project(
        _request(metadata={"waiting_for_user_input": True})
    ) == ()
    assert utility.calls == []


class _Handle:
    run_id = "run-1"

    async def events(self, after_sequence=0):
        del after_sequence
        yield AgentEvent(
            event_id="content",
            sequence=1,
            session_id="session-1",
            run_id=self.run_id,
            turn_id="turn-1",
            type="model.content.delta",
            payload={"content": "React 当前 npm 稳定最新版是 19.2.7。"},
        )

    async def wait(self):
        return RunResult(
            status="completed",
            stop_reason="stop",
            final_message="React 当前 npm 稳定最新版是 19.2.7。",
        )

    async def cancel(self, reason=None):
        del reason


class _Service:
    async def open_session(self, request):
        return SessionInfo("session-1", "now", request.metadata)

    async def update_session_metadata(self, session_id, metadata):
        return SessionInfo(session_id, "now", metadata)

    async def start(self, request):
        return _Handle()


class _Connection:
    def __init__(self) -> None:
        self.updates = []

    async def sessionUpdate(self, payload):
        self.updates.append(payload)


@pytest.mark.asyncio
async def test_acp_returns_before_background_projection_and_renders_typed_payload() -> None:
    utility = _UtilityPrompt(blocked=True)
    manager = PostRunProjectionManager(
        lambda: (FollowUpSuggestionsProjection(utility),)
    )
    conn = _Connection()
    agent = KernelACPAgent(conn, _Service(), projection_manager=manager)
    await agent.newSession(
        SimpleNamespace(cwd=".", field_meta={"follow_up_suggestions": True})
    )

    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="React 最新版是多少")],
            field_meta={"turn_id": "office-turn-1", "title": "React 版本查询"},
        )
    )

    assert response.stopReason == "end_turn"
    assert not any(
        update.update.rawOutput.get("type") == "follow_up_suggestions"
        for update in conn.updates
        if isinstance(getattr(update.update, "rawOutput", None), dict)
    )

    utility.release.set()
    await manager.wait_for_session("session-1")
    outputs = [
        update.update.rawOutput
        for update in conn.updates
        if isinstance(getattr(update.update, "rawOutput", None), dict)
        and update.update.rawOutput.get("type") == "follow_up_suggestions"
    ]
    assert outputs == [
        {
            "type": "follow_up_suggestions",
            "turn_id": "office-turn-1",
            "suggestions": [
                "核对项目当前的 React 版本",
                "查看 React 19.2 的新增内容",
            ],
        }
    ]
