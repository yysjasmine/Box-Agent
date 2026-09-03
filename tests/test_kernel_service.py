"""Service contract tests for the Agent Loop Kernel path."""

from __future__ import annotations

import asyncio

import pytest

from box_agent.api import (
    AgentEvent,
    ControlCommand,
    ErrorCode,
    Message,
    RunRequest,
    RunResult,
    SessionOpenRequest,
    ModelChunk,
    Usage,
)
from box_agent.kernel_service import KernelAgentService
from box_agent.persistence import SQLiteEventLog
from box_agent.persistence import SQLiteLeaseStore
from box_agent.persistence import SQLiteSessionStore
from box_agent.persistence import PersistenceConflictError
from box_agent.plugins import PluginHost, PluginManifest


class FakeKernel:
    async def run(self, request, *, emit, cancel_event, controls=None):
        await emit(
            AgentEvent(
                event_id=f"event-{request.metadata['run_id']}",
                sequence=1,
                session_id=request.session_id,
                run_id=request.metadata["run_id"],
                turn_id=request.turn_id,
                type="run.completed",
                payload={"stop_reason": "test", "final_content": "done"},
            )
        )
        return RunResult(status="completed", stop_reason="test", final_message="done")


@pytest.mark.asyncio
async def test_service_notifies_session_lifecycle_plugins_on_close() -> None:
    closed: list[str] = []

    class Lifecycle:
        async def on_session_close(self, session_id: str) -> None:
            closed.append(session_id)

    host = PluginHost()
    host.registries["session.lifecycle"].register(
        "resource-owner",
        Lifecycle(),
        source="test.lifecycle",
    )
    service = KernelAgentService.from_plugin_host(host)
    await service.open_session(SessionOpenRequest(session_id="session-1"))

    await service.close_session("session-1")

    assert closed == ["session-1"]


@pytest.mark.asyncio
async def test_kernel_service_runs_new_loop_and_replays_after_restart(tmp_path) -> None:
    db_path = tmp_path / "runs.sqlite3"
    request = RunRequest(
        request_id="request-1",
        session_id="session-1",
        turn_id="turn-1",
        user_input=Message.user("hello"),
    )
    first = KernelAgentService(kernel_factory=FakeKernel, event_log=SQLiteEventLog(db_path))
    await first.open_session(SessionOpenRequest(session_id="session-1"))
    handle = await first.start(request)
    events = [event async for event in handle.events()]
    await handle.wait()

    restarted = KernelAgentService(
        kernel_factory=FakeKernel, event_log=SQLiteEventLog(db_path)
    )
    resumed = await restarted.resume(handle.run_id)
    replayed = [event async for event in resumed.events(after_sequence=0)]

    assert events == replayed
    assert (await resumed.wait()).final_message == "done"


@pytest.mark.asyncio
async def test_kernel_service_reconstructs_total_usage_from_all_model_calls(
    tmp_path,
) -> None:
    class UsageKernel:
        async def run(self, request, *, emit, cancel_event, controls=None):
            del cancel_event, controls
            run_id = request.metadata["run_id"]
            for sequence, usage in enumerate(
                (
                    Usage(input_tokens=2, output_tokens=1, total_tokens=3),
                    Usage(input_tokens=4, output_tokens=2, total_tokens=6),
                ),
                start=1,
            ):
                await emit(
                    AgentEvent(
                        event_id=f"response-{sequence}",
                        sequence=sequence,
                        session_id=request.session_id,
                        run_id=run_id,
                        turn_id=request.turn_id,
                        type="model.response.completed",
                        payload={"usage": usage.to_dict()},
                    )
                )
            await emit(
                AgentEvent(
                    event_id="usage-complete",
                    sequence=3,
                    session_id=request.session_id,
                    run_id=run_id,
                    turn_id=request.turn_id,
                    type="run.completed",
                    payload={"stop_reason": "stop", "final_content": "done"},
                )
            )
            return RunResult(
                status="completed",
                stop_reason="stop",
                final_message="done",
                usage=Usage(input_tokens=6, output_tokens=3, total_tokens=9),
            )

    db_path = tmp_path / "usage.sqlite3"
    request = RunRequest(
        request_id="usage-request",
        session_id="usage-session",
        turn_id="usage-turn",
        user_input=Message.user("measure"),
    )
    first = KernelAgentService(
        kernel_factory=UsageKernel,
        event_log=SQLiteEventLog(db_path),
    )
    await first.open_session(SessionOpenRequest(session_id=request.session_id))
    handle = await first.start(request)
    assert (await handle.wait()).usage.total_tokens == 9

    restarted = KernelAgentService(
        kernel_factory=UsageKernel,
        event_log=SQLiteEventLog(db_path),
    )
    resumed = await restarted.resume(handle.run_id)

    assert (await resumed.wait()).usage == Usage(
        input_tokens=6,
        output_tokens=3,
        total_tokens=9,
    )


