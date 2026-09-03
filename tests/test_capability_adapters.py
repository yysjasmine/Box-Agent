"""Concrete Box-Agent capabilities implement the stable runtime ports."""

from __future__ import annotations

import pytest

from box_agent.adapters import (
    LLMClientPort,
    MemoryManagerEngine,
    build_kernel_service,
    build_plugin_host,
)
from box_agent.api import LLMRequest, MemoryQuery, Message, RunRequest, SessionOpenRequest
from box_agent.schema import StreamEvent, TokenUsage
from box_agent.schema import LLMResponse


class LegacyClient:
    async def generate_stream(self, messages, tools, **kwargs):
        assert messages[0].content == "hello"
        yield StreamEvent(type="text", delta="hi")
        yield StreamEvent(
            type="finish",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
        )


@pytest.mark.asyncio
async def test_llm_client_port_normalizes_stream_chunks() -> None:
    port = LLMClientPort(LegacyClient())
    chunks = [
        chunk
        async for chunk in port.stream(
            LLMRequest(messages=(Message.user("hello"),), session_id="s", turn_id="t")
        )
    ]

    assert chunks[0].content == "hi"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 5


@pytest.mark.asyncio
async def test_llm_client_port_supports_keyword_only_provider_stream() -> None:
    class KeywordOnlyClient:
        async def generate_stream(self, *, messages, **kwargs):
            assert messages
            assert kwargs["thinking_enabled"] is True
            yield StreamEvent(type="finish", finish_reason="stop")

    port = LLMClientPort(KeywordOnlyClient())
    chunks = [
        chunk
        async for chunk in port.stream(
            LLMRequest(
                messages=(Message.user("hello"),),
                metadata={"thinking_enabled": True},
            )
        )
    ]

    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_llm_client_port_restores_tool_protocol_fields_from_message_metadata() -> None:
    captured = []

    class Client:
        async def generate_stream(self, *, messages, **_kwargs):
            captured.extend(messages)
            yield StreamEvent(type="finish", finish_reason="stop")

    messages = (
        Message(
            role="assistant",
            content="",
            metadata={
                "thinking": "reasoning",
                "tool_calls": [
                    {
                        "call_id": "call-1",
                        "tool_name": "echo",
                        "arguments": {"text": "hello"},
                    }
                ],
            },
        ),
        Message(
            role="tool",
            content="echo:hello",
            metadata={"tool_call_id": "call-1", "name": "echo"},
        ),
    )

    _ = [chunk async for chunk in LLMClientPort(Client()).stream(LLMRequest(messages=messages))]

    assert captured[0].thinking == "reasoning"
    assert captured[0].tool_calls[0].id == "call-1"
    assert captured[0].tool_calls[0].function.name == "echo"
    assert captured[1].tool_call_id == "call-1"
    assert captured[1].name == "echo"


@pytest.mark.asyncio
async def test_llm_port_normalizes_generate_only_provider_and_request_metadata() -> None:
    captured = {}

    class GenerateOnlyClient:
        async def generate(self, *, messages, tools, **kwargs):
            captured.update(kwargs)
            return LLMResponse(content="generated", finish_reason="stop")

    chunks = [
        chunk
        async for chunk in LLMClientPort(GenerateOnlyClient()).stream(
            LLMRequest(
                messages=(Message.user("hello"),),
                session_id="session-1",
                turn_id="turn-1",
                metadata={
                    "title": "child",
                    "call_kind": "subagent_step",
                },
            )
        )
    ]

    assert chunks[-1].content == "generated"
    assert chunks[-1].finish_reason == "stop"
    assert captured["session_id"] == "session-1"
    assert captured["turn_id"] == "turn-1"
    assert captured["title"] == "child"
    assert captured["call_kind"] == "subagent_step"


@pytest.mark.asyncio
async def test_llm_port_prefers_canonical_host_correlation_identity() -> None:
    captured = {}

    class Client:
        async def generate(self, *, messages, tools, **kwargs):
            captured.update(kwargs)
            return LLMResponse(content="ok", finish_reason="stop")

    async for _ in LLMClientPort(Client()).stream(
        LLMRequest(
            messages=(Message.user("hello"),),
            session_id="kernel-session",
            turn_id="kernel-turn",
            metadata={
                "correlation_session_id": "office-session",
                "correlation_turn_id": "office-turn",
                "title": "Quarterly review",
            },
        )
    ):
        pass

    assert captured["session_id"] == "office-session"
    assert captured["turn_id"] == "office-turn"
    assert captured["title"] == "Quarterly review"


