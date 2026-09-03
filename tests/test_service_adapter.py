"""Host payload adapters remain thinner than the runtime kernel."""

from __future__ import annotations

import pytest

from box_agent.adapters import SDKServiceAdapter, ServiceAdapter, request_from_payload
from box_agent.api import AgentEvent, RunResult


class Service:
    async def open_session(self, request):
        from box_agent.api import SessionInfo

        return SessionInfo(request.session_id or "generated", "now", request.metadata)

    async def load_session(self, request):
        from box_agent.api import SessionInfo

        if request.session_id != "persisted":
            raise ValueError(f"unknown durable session: {request.session_id}")
        return SessionInfo("persisted", "now", {"source": "store"})

    async def start(self, request):
        from box_agent.api import CommandAck, RunStatus

        class Handle:
            run_id = "run-1"

            async def events(self, after_sequence=0):
                yield AgentEvent(
                    event_id="event-1",
                    sequence=1,
                    session_id=request.session_id,
                    run_id=self.run_id,
                    type="run.completed",
                    payload={"final_content": "done"},
                )

            async def wait(self):
                return RunResult(status="completed", stop_reason="stop", final_message="done")

            async def send(self, command):
                return CommandAck(command.command_id, True, "accepted")

        return Handle()

    async def resume(self, run_id):
        return await self.start(None)


@pytest.mark.asyncio
async def test_service_adapter_forwards_normalized_events_to_sink() -> None:
    seen: list[dict] = []
    result = await ServiceAdapter(Service()).run_to_sink(
        {"session_id": "session-1", "message": "hello"},
        seen.append,
    )

    assert seen[0]["type"] == "run.completed"
    assert result["final_message"] == "done"


@pytest.mark.asyncio
async def test_sdk_adapter_uses_the_same_service_contract_without_rendering() -> None:
    result = await SDKServiceAdapter(Service()).run(
        {"session_id": "session-1", "message": "hello"}
    )

    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_service_adapter_loads_existing_session_without_opening_one() -> None:
    service = Service()

    loaded = await ServiceAdapter(service).load_session(
        {"session_id": "persisted", "metadata": {"ignored": True}}
    )

    assert loaded["session_id"] == "persisted"
    assert loaded["metadata"] == {"source": "store"}


@pytest.mark.asyncio
async def test_acp_service_adapter_exposes_strict_session_load() -> None:
    from box_agent.adapters import ACPServiceAdapter

    result = await ACPServiceAdapter(Service()).handle_request(
        {"operation": "session.load", "session_id": "persisted"}
    )

    assert result["session"]["session_id"] == "persisted"


@pytest.mark.asyncio
async def test_acp_wait_observes_without_resuming_a_run() -> None:
    from box_agent.adapters import ACPServiceAdapter

    class ReadOnlyHandle:
        async def wait(self):
            return RunResult(status="completed", stop_reason="stop", final_message="done")

    class ACPService:
        def __init__(self) -> None:
            self.attached: list[str] = []
            self.resumed: list[str] = []

        async def attach(self, run_id):
            self.attached.append(run_id)
            return ReadOnlyHandle()

        async def resume(self, run_id):  # pragma: no cover - regression sentinel
            self.resumed.append(run_id)
            raise AssertionError("run.wait must not take over a worker lease")

    service = ACPService()
    result = await ACPServiceAdapter(service).handle_request(
        {"operation": "run.wait", "run_id": "run-1"}
    )

    assert result["result"]["status"] == "completed"
    assert service.attached == ["run-1"]
    assert service.resumed == []


@pytest.mark.asyncio
async def test_one_shot_adapter_creates_a_session_when_host_omits_identity() -> None:
    service = Service()
    result = await SDKServiceAdapter(service).run({"message": "hello"})

    assert result["status"] == "completed"


def test_payload_adapter_rejects_non_mapping_options() -> None:
    with pytest.raises(ValueError, match="options"):
        request_from_payload(
            {"session_id": "session-1", "message": "hello", "options": []}
        )


def test_payload_adapter_drops_nested_provider_credentials() -> None:
    request = request_from_payload(
        {
            "session_id": "session-1",
            "message": "hello",
            "metadata": {
                "apiKey": "secret-token",
                "nested": {"access_token": "nested-secret", "trace": "keep"},
            },
        }
    )

    assert request.metadata == {"nested": {"trace": "keep"}}