@pytest.mark.asyncio
async def test_kernel_service_rejects_unsupported_controls_structurally() -> None:
    service = KernelAgentService(kernel_factory=FakeKernel)
    await service.open_session(SessionOpenRequest(session_id="session-1"))
    handle = await service.start(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        )
    )

    ack = await handle.send(
        ControlCommand(
            command_id="command-1",
            session_id="session-1",
            run_id=handle.run_id,
            kind="run.pause",
            payload={},
            source="test",
        )
    )
    await handle.wait()

    assert ack.accepted is False
    assert ack.error is not None
    assert ack.error.code == ErrorCode.UNSUPPORTED_COMMAND


@pytest.mark.asyncio
async def test_kernel_service_accepts_external_control_extension() -> None:
    seen: list[ControlCommand] = []

    async def control_handler(command: ControlCommand) -> bool:
        seen.append(command)
        return True

    service = KernelAgentService(kernel_factory=FakeKernel, control_handler=control_handler)
    await service.open_session(SessionOpenRequest(session_id="session-1"))
    handle = await service.start(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        )
    )

    command = ControlCommand(
        command_id="command-1",
        session_id="session-1",
        run_id=handle.run_id,
        kind="run.pause",
        payload={"reason": "operator"},
        source="test",
    )
    ack = await handle.send(command)
    repeated = await handle.send(command)
    await handle.wait()

    assert ack.accepted is True
    assert repeated.to_dict() == ack.to_dict()
    assert seen == [command]


@pytest.mark.asyncio
async def test_custom_control_ack_is_idempotent_across_service_restart(tmp_path) -> None:
    db_path = tmp_path / "controls.sqlite3"
    seen: list[str] = []

    class ControlKernel:
        async def run(self, request, *, emit, cancel_event, controls=None):
            await asyncio.sleep(0.05)
            await emit(
                AgentEvent(
                    event_id="done",
                    sequence=1,
                    session_id=request.session_id,
                    run_id=request.metadata["run_id"],
                    turn_id=request.turn_id,
                    type="run.completed",
                    payload={"stop_reason": "done", "final_content": "ok"},
                )
            )
            return RunResult(status="completed", stop_reason="done", final_message="ok")

    async def route(command):
        seen.append(command.command_id)
        return True

    request = RunRequest(
        request_id="request-1",
        session_id="session-1",
        turn_id="turn-1",
        user_input=Message.user("hello"),
    )
    first = KernelAgentService(
        kernel_factory=ControlKernel,
        event_log=SQLiteEventLog(db_path),
        control_handler=route,
    )
    await first.open_session(SessionOpenRequest(session_id="session-1"))
    handle = await first.start(request)
    command = ControlCommand(
        command_id="command-1",
        session_id="session-1",
        run_id=handle.run_id,
        kind="run.pause",
        payload={"reason": "operator"},
        source="test",
    )
    assert (await handle.send(command)).accepted is True
    await handle.wait()

    restarted = KernelAgentService(
        kernel_factory=ControlKernel,
        event_log=SQLiteEventLog(db_path),
        control_handler=route,
    )
    resumed = await restarted.resume(handle.run_id)
    repeated = await resumed.send(command)

    assert repeated.accepted is True
    assert seen == ["command-1"]


@pytest.mark.asyncio
async def test_plugin_control_route_extends_service_without_core_changes() -> None:
    seen: list[str] = []

    async def pause_route(command: ControlCommand) -> bool:
        seen.append(command.kind)
        return True

    class PluginLLM:
        async def stream(self, request):
            yield ModelChunk(content="done", finish_reason="stop")

    host = PluginHost()
    host.registries["llm"].register("default", PluginLLM(), source="test.llm")
    host.registries["control.routes"].register(
        "run.pause", pause_route, source="vendor.controls"
    )
    service = KernelAgentService.from_plugin_host(host)
    await service.open_session(SessionOpenRequest(session_id="session-1"))
    handle = await service.start(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        )
    )
    ack = await handle.send(
        ControlCommand(
            command_id="command-1",
            session_id="session-1",
            run_id=handle.run_id,
            kind="run.pause",
            payload={},
            source="test",
        )
    )
    await handle.wait()

    assert ack.accepted is True
    assert seen == ["run.pause"]