@pytest.mark.asyncio
async def test_llm_port_binds_model_and_correlation_per_run_without_mutating_base() -> None:
    calls = []

    class Client:
        def __init__(self, model="base", max_output_tokens=1000):
            self.model = model
            self.max_output_tokens = max_output_tokens

        def for_model(self, model, *, max_output_tokens=None):
            return Client(model, max_output_tokens or self.max_output_tokens)

        async def generate(self, *, messages, tools, **kwargs):
            calls.append((self.model, self.max_output_tokens, dict(kwargs)))
            return LLMResponse(content="bound", finish_reason="stop")

    base = Client()
    port = LLMClientPort(base, prefer_generate=True)
    request = RunRequest(
        request_id="request-1",
        session_id="kernel-session",
        turn_id="kernel-turn",
        user_input=Message.user("hello"),
        metadata={
            "correlation_session_id": "office-session",
            "correlation_turn_id": "office-turn",
            "title": "Bound title",
            "llm_binding": {
                "source": "builtin",
                "model": "session-model",
                "maxTokens": 400,
            },
        },
    )

    bound = port.for_run(request)
    async for _ in bound.stream(
        LLMRequest(
            messages=(Message.user("hello"),),
            session_id=request.session_id,
            turn_id=request.turn_id,
            metadata=request.metadata,
        )
    ):
        pass

    assert base.model == "base"
    assert calls == [
        (
            "session-model",
            400,
            {
                "thinking_enabled": False,
                "session_id": "office-session",
                "turn_id": "office-turn",
                "title": "Bound title",
                "call_kind": "",
            },
        )
    ]


@pytest.mark.asyncio
async def test_memory_manager_engine_delegates_search() -> None:
    class Manager:
        def search(self, text, *, limit):
            assert text == "python"
            return ["remember python"][:limit]

    recall = await MemoryManagerEngine(Manager()).recall(MemoryQuery("python"))

    assert [entry.text for entry in recall.entries] == ["remember python"]


def test_plugin_host_registers_current_capabilities_for_kernel() -> None:
    host = build_plugin_host(llm=LegacyClient(), tools=[])

    assert host.registries["llm"].resolve("default", scope="run") is not None
    assert host.registries["context"].resolve("default", scope="run") is not None
    assert host.registries["llm.providers"].resolve("default", scope="run") is not None
    assert host.registries["context.providers"].resolve("default", scope="run") is not None
    assert len(host.registries["llm.providers"].registrations()) == 1
    assert len(host.registries["context.providers"].registrations()) == 1


def test_plugin_host_deduplicates_tool_alias_index_and_keeps_aliases() -> None:
    class Tool:
        name = "read_file"
        aliases = ("read",)

    tool = Tool()
    host = build_plugin_host(
        llm=LegacyClient(),
        # Agent.tools is commonly an alias index with repeated values.
        tools=(tool, tool),
    )

    registry = host.registries["tools"]
    assert registry.resolve("read_file", scope="run") is tool
    assert registry.resolve("read", scope="run") is tool
    assert registry.resolve("read-file", scope="run") is tool
    assert host.registries["tools.executors"].resolve("read_file", scope="run") is tool


def test_plugin_host_rejects_distinct_tools_with_conflicting_names() -> None:
    from box_agent.plugins import PluginConflictError

    class Tool:
        name = "read_file"
        aliases = ()

    with pytest.raises(PluginConflictError):
        build_plugin_host(
            llm=LegacyClient(),
            tools=(Tool(), Tool()),
        )


@pytest.mark.asyncio
async def test_current_llm_can_run_through_new_kernel_service() -> None:
    service = build_kernel_service(llm=LegacyClient())
    await service.open_session(SessionOpenRequest(session_id="session-1"))

    handle = await service.start(
        RunRequest(
            request_id="request-1",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("hello"),
        )
    )

    assert (await handle.wait()).final_message == "hi"


@pytest.mark.asyncio
async def test_kernel_service_composer_selects_run_bound_model_without_cross_session_mutation() -> None:
    calls = []

    class Client:
        def __init__(self, model="base"):
            self.model = model
            self.max_output_tokens = 1000

        def for_model(self, model, *, max_output_tokens=None):
            child = Client(model)
            child.max_output_tokens = max_output_tokens or self.max_output_tokens
            return child

        async def generate_stream(self, messages, tools=None, **kwargs):
            del messages, tools
            calls.append((self.model, self.max_output_tokens, kwargs["session_id"]))
            yield StreamEvent(type="text", delta=self.model)
            yield StreamEvent(type="finish", finish_reason="stop")

    base = Client()
    service = build_kernel_service(llm=base)
    await service.open_session(SessionOpenRequest(session_id="session-a"))
    await service.open_session(SessionOpenRequest(session_id="session-b"))
    first = await service.start(
        RunRequest(
            request_id="request-a",
            session_id="session-a",
            turn_id="turn-a",
            user_input=Message.user("hello"),
            metadata={
                "correlation_session_id": "office-a",
                "llm_binding": {
                    "source": "builtin",
                    "model": "model-a",
                    "maxTokens": 400,
                },
            },
        )
    )
    second = await service.start(
        RunRequest(
            request_id="request-b",
            session_id="session-b",
            turn_id="turn-b",
            user_input=Message.user("hello"),
            metadata={
                "correlation_session_id": "office-b",
                "llm_binding": {
                    "source": "builtin",
                    "model": "model-b",
                    "maxTokens": 500,
                },
            },
        )
    )

    assert (await first.wait()).final_message == "model-a"
    assert (await second.wait()).final_message == "model-b"
    assert base.model == "base"
    assert sorted(calls) == [
        ("model-a", 400, "office-a"),
        ("model-b", 500, "office-b"),
    ]
