"""Behavioral tests for scoped plugin registration and lifecycle cleanup."""

from __future__ import annotations

import pytest

from box_agent.plugins import (
    DEFAULT_REGISTRY_KINDS,
    PluginConflictError,
    PluginHost,
    PluginManifest,
    TypedRegistry,
    PluginDependencyError,
    PluginDependencyCycleError,
)
from box_agent.kernel import KernelCompositionError, PluginKernelComposer


def test_duplicate_key_fails_within_scope() -> None:
    registry = TypedRegistry("tools")
    registry.register("demo", object(), source="one")

    with pytest.raises(PluginConflictError):
        registry.register("demo", object(), source="two")


def test_scopes_are_isolated_and_priority_resolves_within_scope() -> None:
    registry = TypedRegistry("tools")
    global_value = object()
    session_value = object()

    registry.register("demo", global_value, source="global", scope="global")
    registry.register("demo", session_value, source="session", scope="session")

    assert registry.get("demo", scope="global") is global_value
    assert registry.get("demo", scope="session") is session_value
    assert registry.get("demo", scope="run") is None


def test_resolve_prefers_specific_scope_before_global_priority() -> None:
    registry = TypedRegistry("tools")
    registry.register("demo", "global", source="global", priority=100)
    registry.register("demo", "run", source="run", scope="run", priority=0)

    assert registry.resolve("demo", scope="run") == "run"


def test_resolve_registration_reports_the_same_scoped_owner_as_value() -> None:
    registry = TypedRegistry("tools")
    registry.register(
        "demo",
        "global",
        source="builtin.tools",
        priority=100,
        version="1",
    )
    registry.register(
        "demo",
        "run",
        source="mcp.server:research",
        scope="run",
        priority=0,
        version="2",
    )

    registration = registry.resolve_registration("demo", scope="run")

    assert registration is not None
    assert registration.source == "mcp.server:research"
    assert registration.version == "2"


def test_resolve_all_returns_visible_capabilities_in_priority_order() -> None:
    registry = TypedRegistry("workflow.selectors")
    registry.register("skill", "global-skill", source="builtin", priority=10)
    registry.register("presentation", "presentation", source="builtin", priority=100)
    registry.register(
        "skill", "run-skill", source="vendor", scope="run", priority=0
    )

    assert registry.resolve_all(scope="run") == (
        "presentation",
        "run-skill",
    )


def test_invalid_scope_is_rejected_at_lookup_boundary() -> None:
    registry = TypedRegistry("tools")
    with pytest.raises(ValueError):
        registry.get("demo", scope="request")  # type: ignore[arg-type]


def test_default_plugin_host_exposes_all_capability_registries() -> None:
    host = PluginHost()
    assert set(DEFAULT_REGISTRY_KINDS).issubset(host.registries)
    assert "workflow.selectors" in host.registries
    assert "context.contributors" in host.registries
    assert "host.extensions" in host.registries


@pytest.mark.parametrize(
    ("legacy_name", "canonical_name"),
    (
        ("context", "context.providers"),
        ("tools", "tools.executors"),
        ("permissions", "permission.policies"),
        ("memory", "memory.providers"),
        ("llm", "llm.providers"),
    ),
)
def test_legacy_registry_names_alias_one_canonical_registry(
    legacy_name: str,
    canonical_name: str,
) -> None:
    host = PluginHost()

    assert host.registries[legacy_name] is host.registries[canonical_name]


def test_plugin_host_rejects_conflicting_alias_registry_instances() -> None:
    with pytest.raises(ValueError, match="same registry instance"):
        PluginHost(
            registries={
                "tools": TypedRegistry("tools"),
                "tools.executors": TypedRegistry("tools.executors"),
            }
        )


def test_plugin_host_discovers_entry_point_factories_deterministically(monkeypatch) -> None:
    class DemoPlugin:
        def __init__(self, plugin_id: str) -> None:
            self.manifest = PluginManifest(id=plugin_id, version="1.0.0")

        async def activate(self, ctx):
            del ctx

        async def deactivate(self):
            return None

        async def dispose(self):
            return None

    class EntryPoint:
        def __init__(self, name: str, value) -> None:
            self.name = name
            self.value = f"vendor:{name}"
            self._value = value

        def load(self):
            return self._value

    entries = [
        EntryPoint("z-plugin", lambda: DemoPlugin("vendor.z")),
        EntryPoint("a-plugin", DemoPlugin("vendor.a")),
    ]

    class Catalog:
        def select(self, *, group: str):
            assert group == "box_agent.plugins"
            return entries

    monkeypatch.setattr("box_agent.plugins.host.entry_points", lambda: Catalog())

    discovered = PluginHost.discover()

    assert [plugin.manifest.id for plugin in discovered] == ["vendor.a", "vendor.z"]


