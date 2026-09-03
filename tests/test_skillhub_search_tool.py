from __future__ import annotations

import pytest

from box_agent.tools.skillhub_search_tool import SkillHubSearchTool


def _arguments(**overrides):
    values = {
        "requested_outcome": "Review a landscape construction drawing",
        "request_kind": "capability_gap",
        "missing_capability": "Professional landscape drawing review workflow",
        "gap_type": "missing_specialized_workflow",
        "fallback_assessment": "Generic tools cannot provide the required workflow",
        "queries": ["landscape review", "景观图纸审查"],
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_skillhub_search_requires_deferred_local_discovery_first():
    calls = []

    async def searcher(payload):
        calls.append(payload)
        return {"status": "empty", "items": []}

    tool = SkillHubSearchTool(
        searcher,
        snapshot_provider=lambda: {
            "tool_search_available": True,
            "tool_search_used": False,
        },
    )

    result = await tool.execute(**_arguments())

    assert result.error.startswith("LOCAL_DISCOVERY_REQUIRED:")
    assert calls == []


@pytest.mark.asyncio
async def test_skillhub_search_normalizes_candidates_and_retains_exact_identity():
    async def searcher(payload):
        if payload["query"] == "landscape review":
            return {"status": "empty", "items": []}
        return {
            "status": "found",
            "items": [
                {
                    "id": "skill-1",
                    "slug": "landscape-review",
                    "name": "Landscape Review",
                    "description": "Review drawings",
                    "publisherDisplayName": "Publisher",
                    "currentVersion": "1.0.0",
                    "platforms": ["darwin", "win32"],
                    "riskLabels": ["professional"],
                    "downloadCount": 12,
                }
            ],
        }

    tool = SkillHubSearchTool(
        searcher,
        snapshot_provider=lambda: {
            "tool_search_available": True,
            "tool_search_used": True,
        },
        installation_available=True,
    )

    result = await tool.execute(**_arguments())

    assert result.success
    assert "not installed" in result.content
    assert "skill_id='skill-1'" in result.model_context
    assert tool.candidate("skill-1") == result.raw_output["items"][0]
    assert "Immediately call install_skillhub_skill" in result.content


@pytest.mark.asyncio
async def test_explicit_marketplace_request_skips_local_discovery():
    calls = []

    async def searcher(payload):
        calls.append(payload)
        return {"status": "empty", "items": []}

    tool = SkillHubSearchTool(
        searcher,
        snapshot_provider=lambda: {
            "tool_search_available": True,
            "tool_search_used": False,
        },
    )
    result = await tool.execute(
        **_arguments(request_kind="explicit_marketplace_request")
    )

    assert result.success
    assert len(calls) == 2
    assert result.raw_output["status"] == "empty"


@pytest.mark.asyncio
async def test_skillhub_search_rejects_sensitive_query_and_second_search():
    calls = []

    async def searcher(payload):
        calls.append(payload)
        return {"status": "empty", "items": []}

    tool = SkillHubSearchTool(searcher)
    unsafe = await tool.execute(
        **_arguments(queries=["landscape review", "alice@example.com"])
    )
    first = await tool.execute(**_arguments())
    second = await tool.execute(**_arguments(queries=["review tool", "审查工具"]))

    assert unsafe.error.startswith("UNSAFE_MARKET_QUERY:")
    assert first.success
    assert second.error.startswith("SEARCH_ALREADY_PERFORMED:")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_skillhub_search_budget_resets_at_run_boundary():
    async def searcher(_payload):
        return {"status": "empty", "items": []}

    tool = SkillHubSearchTool(searcher)
    assert (await tool.execute(**_arguments())).success
    assert not (await tool.execute(**_arguments())).success

    tool.end_run()

    assert (await tool.execute(**_arguments())).success