@pytest.mark.asyncio
async def test_kernel_service_accepts_builtin_run_injection_without_route() -> None:
    class SlowKernel:
        async def run(self, request, *, emit, cancel_event, controls=None):
            command = await anext(controls)
            await emit(
                AgentEvent(
                    event_id="control",
                    sequence=1,
                    session_id=request.session_id,
                    run_id=request.metadata["run_id"],
                    turn_id=request.turn_id,
                    type="control.received",
                    payload=command.to_dict(),
                )
            )
            return RunResult(status="completed", stop_reason="stop", final_message="ok")

    service = KernelAgentService(kernel_factory=SlowKernel)
    await service.open_session(SessionOpenRequest(session_id="session-1"))
    handle = await service.start(
        RunRequest(
            request_id="request-inject",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        )
    )
    ack = await handle.send(
        ControlCommand(
            command_id="inject-1",
            session_id="session-1",
            run_id=handle.run_id,
            kind="run.inject",
            payload={"injection_id": "note-1", "text": "continue"},
            source="test",
        )
    )

    assert ack.accepted is True
    assert (await handle.wait()).status == "completed"


@pytest.mark.asyncio
async def test_kernel_service_normalizes_control_handler_failure() -> None:
    def control_handler(command: ControlCommand):
        raise RuntimeError("boom")

    service = KernelAgentService(kernel_factory=FakeKernel, control_handler=control_handler)
    await service.open_session(SessionOpenRequest(session_id="session-1"))
    handle = await service.start(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        )
    )

    ack = await handle.send(
        ControlCommand(
            command_id="command-1",
            session_id="session-1",
            run_id=handle.run_id,
            kind="run.pause",
            payload={},
            source="test",
        )
    )
    await handle.wait()

    assert ack.accepted is False
    assert ack.error is not None
    assert ack.error.category == "control"


@pytest.mark.asyncio
async def test_kernel_service_continues_unfinished_run_after_durable_events(tmp_path) -> None:
    db_path = tmp_path / "runs.sqlite3"
    event_log = SQLiteEventLog(db_path)
    request = RunRequest(
        request_id="request-1",
        session_id="session-1",
        turn_id="turn-1",
        user_input=Message.user("hello"),
    )
    run_id = "run-recovered"
    await event_log.register_run(run_id, request.to_dict())
    await event_log.append(
        AgentEvent(
            event_id="event-before-crash",
            sequence=1,
            session_id="session-1",
            run_id=run_id,
            turn_id="turn-1",
            type="model.content.delta",
            payload={"content": "before crash"},
        )
    )

    created: list[tuple[RunRequest, object]] = []

    class ContinuationKernel:
        async def run(self, restored_request, *, emit, cancel_event, controls=None):
            await emit(
                AgentEvent(
                    event_id="event-after-restart",
                    sequence=1,
                    session_id=restored_request.session_id,
                    run_id=run_id,
                    turn_id=restored_request.turn_id,
                    type="run.completed",
                    payload={"stop_reason": "resumed", "final_content": "done"},
                )
            )
            return RunResult(status="completed", stop_reason="resumed", final_message="done")

    def factory(restored_request, bundle):
        created.append((restored_request, bundle))
        return ContinuationKernel()

    service = KernelAgentService(kernel_factory=factory, event_log=SQLiteEventLog(db_path))
    handle = await service.resume(run_id)
    events = [event async for event in handle.events(after_sequence=1)]
    result = await handle.wait()

    assert created and created[0][0].request_id == request.request_id
    assert [event.sequence for event in events] == [2]
    assert result.final_message == "done"


