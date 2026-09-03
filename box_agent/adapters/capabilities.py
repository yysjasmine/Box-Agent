"""Adapters from concrete Box-Agent capabilities to stable runtime ports."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Mapping
from typing import Any

from box_agent.api import LLMRequest, MemoryEntry, MemoryQuery, MemoryRecall, ModelChunk, Usage


class LLMClientPort:
    """Normalize the existing ``LLMClient.generate_stream`` API."""

    def __init__(
        self,
        client: Any,
        *,
        prefer_generate: bool = False,
        client_info: Any | None = None,
        auto_model_candidates: tuple[Mapping[str, Any], ...] = (),
    ) -> None:
        self._client = client
        self._prefer_generate = bool(prefer_generate)
        self._client_info = client_info
        self._auto_model_candidates = tuple(
            dict(candidate) for candidate in auto_model_candidates
        )

    @property
    def model(self) -> str:
        return str(getattr(self._client, "model", "") or "")

    @property
    def max_output_tokens(self) -> int:
        value = getattr(self._client, "max_output_tokens", 0)
        return (
            value
            if isinstance(value, int)
            and not isinstance(value, bool)
            and value > 0
            else 0
        )

    @property
    def auto_model_candidates(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(candidate) for candidate in self._auto_model_candidates)

    def for_model(
        self,
        model: str,
        *,
        max_output_tokens: int | None = None,
    ) -> "LLMClientPort":
        clone_for_model = getattr(self._client, "for_model", None)
        if not callable(clone_for_model):
            raise ValueError("configured LLM client does not support model binding")
        return LLMClientPort(
            clone_for_model(model, max_output_tokens=max_output_tokens),
            prefer_generate=self._prefer_generate,
            client_info=self._client_info,
        )

    def for_run(self, request: Any, bundle: Any | None = None) -> "LLMClientPort":
        """Create an isolated model/correlation binding for one Agent Run."""

        del bundle
        if request is None:
            return self
        from box_agent.client_info import ClientInfo
        from box_agent.llm.binding import normalize_llm_binding

        metadata = getattr(request, "metadata", {})
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        binding = normalize_llm_binding(metadata)
        client = self._client
        if binding is not None:
            clone_for_model = getattr(client, "for_model", None)
            if not callable(clone_for_model):
                raise ValueError(
                    "configured LLM client does not support session model binding"
                )
            client = clone_for_model(
                binding["model"],
                max_output_tokens=binding.get("maxTokens"),
            )
        client_info = ClientInfo.from_meta(
            metadata.get("client_info", metadata.get("clientInfo"))
        )
        candidates: tuple[Mapping[str, Any], ...] = ()
        if binding is not None:
            auto_routing = binding.get("autoRouting", {})
            if isinstance(auto_routing, Mapping):
                raw_candidates = auto_routing.get("models", ())
                if isinstance(raw_candidates, (list, tuple)):
                    candidates = tuple(
                        dict(candidate)
                        for candidate in raw_candidates
                        if isinstance(candidate, Mapping)
                    )
        return LLMClientPort(
            client,
            prefer_generate=self._prefer_generate,
            client_info=client_info,
            auto_model_candidates=candidates,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[ModelChunk]:
        from box_agent.schema import (
            FunctionCall as LegacyFunctionCall,
            Message as LegacyMessage,
            ToolCall as LegacyToolCall,
        )
        from box_agent.client_info import scoped_client_info

        messages = []
        for message in request.messages:
            metadata = dict(message.metadata)
            tool_calls = []
            for raw_call in metadata.get("tool_calls", ()) or ():
                if not isinstance(raw_call, Mapping):
                    continue
                tool_calls.append(
                    LegacyToolCall(
                        id=str(raw_call.get("call_id", raw_call.get("id", ""))),
                        type="function",
                        function=LegacyFunctionCall(
                            name=str(
                                raw_call.get("tool_name", raw_call.get("name", ""))
                            ),
                            arguments=dict(raw_call.get("arguments", {}) or {}),
                        ),
                    )
                )
            messages.append(
                LegacyMessage(
                    role=message.role,
                    content=(
                        list(message.content)
                        if isinstance(message.content, tuple)
                        else message.content
                    ),
                    thinking=str(metadata.get("thinking", "") or "") or None,
                    tool_calls=tool_calls or None,
                    tool_call_id=str(metadata.get("tool_call_id", "") or "")
                    or None,
                    name=message.name
                    or str(metadata.get("name", "") or "")
                    or None,
                )
            )
        kwargs = {
            "messages": messages,
            "tools": list(request.tools),
            "thinking_enabled": bool(
                request.metadata.get("thinking_enabled", False)
            ),
            "session_id": str(
                request.metadata.get("correlation_session_id", "")
                or request.session_id
            ),
            "turn_id": str(
                request.metadata.get("correlation_turn_id", "")
                or request.turn_id
            ),
            "title": str(request.metadata.get("title", "") or ""),
            "call_kind": str(request.metadata.get("call_kind", "") or ""),
        }
        generate_stream = getattr(self._client, "generate_stream", None)
        if callable(generate_stream) and not self._prefer_generate:
            with scoped_client_info(self._client_info):
                stream = generate_stream(**kwargs)
            while True:
                try:
                    with scoped_client_info(self._client_info):
                        event = await anext(stream)
                except StopAsyncIteration:
                    break
                event_type = getattr(event, "type", "")
                if event_type == "text":
                    yield ModelChunk(content=str(getattr(event, "delta", "") or ""))
                elif event_type == "thinking":
                    yield ModelChunk(thinking=str(getattr(event, "delta", "") or ""))
                elif event_type == "activity":
                    yield ModelChunk(activity=getattr(event, "activity", None))
                elif event_type == "finish":
                    yield _model_chunk_from_response(event)
            return

        generate = getattr(self._client, "generate", None)
        if not callable(generate):
            raise TypeError("LLM client must provide generate_stream(...) or generate(...)")
        with scoped_client_info(self._client_info):
            response = generate(**kwargs)
            if inspect.isawaitable(response):
                response = await response
        yield _model_chunk_from_response(response, include_content=True)


class MemoryManagerEngine:
    """Expose ``MemoryManager`` recall/write operations through MemoryEngine."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    async def recall(self, query: MemoryQuery) -> MemoryRecall:
        entries: list[MemoryEntry] = []
        search = getattr(self._manager, "search", None)
        if callable(search):
            matches = await _call_capability(
                search,
                query.text,
                limit=query.limit,
            )
            for index, value in enumerate(matches or ()):
                if isinstance(value, Mapping):
                    content = str(value.get("content", value.get("text", "")))
                    entry_id = str(value.get("id", f"legacy:{index}"))
                    metadata = dict(value)
                else:
                    content = str(value)
                    entry_id = f"legacy:{index}"
                    metadata = {}
                if content.strip():
                    entries.append(MemoryEntry(entry_id, content, metadata=metadata))
        return MemoryRecall(entries=tuple(entries[: query.limit]), query=query)

    async def write(self, entry: MemoryEntry) -> MemoryEntry:
        writer = getattr(self._manager, "append_context", None)
        if callable(writer):
            await _call_capability(writer, entry.text, topic=entry.kind)
        return entry

    async def flush(self) -> None:
        flush = getattr(self._manager, "flush", None)
        if callable(flush):
            await _call_capability(flush)