def test_plugin_host_discovery_rejects_entry_point_without_manifest(monkeypatch) -> None:
    class EntryPoint:
        name = "invalid"
        value = "vendor:invalid"

        def load(self):
            return object()

    class Catalog:
        def select(self, *, group: str):
            del group
            return (EntryPoint(),)

    monkeypatch.setattr("box_agent.plugins.host.entry_points", lambda: Catalog())

    with pytest.raises(PluginDependencyError, match="does not expose a manifest"):
        PluginHost.discover()


def test_plugin_host_discovery_rejects_incomplete_lifecycle(monkeypatch) -> None:
    class IncompletePlugin:
        manifest = PluginManifest(id="vendor.incomplete", version="1.0.0")

    class EntryPoint:
        name = "incomplete"
        value = "vendor:incomplete"

        def load(self):
            return IncompletePlugin()

    class Catalog:
        def select(self, *, group: str):
            del group
            return (EntryPoint(),)

    monkeypatch.setattr("box_agent.plugins.host.entry_points", lambda: Catalog())

    with pytest.raises(PluginDependencyError, match="lifecycle method 'activate'"):
        PluginHost.discover()


def test_plugin_host_lock_includes_raw_registration_versions() -> None:
    host = PluginHost()
    host.registries["tools.executors"].register(
        "search",
        object(),
        source="vendor.search",
        version="2.1.0",
        state_schema="search.v1",
    )

    assert host.lock_snapshot()[
        "registration:tools.executors:global:search"
    ] == "vendor.search@2.1.0;schema=search.v1"


@pytest.mark.asyncio
async def test_dispose_source_removes_all_registrations_idempotently() -> None:
    registry = TypedRegistry("tools")
    first = registry.register("one", object(), source="plugin")
    second = registry.register("two", object(), source="plugin")

    await registry.dispose_source("plugin")
    await registry.dispose_source("plugin")

    assert registry.get("one") is None
    assert registry.get("two") is None
    await first.dispose()
    await second.dispose()


def test_replace_source_is_atomic_and_stale_handles_cannot_remove_replacement() -> None:
    registry = TypedRegistry("tools.executors")
    old = registry.register("search", "old", source="mcp.server:search")

    replacements = registry.replace_source(
        {"search": "new", "search_alias": "new"},
        source="mcp.server:search",
        version="2",
    )

    assert registry.resolve("search", scope="run") == "new"
    assert registry.resolve("search_alias", scope="run") == "new"
    assert {item.key for item in replacements} == {"search", "search_alias"}

    # A registration handle may outlive a hot reload. Disposing that stale
    # handle must not remove the newer generation registered under the key.
    import asyncio

    asyncio.run(old.dispose())
    assert registry.resolve("search", scope="run") == "new"


def test_replace_source_conflict_preserves_previous_generation() -> None:
    registry = TypedRegistry("tools.executors")
    registry.register("search", "vendor", source="vendor.search")
    registry.register("old", "old", source="mcp.server:search")

    with pytest.raises(PluginConflictError):
        registry.replace_source(
            {"search": "replacement"},
            source="mcp.server:search",
        )

    assert registry.resolve("search", scope="run") == "vendor"
    assert registry.resolve("old", scope="run") == "old"


@pytest.mark.asyncio
async def test_plugin_host_disposes_registrations_after_deactivation() -> None:
    registry = TypedRegistry("tools")

    class DemoPlugin:
        manifest = PluginManifest(
            id="demo.plugin",
            version="1.0.0",
            provides=("tool:demo",),
        )

        async def activate(self, ctx) -> None:
            ctx.registry("tools").register(
                "demo",
                "value",
                source=self.manifest.id,
            )

        async def deactivate(self) -> None:
            pass

        async def dispose(self) -> None:
            pass

    host = PluginHost(registries={"tools": registry})
    await host.activate(DemoPlugin())
    assert registry.get("demo") == "value"

    await host.deactivate("demo.plugin")
    assert registry.get("demo") is None


@pytest.mark.asyncio
async def test_plugin_host_checks_dependency_version_constraints() -> None:
    class BasePlugin:
        manifest = PluginManifest(id="base", version="1.2.0")

        async def activate(self, ctx):
            return None

        async def deactivate(self):
            return None

        async def dispose(self):
            return None

    class DependentPlugin(BasePlugin):
        manifest = PluginManifest(
            id="dependent",
            version="1.0.0",
            requires={"base": ">=2.0"},
        )

    host = PluginHost()
    await host.activate(BasePlugin())
    with pytest.raises(PluginDependencyError):
        await host.activate(DependentPlugin())