@pytest.mark.asyncio
async def test_plugin_composed_kernel_rebuilds_context_from_recovery_events(tmp_path) -> None:
    db_path = tmp_path / "runs.sqlite3"
    event_log = SQLiteEventLog(db_path)
    run_id = "run-recovered"
    request = RunRequest(
        request_id="request-1",
        session_id="session-1",
        turn_id="turn-1",
        user_input=Message.user("continue the task"),
    )
    await event_log.register_run(run_id, request.to_dict())
    await event_log.append(
        AgentEvent(
            event_id="model-before-crash",
            sequence=1,
            session_id="session-1",
            run_id=run_id,
            turn_id="turn-1",
            type="model.response.completed",
            payload={"content": "", "tool_call_ids": ["call-1"], "finish_reason": "tool_calls"},
        )
    )
    await event_log.append(
        AgentEvent(
            event_id="tool-before-crash",
            sequence=2,
            session_id="session-1",
            run_id=run_id,
            turn_id="turn-1",
            type="tool.call.completed",
            payload={"call_id": "call-1", "status": "succeeded", "content": "tool result"},
        )
    )

    class ResumingLLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, llm_request):
            self.requests.append(llm_request)
            yield ModelChunk(content="recovered answer", finish_reason="stop")

    llm = ResumingLLM()
    host = PluginHost()
    host.registries["llm"].register("default", llm, source="test")
    service = KernelAgentService.from_plugin_host(host, event_log=event_log)

    handle = await service.resume(run_id)
    result = await handle.wait()

    assert result.final_message == "recovered answer"
    roles = [message.role for message in llm.requests[0].messages]
    assert roles == ["user", "assistant", "tool"]
    assert llm.requests[0].messages[-1].content == "tool result"
    again = await service.start(request)
    assert again.run_id == run_id


@pytest.mark.asyncio
async def test_new_turn_rehydrates_prior_session_facts_without_advancing_run_budget(tmp_path) -> None:
    event_log = SQLiteEventLog(tmp_path / "runs.sqlite3")
    captured: list[dict] = []

    class SessionKernel:
        def __init__(self, request):
            self.request = request

        async def run(self, request, *, emit, cancel_event, controls=None):
            captured.append(dict(request.metadata))
            await emit(
                AgentEvent(
                    event_id=f"model-{request.turn_id}",
                    sequence=1,
                    session_id=request.session_id,
                    run_id=request.metadata["run_id"],
                    turn_id=request.turn_id,
                    type="model.response.completed",
                    payload={"content": f"answer-{request.turn_id}", "tool_call_ids": []},
                )
            )
            await emit(
                AgentEvent(
                    event_id=f"done-{request.turn_id}",
                    sequence=2,
                    session_id=request.session_id,
                    run_id=request.metadata["run_id"],
                    turn_id=request.turn_id,
                    type="run.completed",
                    payload={"stop_reason": "stop", "final_content": f"answer-{request.turn_id}"},
                )
            )
            return RunResult(
                status="completed",
                stop_reason="stop",
                final_message=f"answer-{request.turn_id}",
            )

    def factory(request, _bundle=None):
        return SessionKernel(request)

    service = KernelAgentService(kernel_factory=factory, event_log=event_log)
    await service.open_session(SessionOpenRequest(session_id="session-1"))
    first = await service.start(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("first"),
        )
    )
    await first.wait()
    second = await service.start(
        RunRequest(
            request_id="request-2",
            session_id="session-1",
            turn_id="turn-2",
            user_input=Message.user("second"),
        )
    )
    await second.wait()

    assert len(captured) == 2
    prior = captured[1]["_session_events"]["events"]
    assert [event["type"] for event in prior] == [
        "model.response.completed",
        "run.completed",
    ]


@pytest.mark.asyncio
async def test_kernel_service_composes_kernel_from_plugin_registries() -> None:
    class PluginLLM:
        async def stream(self, request):
            yield ModelChunk(content="plugin answer", finish_reason="stop")

    host = PluginHost()
    host.registries["llm"].register("default", PluginLLM(), source="vendor.llm")
    service = KernelAgentService.from_plugin_host(host)
    await service.open_session(SessionOpenRequest(session_id="session-1"))
    handle = await service.start(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        )
    )

    result = await handle.wait()

    assert result.status == "completed"
    assert result.final_message == "plugin answer"


@pytest.mark.asyncio
async def test_kernel_service_releases_run_lease_after_terminal_result(tmp_path) -> None:
    lease_store = SQLiteLeaseStore(tmp_path / "leases.sqlite3")
    service = KernelAgentService(
        kernel_factory=FakeKernel,
        lease_store=lease_store,
        owner_id="worker-1",
    )
    await service.open_session(SessionOpenRequest(session_id="session-1"))
    handle = await service.start(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        )
    )

    await handle.wait()

    assert await lease_store.get(handle.run_id) is None


