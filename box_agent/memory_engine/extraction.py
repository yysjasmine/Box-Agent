"""Kernel-event adapter for conversation memory extraction plugins."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from box_agent.schema import Message


class ConversationMemoryExtractionHook:
    """Drive one extractor per Session from the stable AgentEvent stream.

    The adapter owns no extraction policy.  It translates durable lifecycle
    facts into the existing ``maybe_extract(messages, trigger)`` SPI.  Using
    the exact provider-neutral request snapshot when available also restores
    prior Session turns without coupling the extractor to CLI or ACP state.
    """

    _TERMINAL_EVENTS = frozenset(
        {"run.completed", "run.failed", "run.cancelled"}
    )

    def __init__(self, extractor_factory: Callable[[str], Any]) -> None:
        self._extractor_factory = extractor_factory
        self._extractors: dict[str, Any] = {}
        self._messages: dict[tuple[str, str], list[Message]] = {}

    async def on_event(self, event: Any) -> None:
        session_id = str(getattr(event, "session_id", "") or "")
        run_id = str(getattr(event, "run_id", "") or "")
        turn_id = str(getattr(event, "turn_id", "") or "")
        event_type = str(getattr(event, "type", "") or "")
        payload = getattr(event, "payload", {})
        if not session_id or not run_id or not isinstance(payload, Mapping):
            return

        key = (session_id, run_id)
        messages = self._messages.setdefault(key, [])
        if event_type == "run.started":
            user_input = payload.get("user_input")
            if isinstance(user_input, Mapping):
                message = _message_from_mapping(user_input)
                if message is not None:
                    messages[:] = [message]
            self._extractor(session_id)
            return

        if event_type == "model.requested":
            request = payload.get("request")
            raw_messages = request.get("messages") if isinstance(request, Mapping) else None
            if isinstance(raw_messages, (list, tuple)):
                exact = [
                    message
                    for raw in raw_messages
                    if isinstance(raw, Mapping)
                    and (message := _message_from_mapping(raw)) is not None
                ]
                if exact:
                    messages[:] = exact
            return

        if event_type == "model.response.completed":
            content = _message_content(payload.get("content", ""))
            tool_call_ids = payload.get("tool_call_ids", ())
            if content or tool_call_ids:
                messages.append(
                    Message(
                        role="assistant",
                        content=content or "[assistant requested tool execution]",
                    )
                )
            return

        if event_type == "tool.call.completed":
            content = _message_content(payload.get("content", ""))
            error = payload.get("error")
            if not content and isinstance(error, Mapping):
                content = str(error.get("message", "") or "")
            messages.append(
                Message(
                    role="tool",
                    content=content,
                    tool_call_id=str(payload.get("call_id", "") or "") or None,
                    name=str(payload.get("tool_name", "") or "") or None,
                )
            )
            return

        trigger = None
        if event_type == "context.compacted":
            trigger = "pre_summarize"
        elif event_type == "step.completed":
            trigger = "step_interval"
        elif event_type in self._TERMINAL_EVENTS:
            trigger = "loop_end"
        if trigger is None:
            return

        extractor = self._extractor(session_id)
        await extractor.maybe_extract(
            list(messages),
            trigger,
            turn_id=turn_id,
        )
        if event_type in self._TERMINAL_EVENTS:
            self._messages.pop(key, None)

    def close_session(self, session_id: str) -> None:
        self._extractors.pop(session_id, None)
        for key in tuple(self._messages):
            if key[0] == session_id:
                self._messages.pop(key, None)

    def _extractor(self, session_id: str) -> Any:
        extractor = self._extractors.get(session_id)
        if extractor is None:
            extractor = self._extractor_factory(session_id)
            if not callable(getattr(extractor, "maybe_extract", None)):
                raise TypeError("memory extractor plugin must define maybe_extract")
            self._extractors[session_id] = extractor
        return extractor


def _message_from_mapping(value: Mapping[str, Any]) -> Message | None:
    role = value.get("role")
    if not isinstance(role, str) or not role.strip():
        return None
    return Message(
        role=role,
        content=_message_content(value.get("content", "")),
        thinking=(
            str(value["thinking"])
            if value.get("thinking") is not None
            else None
        ),
        tool_call_id=(
            str(value["tool_call_id"])
            if value.get("tool_call_id") is not None
            else None
        ),
        name=str(value["name"]) if value.get("name") is not None else None,
    )


def _message_content(value: Any) -> str | list[dict[str, Any]]:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


__all__ = ["ConversationMemoryExtractionHook"]