@pytest.mark.asyncio
async def test_plugin_host_activates_unordered_dependency_graph() -> None:
    activated: list[str] = []

    class Base:
        manifest = PluginManifest(id="base", version="1.0.0")

        async def activate(self, ctx):
            activated.append("base")

        async def deactivate(self):
            pass

        async def dispose(self):
            pass

    class Child(Base):
        manifest = PluginManifest(id="child", version="1.0.0", requires={"base": ">=1"})

        async def activate(self, ctx):
            activated.append("child")

    host = PluginHost()
    await host.activate_many([Child(), Base()])

    assert activated == ["base", "child"]


@pytest.mark.asyncio
async def test_plugin_host_reports_dependency_cycles() -> None:
    class A:
        manifest = PluginManifest(id="a", version="1.0.0", requires={"b": "*"})

        async def activate(self, ctx):
            pass

        async def deactivate(self):
            pass

        async def dispose(self):
            pass

    class B(A):
        manifest = PluginManifest(id="b", version="1.0.0", requires={"a": "*"})

    with pytest.raises(PluginDependencyCycleError):
        await PluginHost().activate_many([A(), B()])


@pytest.mark.asyncio
async def test_plugin_host_rolls_back_partial_graph_activation() -> None:
    disposed: list[str] = []

    class Base:
        manifest = PluginManifest(id="base", version="1.0.0")

        async def activate(self, ctx):
            ctx.registry("tools").register("base", object(), source="base")

        async def deactivate(self):
            disposed.append("base.deactivate")

        async def dispose(self):
            disposed.append("base.dispose")

    class Missing(Base):
        manifest = PluginManifest(id="missing", version="1.0.0", requires={"absent": "*"})

    host = PluginHost()
    with pytest.raises(PluginDependencyError):
        await host.activate_many([Base(), Missing()])

    assert host.plugins == ()
    assert disposed == ["base.deactivate", "base.dispose"]


@pytest.mark.asyncio
async def test_plugin_host_disposes_dependents_before_providers() -> None:
    disposed: list[str] = []

    class Base:
        manifest = PluginManifest(id="base", version="1.0.0")

        async def activate(self, ctx):
            return None

        async def deactivate(self):
            disposed.append("base.deactivate")

        async def dispose(self):
            disposed.append("base.dispose")

    class Child(Base):
        manifest = PluginManifest(id="child", version="1.0.0", requires={"base": "*"})

        async def deactivate(self):
            disposed.append("child.deactivate")

        async def dispose(self):
            disposed.append("child.dispose")

    host = PluginHost()
    await host.activate_many([Child(), Base()])
    await host.dispose()

    assert disposed == [
        "child.deactivate",
        "child.dispose",
        "base.deactivate",
        "base.dispose",
    ]


def test_kernel_composer_resolves_new_loop_capabilities_from_plugins() -> None:
    class LLM:
        pass

    host = PluginHost()
    llm = LLM()
    host.registries["llm"].register("default", llm, source="vendor.llm")

    kernel = PluginKernelComposer(host).build()

    assert kernel._llm is llm  # composition is intentionally identity-preserving


def test_kernel_composer_resolves_plural_permission_and_workflow_registries() -> None:
    class LLM:
        pass

    permission = object()
    workflow = object()
    host = PluginHost()
    host.registries["llm"].register("default", LLM(), source="vendor.llm")
    host.registries["permissions"].register(
        "default", permission, source="vendor.permissions"
    )
    host.registries["workflows"].register(
        "default", workflow, source="vendor.workflow"
    )

    kernel = PluginKernelComposer(host).build()

    assert kernel._tools._permission_gateway is permission
    assert kernel._workflow is workflow


def test_kernel_composer_selects_requested_third_party_workflow_key() -> None:
    class LLM:
        pass

    native_workflow = object()
    host = PluginHost()
    host.registries["llm"].register("default", LLM(), source="vendor.llm")
    host.registries["workflows"].register(
        "vendor.custom", native_workflow, source="vendor.workflow"
    )
    request = type(
        "Request",
        (),
        {
            "options": type("Options", (), {"workflow_id": "vendor.custom"})(),
            "metadata": {},
        },
    )()

    kernel = PluginKernelComposer(host).build(request)

    assert kernel._workflow is native_workflow


def test_kernel_composer_overlays_requested_workflow_on_default_policies() -> None:
    from box_agent.workflows import CompositeWorkflowPolicy

    class LLM:
        pass

    goal_plan = object()
    selected = object()
    host = PluginHost()
    host.registries["llm.providers"].register("default", LLM(), source="test")
    host.registries["workflows"].register("default", goal_plan, source="test")
    host.registries["workflows"].register("artifact", selected, source="test")
    request = type(
        "Request",
        (),
        {
            "options": type(
                "Options",
                (),
                {"workflow_id": "artifact", "component_keys": {}},
            )(),
            "metadata": {},
        },
    )()

    kernel = PluginKernelComposer(host).build(request)

    assert isinstance(kernel._workflow, CompositeWorkflowPolicy)
    assert kernel._workflow.policies == (goal_plan, selected)


