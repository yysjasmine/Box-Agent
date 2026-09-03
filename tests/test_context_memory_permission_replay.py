"""Native Context, Memory, and Permission event contracts."""

from __future__ import annotations

import asyncio

from box_agent.api import (
    AgentEvent,
    Message,
    ModelChunk,
    PermissionDecision,
    RunRequest,
    ToolCallRequest,
    ToolCallResult,
)
from box_agent.context import InMemoryContextEngine
from box_agent.context import ContextItem
from box_agent.kernel import AgentLoopKernel
from box_agent.memory_engine import MemoryEntry, MemoryRecall
from box_agent.plugins import PluginHost
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.engine import RegistryToolEngine


class _PreflightTool(Tool):
    @property
    def name(self) -> str:
        return "preflight"

    @property
    def description(self) -> str:
        return "preflight"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def preflight(self, arguments, *, context=None):
        del arguments, context
        return ToolResult(
            success=False,
            permission_request={"scope": "test", "reason": "approval"},
        )

    async def execute(self):
        return ToolResult(success=True, content="executed")


class _Approve:
    async def decide(self, request):
        del request
        return PermissionDecision(granted=True, reason="approved")


class _Deny:
    async def decide(self, request):
        del request
        return PermissionDecision(granted=False, reason="denied by policy")


def test_permission_resolution_is_returned_as_a_stable_result_fact() -> None:
    async def scenario() -> None:
        host = PluginHost()
        host.registries["tools"].register("preflight", _PreflightTool(), source="test")
        result = await RegistryToolEngine(
            host.registries["tools"], permission_gateway=_Approve()
        ).execute(ToolCallRequest(call_id="call-1", tool_name="preflight"))

        assert result.status == "succeeded"
        assert result.permission_decision == {"granted": True, "reason": "approved", "metadata": {}}

    asyncio.run(scenario())


def test_permission_denial_is_returned_as_a_stable_result_fact() -> None:
    async def scenario() -> None:
        host = PluginHost()
        host.registries["tools"].register("preflight", _PreflightTool(), source="test")
        result = await RegistryToolEngine(
            host.registries["tools"], permission_gateway=_Deny()
        ).execute(ToolCallRequest(call_id="call-deny", tool_name="preflight"))

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "PERMISSION_DENIED"
        assert result.permission_decision == {
            "granted": False,
            "reason": "denied by policy",
            "metadata": {},
        }

    asyncio.run(scenario())


def test_legacy_direct_helper_runs_preflight_before_executor() -> None:
    from box_agent.runtime import invoke_tool_with_permissions

    class Negotiator:
        async def negotiate(self, request):
            del request
            return True

    class Guarded(Tool):
        def __init__(self) -> None:
            self.executed = 0

        @property
        def name(self):
            return "guarded"

        @property
        def description(self):
            return "guarded"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def preflight(self, arguments, *, context=None):
            del arguments, context
            return ToolResult(
                success=False,
                permission_request={"scope": "test", "reason": "approval"},
            )

        async def execute(self):
            self.executed += 1
            return ToolResult(success=True, content="ok")

    async def scenario() -> None:
        tool = Guarded()
        result, decision = await invoke_tool_with_permissions(
            tool,
            {},
            permission_negotiator=Negotiator(),
        )
        assert result.success is True
        assert decision is not None
        assert tool.executed == 1

    asyncio.run(scenario())


def test_kernel_emits_context_manifest_permission_and_memory_write_boundaries() -> None:
    class Memory:
        def __init__(self) -> None:
            self.writes: list[MemoryEntry] = []

        async def recall(self, query):
            return MemoryRecall(entries=(), query=query)

        async def write(self, entry):
            self.writes.append(entry)
            return entry

        async def flush(self):
            return None

    class LLM:
        async def stream(self, request):
            yield ModelChunk(content="done", finish_reason="stop")

    async def scenario() -> None:
        emitted: list[AgentEvent] = []
        memory = Memory()
        result = await AgentLoopKernel(
            llm=LLM(),
            context_engine=InMemoryContextEngine(),
            memory_engine=memory,
        ).run(
            RunRequest(
                request_id="request-1",
                session_id="session-1",
                turn_id="turn-1",
                user_input=Message.user("hello"),
                metadata={
                    "memory_writes": [
                        {"entry_id": "fact-1", "text": "remember this", "kind": "fact"}
                    ]
                },
            ),
            emit=emitted.append,
            cancel_event=asyncio.Event(),
        )

        assert result.status == "completed"
        assert [entry.entry_id for entry in memory.writes] == ["fact-1"]
        events = {event.type: event for event in emitted}
        assert events["context.assembled"].payload["manifest"]["content_hash"]
        assert events["memory.write.requested"].payload["entry_id"] == "fact-1"
        assert events["memory.written"].payload["entry_id"] == "fact-1"

    asyncio.run(scenario())


