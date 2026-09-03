"""Contract tests for the first pluggable Context Engine slice."""

from __future__ import annotations

import pytest

from box_agent.context import (
    CompositeContextEngine,
    ContextBuildRequest,
    ContextBuildResult,
    ContextItem,
    ContextProviderAdapter,
    InMemoryContextEngine,
    SkillCatalogContextContributor,
    ExpertContextContributor,
)
from box_agent.tools.skill_loader import Skill, SkillLoader


@pytest.mark.asyncio
async def test_context_engine_compacts_low_priority_items_with_stable_ids() -> None:
    engine = InMemoryContextEngine()
    result = await engine.assemble(
        ContextBuildRequest(
            items=(
                ContextItem("system", "system prompt", priority=100, pinned=True),
                ContextItem("user", "latest request", priority=90, pinned=True),
                ContextItem("tool", "large old output", priority=1),
                ContextItem("memory", "useful memory", priority=50),
            ),
            token_budget=8,
        )
    )

    assert [item.item_id for item in result.items] == ["system", "user"]
    assert result.compacted is True
    assert result.removed_item_ids == ("tool", "memory")
    assert result.estimated_tokens <= 8


@pytest.mark.asyncio
async def test_context_engine_restores_an_evicted_item_by_resource_id() -> None:
    engine = InMemoryContextEngine()
    item = ContextItem(
        "tool-1",
        "reconstructable content",
        resource_id="file:///workspace/a.py",
        content_version="version-1",
    )
    await engine.assemble(ContextBuildRequest(items=(item,), token_budget=1))

    restored = await engine.restore(
        resource_id="file:///workspace/a.py", content_version="version-1"
    )
    assert restored == item


def test_context_item_rejects_empty_identity() -> None:
    with pytest.raises(ValueError):
        ContextItem("", "content")


@pytest.mark.asyncio
async def test_context_provider_adapter_normalizes_minimal_provider() -> None:
    class Provider:
        async def provide(self, request):
            return [ContextItem("provider-item", "from provider")]

    result = await ContextProviderAdapter(Provider()).assemble(
        ContextBuildRequest(items=(), token_budget=10)
    )

    assert result.items[0].content == "from provider"


@pytest.mark.asyncio
async def test_composite_context_engine_runs_provider_then_compactor() -> None:
    seen = []

    class Provider:
        async def provide(self, request):
            return [ContextItem("provider", "raw")]

    class Compactor:
        async def compact(self, request):
            seen.append(tuple(item.item_id for item in request.items))
            return await InMemoryContextEngine().assemble(request)

    result = await CompositeContextEngine(
        provider=Provider(),
        compactor=Compactor(),
    ).assemble(ContextBuildRequest(items=(), token_budget=10))

    assert seen == [("provider",)]
    assert [item.item_id for item in result.items] == ["provider"]


@pytest.mark.asyncio
async def test_composite_context_engine_adds_contributors_before_provider() -> None:
    seen = []

    class Contributor:
        async def provide(self, request):
            assert [item.item_id for item in request.items] == ["user"]
            return [ContextItem("skill-catalog", "matched skill", pinned=True)]

    class Provider:
        async def assemble(self, request):
            seen.append(tuple(item.item_id for item in request.items))
            return await InMemoryContextEngine().assemble(request)

    result = await CompositeContextEngine(
        provider=Provider(),
        contributors=(Contributor(),),
    ).assemble(
        ContextBuildRequest(
            items=(ContextItem("user", "make a deck", kind="user"),),
            token_budget=20,
        )
    )

    assert seen == [("user", "skill-catalog")]
    assert [item.item_id for item in result.items] == ["user", "skill-catalog"]


@pytest.mark.asyncio
async def test_composite_context_preserves_contributor_host_projections() -> None:
    from box_agent.context import HostProjection

    class Contributor:
        async def provide(self, request):
            del request
            return ContextBuildResult(
                items=(ContextItem("expert", "expert context"),),
                estimated_tokens=2,
                host_projections=(
                    HostProjection(
                        "expert-progress",
                        {"type": "expert_team_progress", "team": "research"},
                    ),
                ),
            )

    result = await CompositeContextEngine(
        provider=InMemoryContextEngine(),
        contributors=(Contributor(),),
    ).assemble(ContextBuildRequest(items=(), token_budget=20))

    assert [projection.to_dict() for projection in result.host_projections] == [
        {
            "projection_id": "expert-progress",
            "surface": "raw_output",
            "schema_version": 1,
            "payload": {"type": "expert_team_progress", "team": "research"},
        }
    ]


@pytest.mark.asyncio
async def test_skill_catalog_contributor_uses_cumulative_user_context() -> None:
    loader = SkillLoader(".")
    loader.loaded_skills["deck-author"] = Skill(
        name="deck-author",
        description="Create investor deck presentations.",
        content="Full instructions stay behind get_skill.",
        source="user",
        keywords=["deck", "presentation"],
    )
    contributor = SkillCatalogContextContributor(loader)

    items = await contributor.provide(
        ContextBuildRequest(
            items=(
                ContextItem(
                    "old-user",
                    "investor",
                    kind="user",
                    metadata={"role": "user"},
                ),
                ContextItem(
                    "new-user",
                    "create a deck",
                    kind="user",
                    metadata={"role": "user"},
                ),
            ),
            token_budget=100,
        )
    )

    assert len(items) == 1
    assert "`deck-author`" in items[0].content
    assert "Full instructions stay behind get_skill" not in items[0].content
    assert items[0].pinned is True


@pytest.mark.asyncio
async def test_expert_context_contributor_uses_run_metadata_not_adapter_state() -> None:
    result = await ExpertContextContributor().provide(
        ContextBuildRequest(
            items=(ContextItem("user", "research", kind="user"),),
            token_budget=100,
            metadata={
                "expert": {
                    "id": "researcher",
                    "name": "行业研究员",
                    "role": "区分事实与推断",
                    "revision": "rev-1",
                }
            },
        )
    )

    assert isinstance(result, ContextBuildResult)
    assert len(result.items) == 1
    assert result.items[0].pinned is True
    assert "行业研究员" in str(result.items[0].content)
    assert result.items[0].metadata["expert"]["revision"] == "rev-1"


@pytest.mark.asyncio
async def test_kernel_composer_sends_registered_skill_catalog_to_model() -> None:
    from box_agent.api import Message, ModelChunk, RunRequest, SessionOpenRequest
    from box_agent.plugins import PluginHost
    from box_agent.services.kernel import KernelAgentService

    class LLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ModelChunk(content="done", finish_reason="stop")

    loader = SkillLoader(".")
    loader.loaded_skills["deck-author"] = Skill(
        name="deck-author",
        description="Create investor deck presentations.",
        content="Full instructions.",
        source="user",
        keywords=["deck"],
    )
    llm = LLM()
    host = PluginHost()
    host.registries["llm"].register("default", llm, source="test")
    host.registries["context"].register(
        "default", InMemoryContextEngine(), source="test"
    )
    host.registries["context.contributors"].register(
        "skills", SkillCatalogContextContributor(loader), source="test"
    )
    service = KernelAgentService.from_plugin_host(host)
    await service.open_session(SessionOpenRequest(session_id="skill-context-session"))

    result = await (
        await service.start(
            RunRequest(
                request_id="skill-context-request",
                session_id="skill-context-session",
                turn_id="skill-context-turn",
                user_input=Message.user("create an investor deck"),
            )
        )
    ).wait()

    assert result.status == "completed"
    assert any(
        "`deck-author`" in str(message.content)
        for message in llm.requests[0].messages
    )