def test_kernel_composer_rejects_unregistered_requested_workflow() -> None:
    """An explicit workflow selection must not silently degrade to no policy."""

    class LLM:
        pass

    host = PluginHost()
    host.registries["llm"].register("default", LLM(), source="vendor.llm")
    request = type(
        "Request",
        (),
        {
            "options": type("Options", (), {"workflow_id": "vendor.missing"})(),
            "metadata": {},
        },
    )()

    with pytest.raises(KernelCompositionError, match="vendor.missing"):
        PluginKernelComposer(host).build(request)


def test_kernel_composer_applies_run_local_component_keys() -> None:
    class LLM:
        pass

    default_context = object()
    vendor_context = object()
    host = PluginHost()
    host.registries["llm.providers"].register(
        "default", LLM(), source="vendor.llm"
    )
    host.registries["context.providers"].register(
        "default", default_context, source="box.context"
    )
    host.registries["context.providers"].register(
        "vendor", vendor_context, source="vendor.context"
    )
    request = type(
        "Request",
        (),
        {
            "options": type(
                "Options",
                (),
                {"workflow_id": None, "component_keys": {"context": "vendor"}},
            )(),
            "metadata": {},
        },
    )()

    kernel = PluginKernelComposer(host).build(request)

    assert kernel._context is vendor_context


def test_kernel_composer_does_not_treat_single_tool_as_tool_engine() -> None:
    from box_agent.api import Message, ModelChunk, RunRequest
    from box_agent.tools.base import Tool, ToolResult
    from box_agent.tools.engine import RegistryToolEngine

    class LLM:
        async def stream(self, request):
            yield ModelChunk(content="ok", finish_reason="stop")

    class EchoTool(Tool):
        @property
        def name(self):
            return "echo"

        @property
        def description(self):
            return "echo"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"text": {"type": "string"}}}

        async def execute(self, **arguments):
            return ToolResult(success=True, content=arguments["text"])

    host = PluginHost()
    host.registries["llm.providers"].register("default", LLM(), source="test")
    # A raw Tool under ``default`` must still produce a registry-backed engine.
    host.registries["tools.executors"].register("default", EchoTool(), source="test")
    kernel = PluginKernelComposer(host).build(
        RunRequest(
            request_id="request-tool-engine",
            session_id="session-tool-engine",
            turn_id="turn-tool-engine",
            user_input=Message.user("hello"),
        )
    )

    assert isinstance(kernel._tools, RegistryToolEngine)
    assert any(schema["name"] == "echo" for schema in kernel._tool_schemas())


def test_kernel_composer_combines_split_memory_provider_and_writer() -> None:
    from box_agent.memory_engine import MemoryEntry, MemoryQuery, MemoryRecall

    class LLM:
        pass

    class Provider:
        async def recall(self, query: MemoryQuery) -> MemoryRecall:
            return MemoryRecall(entries=(), query=query)

    class Writer:
        def __init__(self) -> None:
            self.writes = []

        async def write(self, entry: MemoryEntry) -> MemoryEntry:
            self.writes.append(entry.entry_id)
            return entry

    host = PluginHost()
    host.registries["llm.providers"].register("default", LLM(), source="test")
    provider = Provider()
    writer = Writer()
    host.registries["memory.providers"].register("default", provider, source="test")
    host.registries["memory.writers"].register("default", writer, source="test")

    kernel = PluginKernelComposer(host).build()

    assert kernel._memory is not provider
    assert all(callable(getattr(kernel._memory, name, None)) for name in ("recall", "write", "flush"))


def test_kernel_composer_combines_context_provider_and_compactor() -> None:
    from box_agent.context import ContextBuildResult, ContextItem

    class LLM:
        pass

    class Provider:
        async def provide(self, request):
            return (ContextItem(item_id="provider", content="provider"),)

    class Compactor:
        async def compact(self, request):
            return ContextBuildResult(
                items=request.items[:1],
                estimated_tokens=request.items[0].estimated_tokens,
                compacted=True,
            )

    host = PluginHost()
    host.registries["llm.providers"].register("default", LLM(), source="test")
    provider = Provider()
    host.registries["context.providers"].register("default", provider, source="test")
    host.registries["context.compactors"].register(
        "default", Compactor(), source="test"
    )

    kernel = PluginKernelComposer(host).build()

    assert kernel._context.__class__.__name__ == "CompositeContextEngine"


def test_kernel_composer_fails_when_required_llm_is_missing() -> None:
    with pytest.raises(KernelCompositionError):
        PluginKernelComposer(PluginHost()).build()
