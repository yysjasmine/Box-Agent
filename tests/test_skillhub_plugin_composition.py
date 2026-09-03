from types import SimpleNamespace

import pytest

from box_agent.tools.skillhub_contributor import (
    SkillHubContextContributor,
    SkillHubDiscoveryState,
    SkillHubToolContributor,
)


def _request(capabilities):
    return SimpleNamespace(
        session_id="session-1",
        metadata={
            "workspace_dir": "/workspace",
            "host_capabilities": capabilities,
        },
    )


def test_skillhub_tools_are_composed_only_for_matching_host_capabilities():
    contributor = SkillHubToolContributor(SimpleNamespace())

    assert contributor.provide_tools(_request({})) == ()
    search_only = contributor.provide_tools(_request({"skillhub_search": 1}))
    search_install = contributor.provide_tools(
        _request({"skillhubSearch": {"version": 1}, "skillhubInstall": 1})
    )

    assert [tool.name for tool in search_only] == ["search_skillhub"]
    assert [tool.name for tool in search_install] == [
        "search_skillhub",
        "install_skillhub_skill",
    ]


@pytest.mark.asyncio
async def test_skillhub_tools_delegate_to_host_reverse_extension():
    calls = []

    class Connection:
        async def ext_method(self, method, payload):
            calls.append((method, payload))
            return {"status": "empty", "items": []}

    tool = SkillHubToolContributor(Connection()).provide_tools(
        _request({"skillhub_search": 1})
    )[0]

    result = await tool.execute(
        requested_outcome="Generate speech",
        request_kind="explicit_marketplace_request",
        missing_capability="text to speech",
        gap_type="missing_tool_or_runtime",
        fallback_assessment="No installed speech generator",
        queries=["TTS", "语音合成"],
    )

    assert result.success
    assert calls == [
        (
            "session/skillhub_search",
            {
                "sessionId": "session-1",
                "query": "TTS",
                "gapType": "missing_tool_or_runtime",
                "limit": 3,
            },
        ),
        (
            "session/skillhub_search",
            {
                "sessionId": "session-1",
                "query": "语音合成",
                "gapType": "missing_tool_or_runtime",
                "limit": 3,
            },
        ),
    ]


def test_skillhub_context_is_injected_only_when_search_is_available():
    contributor = SkillHubContextContributor()
    request = SimpleNamespace(
        session_id="session-1",
        run_id="run-1",
        metadata={"host_capabilities": {"skillhub_search": 1}},
    )
    absent = SimpleNamespace(session_id="session-2", run_id="run-2", metadata={})

    items = contributor.provide(request)

    assert len(items) == 1
    assert "search_skillhub" in items[0].content
    assert contributor.provide(absent) == ()


@pytest.mark.asyncio
async def test_skillhub_discovery_state_is_scoped_to_run():
    state = SkillHubDiscoveryState(tool_search_available=True)
    event = SimpleNamespace(
        type="tool.call.requested",
        session_id="session-1",
        run_id="run-1",
        payload={"tool_name": "tool_search"},
    )

    await state.on_event(event)

    assert state.snapshot("session-1", "run-1")["tool_search_used"] is True
    assert state.snapshot("session-1", "run-2")["tool_search_used"] is False
