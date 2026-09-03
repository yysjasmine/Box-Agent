"""Typed registry names and plugin state snapshot contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path

from box_agent.api import Message, ModelChunk, RunRequest, SessionOpenRequest
from box_agent.kernel import PluginKernelComposer
from box_agent.persistence import SQLiteEventLog
from box_agent.plugins import PluginHost, PluginManifest
from box_agent.services.kernel import KernelAgentService


class _LLM:
    async def stream(self, request):
        yield ModelChunk(content="done", finish_reason="stop")


def test_composer_resolves_canonical_llm_provider_registry() -> None:
    host = PluginHost()
    provider = _LLM()
    host.registries["llm.providers"].register(
        "default", provider, source="vendor.llm"
    )

    kernel = PluginKernelComposer(host).build()

    assert kernel._llm is provider


def test_kernel_composer_uses_registered_hooks_by_default() -> None:
    class Hook:
        async def on_event(self, event):
            del event

    host = PluginHost()
    provider = _LLM()
    hook = Hook()
    host.registries["llm.providers"].register(
        "default", provider, source="vendor.llm"
    )
    host.registries["hooks"].register("audit", hook, source="vendor.hook")

    kernel = PluginKernelComposer(host).build()

    assert kernel._hooks == (hook,)


def test_kernel_composer_allows_explicit_empty_hook_selection() -> None:
    class Hook:
        async def on_event(self, event):
            del event

    host = PluginHost()
    host.registries["llm.providers"].register(
        "default", _LLM(), source="vendor.llm"
    )
    host.registries["hooks"].register("audit", Hook(), source="vendor.hook")

    kernel = PluginKernelComposer(host, hook_keys=()).build()

    assert kernel._hooks == ()


def test_plugin_snapshot_is_persisted_at_a_checkpoint(tmp_path: Path) -> None:
    class StatefulPlugin:
        manifest = PluginManifest(
            id="vendor.stateful",
            version="1.0.0",
            state_schema="goal.v1",
        )

        async def activate(self, ctx):
            self.value = "active"

        async def deactivate(self):
            return None

        async def dispose(self):
            return None

        async def snapshot(self):
            return {"value": self.value}

    class Kernel:
        async def run(self, request, *, emit, cancel_event, controls=None):
            from box_agent.api import AgentEvent, RunResult

            await emit(
                AgentEvent(
                    event_id="snapshot-done",
                    sequence=1,
                    session_id=request.session_id,
                    run_id=request.metadata["run_id"],
                    turn_id=request.turn_id,
                    type="run.completed",
                    payload={"stop_reason": "done", "final_content": "ok"},
                )
            )
            return RunResult(status="completed", stop_reason="done", final_message="ok")

    async def scenario() -> None:
        host = PluginHost()
        plugin = StatefulPlugin()
        await host.activate(plugin)
        log = SQLiteEventLog(tmp_path / "plugin-snapshot.sqlite3")
        service = KernelAgentService(
            kernel=Kernel(),
            event_log=log,
            plugin_lock=host.lock_snapshot(),
            plugin_snapshot_provider=host.snapshot,
        )
        await service.open_session(SessionOpenRequest(session_id="snapshot-session"))
        handle = await service.start(
            RunRequest(
                request_id="snapshot-request",
                session_id="snapshot-session",
                turn_id="snapshot-turn",
                user_input=Message.user("snapshot"),
            )
        )
        await handle.wait()
        bundle = await log.load_recovery_bundle(handle.run_id)
        assert bundle.checkpoint is not None
        assert bundle.checkpoint.plugin_snapshot["vendor.stateful"]["state"] == {
            "value": "active"
        }
        assert (
            bundle.checkpoint.plugin_snapshot["vendor.stateful"]["schema_version"]
            == "goal.v1"
        )
        log.close()

    asyncio.run(scenario())


def test_plugin_snapshot_restore_validates_schema_and_restores_state() -> None:
    class StatefulPlugin:
        manifest = PluginManifest(
            id="vendor.restore",
            version="1.0.0",
            state_schema="state.v1",
        )

        def __init__(self) -> None:
            self.value = "empty"

        async def activate(self, ctx):
            del ctx

        async def deactivate(self):
            return None

        async def dispose(self):
            return None

        async def restore(self, state):
            self.value = state["value"]

    async def scenario() -> None:
        host = PluginHost()
        plugin = StatefulPlugin()
        await host.activate(plugin)
        await host.restore_snapshot(
            {
                "vendor.restore": {
                    "version": "1.0.0",
                    "schema_version": "state.v1",
                    "state": {"value": "restored"},
                }
            }
        )
        assert plugin.value == "restored"

    asyncio.run(scenario())


def test_service_restores_plugin_snapshot_before_replay(tmp_path: Path) -> None:
    class StatefulPlugin:
        manifest = PluginManifest(
            id="vendor.service-state",
            version="1.0.0",
            state_schema="state.v1",
        )

        def __init__(self, value: str) -> None:
            self.value = value
            self.restored = False

        async def activate(self, ctx):
            del ctx

        async def deactivate(self):
            return None

        async def dispose(self):
            return None

        async def snapshot(self):
            return {"value": self.value}

        async def restore(self, state):
            self.value = state["value"]
            self.restored = True

    class Kernel:
        async def run(self, request, *, emit, cancel_event, controls=None):
            from box_agent.api import AgentEvent, RunResult

            await emit(
                AgentEvent(
                    event_id="service-state-done",
                    sequence=1,
                    session_id=request.session_id,
                    run_id=request.metadata["run_id"],
                    turn_id=request.turn_id,
                    type="run.completed",
                    payload={"stop_reason": "done", "final_content": "ok"},
                )
            )
            return RunResult(status="completed", stop_reason="done", final_message="ok")

    async def scenario() -> None:
        log_path = tmp_path / "service-state.sqlite3"
        first_host = PluginHost()
        first_plugin = StatefulPlugin("first")
        await first_host.activate(first_plugin)
        log = SQLiteEventLog(log_path)
        first = KernelAgentService(
            kernel=Kernel(),
            event_log=log,
            plugin_lock=first_host.lock_snapshot(),
            plugin_snapshot_provider=first_host.snapshot,
        )
        await first.open_session(SessionOpenRequest(session_id="service-state"))
        handle = await first.start(
            RunRequest(
                request_id="service-state-request",
                session_id="service-state",
                turn_id="service-state-turn",
                user_input=Message.user("state"),
            )
        )
        await handle.wait()

        second_host = PluginHost()
        second_plugin = StatefulPlugin("empty")
        await second_host.activate(second_plugin)
        restarted = KernelAgentService(
            kernel=Kernel(),
            event_log=SQLiteEventLog(log_path),
            plugin_lock=second_host.lock_snapshot(),
            plugin_snapshot_provider=second_host.snapshot,
            plugin_restore_provider=second_host.restore_snapshot,
        )
        resumed = await restarted.resume(handle.run_id)
        await resumed.wait()
        assert second_plugin.restored is True
        assert second_plugin.value == "first"
        log.close()

    asyncio.run(scenario())