def test_kernel_restores_manifest_resources_before_next_model_turn() -> None:
    class RestoringContext(InMemoryContextEngine):
        def __init__(self) -> None:
            super().__init__()
            self.restored: list[str] = []

        async def restore(self, *, resource_id: str, content_version: str | None = None):
            self.restored.append(resource_id)
            return ContextItem(
                item_id=f"restored:{resource_id}",
                resource_id=resource_id,
                content="restored context",
                metadata={"role": "system"},
            )

    class LLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ModelChunk(content="done", finish_reason="stop")

    async def scenario() -> None:
        from box_agent.persistence import RecoveryBundle, RunCheckpoint

        context = RestoringContext()
        llm = LLM()
        recovery_event = AgentEvent(
            event_id="context-event",
            sequence=1,
            session_id="session-restore",
            run_id="run-restore",
            turn_id="turn-restore",
            type="context.assembled",
            payload={
                "manifest": {
                    "provider": "external",
                    "version": "v1",
                    "sources": ["doc-1"],
                    "content_hash": "sha256:context",
                    "pinned": ["restored:doc-1"],
                }
            },
        )
        checkpoint = RunCheckpoint(
            checkpoint_id="run-restore:1",
            session_id="session-restore",
            run_id="run-restore",
            sequence=1,
            state={
                "context_manifest": recovery_event.payload["manifest"],
                "context_digest": "sha256:context",
            },
            event_hash="ignored",
        )
        kernel = AgentLoopKernel(
            llm=llm,
            context_engine=context,
            recovery=RecoveryBundle(checkpoint=checkpoint, events=(recovery_event,)),
        )
        result = await kernel.run(
            RunRequest(
                request_id="request-restore",
                session_id="session-restore",
                turn_id="turn-restore",
                user_input=Message.user("continue"),
            ),
            emit=lambda event: None,
            cancel_event=asyncio.Event(),
        )
        assert result.status == "completed"
        assert context.restored == ["doc-1"]
        assert llm.requests and any(
            message.content == "restored context" for message in llm.requests[0].messages
        )

    asyncio.run(scenario())


def test_recovery_does_not_repeat_a_committed_memory_write() -> None:
    class Memory:
        def __init__(self) -> None:
            self.writes = 0

        async def recall(self, query):
            return MemoryRecall(entries=(), query=query)

        async def write(self, entry):
            self.writes += 1
            return entry

        async def flush(self):
            return None

    class LLM:
        async def stream(self, request):
            yield ModelChunk(content="done", finish_reason="stop")

    async def scenario() -> None:
        from box_agent.persistence import RecoveryBundle

        memory = Memory()
        recovery_event = AgentEvent(
            event_id="memory-written",
            sequence=1,
            session_id="session-memory",
            run_id="run-memory",
            turn_id="turn-memory",
            type="memory.written",
            payload={
                "entry": {
                    "entry_id": "fact-1",
                    "text": "already stored",
                    "kind": "fact",
                    "metadata": {},
                },
                "entry_id": "fact-1",
            },
        )
        kernel = AgentLoopKernel(
            llm=LLM(),
            context_engine=InMemoryContextEngine(),
            memory_engine=memory,
            recovery=RecoveryBundle(checkpoint=None, events=(recovery_event,)),
        )
        result = await kernel.run(
            RunRequest(
                request_id="request-memory",
                session_id="session-memory",
                turn_id="turn-memory",
                user_input=Message.user("continue"),
                metadata={
                    "memory_writes": {
                        "entry_id": "fact-1",
                        "text": "already stored",
                    }
                },
            ),
            emit=lambda event: None,
            cancel_event=asyncio.Event(),
        )
        assert result.status == "completed"
        assert memory.writes == 0

    asyncio.run(scenario())


def test_kernel_emits_permission_resolved_after_gateway_approval() -> None:
    class Tool:
        name = "preflight"
        description = "preflight"
        parameters = {"type": "object", "properties": {}}

        async def preflight(self, arguments, *, context=None):
            del arguments, context
            return ToolResult(
                success=False,
                permission_request={"scope": "test", "reason": "approval"},
            )

        async def invoke(self, arguments, *, context=None):
            del arguments, context
            return ToolCallResult(call_id="call-1", status="succeeded", content="ok")

    class LLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id="call-1",
                            tool_name="preflight",
                            arguments={},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield ModelChunk(content="done", finish_reason="stop")

    async def scenario() -> None:
        host = PluginHost()
        host.registries["tools"].register("preflight", Tool(), source="test")
        host.registries["permissions"].register("default", _Approve(), source="test")
        from box_agent.tools.engine import RegistryToolEngine

        engine = RegistryToolEngine(
            host.registries["tools"], permission_gateway=_Approve()
        )
        emitted: list[AgentEvent] = []
        result = await AgentLoopKernel(
            llm=LLM(),
            context_engine=InMemoryContextEngine(),
            tool_engine=engine,
        ).run(
            RunRequest(
                request_id="request-2",
                session_id="session-2",
                turn_id="turn-2",
                user_input=Message.user("call tool"),
            ),
            emit=emitted.append,
            cancel_event=asyncio.Event(),
        )
        assert result.status == "completed"
        assert any(event.type == "permission.resolved" for event in emitted)

    asyncio.run(scenario())