@pytest.mark.asyncio
async def test_kernel_service_renews_long_running_lease() -> None:
    class Lease:
        def __init__(self) -> None:
            self.run_id = ""
            self.owner_id = "worker"
            self.epoch = 1
            self.expires_at = 0.0

    class LeaseStore:
        def __init__(self) -> None:
            self.lease = None
            self.renewals = 0

        async def acquire(self, run_id, *, owner_id, ttl_seconds):
            self.lease = Lease()
            self.lease.run_id = run_id
            self.lease.owner_id = owner_id
            return self.lease

        async def renew(self, lease, *, ttl_seconds):
            self.renewals += 1
            return lease

        async def release(self, lease):
            self.lease = None
            return True

    class SlowKernel:
        async def run(self, request, *, emit, cancel_event, controls=None):
            await asyncio.sleep(0.15)
            await emit(
                AgentEvent(
                    event_id="done",
                    sequence=1,
                    session_id=request.session_id,
                    run_id=request.metadata["run_id"],
                    turn_id=request.turn_id,
                    type="run.completed",
                    payload={"stop_reason": "done", "final_content": "ok"},
                )
            )
            return RunResult(status="completed", stop_reason="done", final_message="ok")

    leases = LeaseStore()
    service = KernelAgentService(
        kernel_factory=SlowKernel,
        lease_store=leases,
        owner_id="worker",
        lease_ttl_seconds=0.03,
    )
    await service.open_session(SessionOpenRequest(session_id="session-1"))
    handle = await service.start(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        )
    )

    assert (await handle.wait()).status == "completed"
    assert leases.renewals >= 1


@pytest.mark.asyncio
async def test_kernel_service_loads_persisted_session_before_start_after_restart(tmp_path) -> None:
    db_path = tmp_path / "runs.sqlite3"
    sessions = SQLiteSessionStore(db_path)
    events = SQLiteEventLog(db_path)
    request = RunRequest(
        request_id="request-1",
        session_id="session-1",
        turn_id="turn-1",
        user_input=Message.user("hello"),
    )
    first = KernelAgentService(kernel_factory=FakeKernel, event_log=events, session_store=sessions)
    await first.open_session(SessionOpenRequest(session_id="session-1", metadata={"mode": "code"}))
    handle = await first.start(request)
    await handle.wait()

    restarted = KernelAgentService(
        kernel_factory=FakeKernel,
        event_log=SQLiteEventLog(db_path),
        session_store=SQLiteSessionStore(db_path),
    )
    resumed = await restarted.start(request)

    assert (await resumed.wait()).final_message == "done"


@pytest.mark.asyncio
async def test_kernel_service_load_session_rejects_unknown_and_restores_existing(tmp_path) -> None:
    db_path = tmp_path / "sessions.sqlite3"
    service = KernelAgentService(
        kernel_factory=FakeKernel,
        session_store=SQLiteSessionStore(db_path),
    )

    with pytest.raises(ValueError, match="unknown durable session"):
        await service.load_session(SessionOpenRequest(session_id="missing"))

    created = await service.open_session(
        SessionOpenRequest(session_id="session-1", metadata={"mode": "code"})
    )
    loaded = await service.load_session(
        SessionOpenRequest(session_id="session-1", metadata={"ignored": True})
    )

    assert loaded == created
    await service.close_session("session-1")
    with pytest.raises(ValueError, match="unknown durable session"):
        await service.load_session(SessionOpenRequest(session_id="session-1"))


@pytest.mark.asyncio
async def test_kernel_service_updates_durable_session_metadata(tmp_path) -> None:
    db_path = tmp_path / "session-update.sqlite3"
    service = KernelAgentService(
        kernel_factory=FakeKernel,
        session_store=SQLiteSessionStore(db_path),
    )
    created = await service.open_session(
        SessionOpenRequest(session_id="session-1", metadata={"mode": "code"})
    )

    updated = await service.update_session_metadata(
        created.session_id,
        {"mode": "code", "goal": {"objective": "Ship", "status": "active"}},
    )

    assert updated.created_at == created.created_at
    restarted = KernelAgentService(
        kernel_factory=FakeKernel,
        session_store=SQLiteSessionStore(db_path),
    )
    restored = await restarted.load_session(
        SessionOpenRequest(session_id="session-1")
    )
    assert restored.metadata["goal"]["objective"] == "Ship"