def _tool_call_from_legacy(call: Any):
    from box_agent.api import ToolCallRequest

    function = getattr(call, "function", None)
    name = getattr(function, "name", None) or getattr(call, "name", "")
    arguments = getattr(function, "arguments", None) or getattr(call, "arguments", {})
    return ToolCallRequest(
        call_id=str(getattr(call, "id", "") or getattr(call, "tool_call_id", "")),
        tool_name=str(name),
        arguments=arguments if isinstance(arguments, Mapping) else {},
    )


def _model_chunk_from_response(
    response: Any,
    *,
    include_content: bool = False,
) -> ModelChunk:
    return ModelChunk(
        content=(
            str(getattr(response, "content", "") or "")
            if include_content
            else ""
        ),
        thinking=(
            str(getattr(response, "thinking", "") or "")
            if include_content
            else ""
        ),
        tool_calls=tuple(
            _tool_call_from_legacy(call)
            for call in (getattr(response, "tool_calls", None) or ())
        ),
        finish_reason=getattr(response, "finish_reason", None),
        usage=_usage_from_legacy(getattr(response, "usage", None)),
        provider_response_id=getattr(response, "provider_response_id", None),
        provider_request_id=getattr(response, "provider_request_id", None),
        activity=getattr(response, "activity", None),
        truncated_tool_calls=tuple(
            getattr(response, "truncated_tool_calls", None) or ()
        ),
        raw_finish_reason=getattr(response, "raw_finish_reason", None),
        stream_dropped_mid_tool=bool(
            getattr(response, "stream_dropped_mid_tool", False)
        ),
        oversized_tool_calls=tuple(
            getattr(response, "oversized_tool_calls", None) or ()
        ),
    )


def _usage_from_legacy(value: Any) -> Usage | None:
    if value is None:
        return None
    input_tokens = int(
        getattr(value, "input_tokens", 0) or getattr(value, "prompt_tokens", 0) or 0
    )
    output_tokens = int(
        getattr(value, "output_tokens", 0)
        or getattr(value, "completion_tokens", 0)
        or 0
    )
    total_tokens = int(getattr(value, "total_tokens", 0) or input_tokens + output_tokens)
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


async def _call_capability(target: Any, *args: Any, **kwargs: Any) -> Any:
    """Invoke an async capability directly and a synchronous one off-loop."""

    if inspect.iscoroutinefunction(target):
        return await target(*args, **kwargs)
    value = await asyncio.to_thread(target, *args, **kwargs)
    return await value if inspect.isawaitable(value) else value


__all__ = ["LLMClientPort", "MemoryManagerEngine"]
