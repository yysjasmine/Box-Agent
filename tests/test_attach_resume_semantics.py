"""Observe-versus-takeover semantics for durable runs."""

from __future__ import annotations

import asyncio
from pathlib import Path

from box_agent.api import (
    AgentEvent,
    ControlCommand,
    ErrorCode,
    Message,
    RunRequest,
    RunResult,
    SessionOpenRequest,
)
from box_agent.persistence import SQLiteEventLog, SQLiteLeaseStore
from box_agent.services.kernel import KernelAgentService


def _request(request_id: str = "attach-request") -> RunRequest:
    return RunRequest(
        request_id=request_id,
        session_id="attach-session",
        turn_id=request_id,
        user_input=Message.user("hello"),
    )


class _SlowKernel:
    async def run(self, request, *, emit, cancel_event, controls=None):
        run_id = request.metadata["run_id"]
        await emit(
            AgentEvent(
                event_id=f"{run_id}-started",
                sequence=1,
                session_id=request.session_id,
                run_id=run_id,
                turn_id=request.turn_id,
                type="run.started",
                payload={},
            )
        )
        await asyncio.sleep(0.05)
        await emit(
            AgentEvent(
                event_id=f"{run_id}-done",
                sequence=2,
                session_id=request.session_id,
                run_id=run_id,
                turn_id=request.turn_id,
                type="run.completed",
                payload={"stop_reason": "done", "final_content": "ok"},
            )
        )
        return RunResult(status="completed", stop_reason="done", final_message="ok")


class _IdleKernel:
    async def run(self, request, *, emit, cancel_event, controls=None):
        await asyncio.Event().wait()


def test_attach_polls_without_acquiring_lease_and_rejects_controls(tmp_path: Path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "attach.sqlite3"
        first_log = SQLiteEventLog(db_path)
        first_leases = SQLiteLeaseStore(db_path)
        first = KernelAgentService(
            kernel_factory=_SlowKernel,
            event_log=first_log,
            lease_store=first_leases,
            owner_id="worker-a",
        )
        await first.open_session(SessionOpenRequest(session_id="attach-session"))
        running = await first.start(_request())
        await asyncio.sleep(0.01)

        class FailingAcquireLeaseStore:
            async def acquire(self, *args, **kwargs):
                raise AssertionError("attach must not acquire a lease")

        second = KernelAgentService(
            kernel_factory=_SlowKernel,
            event_log=SQLiteEventLog(db_path),
            lease_store=FailingAcquireLeaseStore(),
            owner_id="observer",
        )
        observer = await second.attach(running.run_id)
        events = [event async for event in observer.events()]
        assert [event.type for event in events] == ["run.started", "run.completed"]
        assert (await observer.wait()).final_message == "ok"

        ack = await observer.send(
            ControlCommand(
                command_id="observer-control",
                session_id="attach-session",
                run_id=running.run_id,
                kind="run.cancel",
                payload={},
                source="observer",
            )
        )
        assert ack.accepted is False
        assert ack.error is not None
        assert ack.error.code == ErrorCode.UNSUPPORTED_COMMAND
        await running.wait()
        first_log.close()

    asyncio.run(scenario())


def test_attach_and_status_accept_registered_run_before_first_event(tmp_path: Path) -> None:
    async def scenario() -> None:
        log = SQLiteEventLog(tmp_path / "attach-startup.sqlite3")
        await log.register_run(
            "startup-run",
            {
                "request_id": "startup-request",
                "session_id": "startup-session",
                "turn_id": "startup-turn",
                "user_input": {"role": "user", "content": "hello"},
                "options": {},
                "metadata": {},
            },
        )
        service = KernelAgentService(kernel=_IdleKernel(), event_log=log)

        observer = await service.attach("startup-run")
        status = await service.get_status("startup-session", "startup-run")

        assert observer.run_id == "startup-run"
        assert status.state == "running"
        assert status.sequence == 0
        assert status.terminal is False
        log.close()

    asyncio.run(scenario())