@pytest.mark.asyncio
async def test_kernel_service_rejects_reused_request_id_with_different_input() -> None:
    service = KernelAgentService(kernel_factory=FakeKernel)
    await service.open_session(SessionOpenRequest(session_id="session-1"))
    request = RunRequest(
        request_id="request-1",
        session_id="session-1",
        turn_id="turn-1",
        user_input=Message.user("hello"),
    )
    await service.start(request)

    with pytest.raises(PersistenceConflictError):
        await service.start(
            RunRequest(
                request_id="request-1",
                session_id="session-1",
                turn_id="turn-1",
                user_input=Message.user("different"),
            )
        )


@pytest.mark.asyncio
async def test_kernel_service_rejects_resume_with_different_plugin_lock(tmp_path) -> None:
    db_path = tmp_path / "plugin-lock.sqlite3"

    class PluginLLM:
        async def stream(self, request):
            yield ModelChunk(content="done", finish_reason="stop")

    host = PluginHost()
    host.registries["llm"].register("default", PluginLLM(), source="test")
    # Register an active plugin solely to make the lock observable.
    class Plugin:
        manifest = PluginManifest(id="vendor.llm", version="1.0.0")

        async def activate(self, ctx):
            pass

        async def deactivate(self):
            pass

        async def dispose(self):
            pass

    await host.activate(Plugin())
    first = KernelAgentService.from_plugin_host(
        host,
        event_log=SQLiteEventLog(db_path),
    )
    await first.open_session(SessionOpenRequest(session_id="session-1"))
    handle = await first.start(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        )
    )
    await handle.wait()

    restarted = KernelAgentService(
        kernel_factory=lambda: PluginLLM(),
        event_log=SQLiteEventLog(db_path),
        plugin_lock={"vendor.llm": "2.0.0"},
    )
    with pytest.raises(PersistenceConflictError):
        await restarted.resume(handle.run_id)


@pytest.mark.asyncio
async def test_kernel_service_captures_current_plugin_lock_for_each_new_run(
    tmp_path,
) -> None:
    db_path = tmp_path / "dynamic-plugin-lock.sqlite3"
    active_lock = {"vendor.dynamic": "1.0.0"}
    event_log = SQLiteEventLog(db_path)
    service = KernelAgentService(
        kernel_factory=FakeKernel,
        event_log=event_log,
        plugin_lock_provider=lambda: dict(active_lock),
    )
    await service.open_session(SessionOpenRequest(session_id="session-1"))

    first = await service.start(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("first"),
        )
    )
    await first.wait()
    active_lock["vendor.dynamic"] = "2.0.0"
    second = await service.start(
        RunRequest(
            request_id="request-2",
            session_id="session-1",
            turn_id="turn-2",
            user_input=Message.user("second"),
        )
    )
    await second.wait()

    first_request = await event_log.get_run_request(first.run_id)
    second_request = await event_log.get_run_request(second.run_id)
    assert first_request["metadata"]["plugin_lock"]["vendor.dynamic"] == "1.0.0"
    assert second_request["metadata"]["plugin_lock"]["vendor.dynamic"] == "2.0.0"
    first_checkpoint = (await event_log.load_recovery_bundle(first.run_id)).checkpoint
    second_checkpoint = (await event_log.load_recovery_bundle(second.run_id)).checkpoint
    assert first_checkpoint is not None
    assert second_checkpoint is not None
    assert first_checkpoint.plugin_lock["vendor.dynamic"] == "1.0.0"
    assert second_checkpoint.plugin_lock["vendor.dynamic"] == "2.0.0"


@pytest.mark.asyncio
async def test_kernel_service_exposes_only_nonterminal_active_runs() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingKernel:
        async def run(self, request, *, emit, cancel_event, controls=None):
            del cancel_event, controls
            entered.set()
            await release.wait()
            await emit(
                AgentEvent(
                    event_id=f"event-{request.metadata['run_id']}",
                    sequence=1,
                    session_id=request.session_id,
                    run_id=request.metadata["run_id"],
                    turn_id=request.turn_id,
                    type="run.completed",
                    payload={"stop_reason": "test", "final_content": "done"},
                )
            )
            return RunResult(
                status="completed", stop_reason="test", final_message="done"
            )

    service = KernelAgentService(kernel_factory=BlockingKernel)
    await service.open_session(SessionOpenRequest(session_id="session-1"))
    handle = await service.start(
        RunRequest(
            request_id="request-active",
            session_id="session-1",
            turn_id="turn-active",
            user_input=Message.user("wait"),
        )
    )
    await entered.wait()

    assert service.active_run_ids() == (handle.run_id,)

    release.set()
    await handle.wait()
    assert service.active_run_ids() == ()
