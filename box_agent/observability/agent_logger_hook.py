"""AgentLogger bridge implemented as a stable Kernel event plugin."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .cache_fingerprint import build_cache_fingerprint
from box_agent.llm.debug_logging import reset_llm_debug_sink, set_llm_debug_sink
from box_agent.schema import Message


@dataclass(frozen=True)
class _SchemaTool:
    schema: Mapping[str, Any]

    @property
    def name(self) -> str:
        function = self.schema.get("function")
        if isinstance(function, Mapping):
            return str(function.get("name", ""))
        return str(self.schema.get("name", ""))

    @property
    def server_name(self) -> str:
        return str(self.schema.get("server_name", "") or "")

    def to_schema(self) -> dict[str, Any]:
        return dict(self.schema)


class AgentLoggerHook:
    """Project stable events into the established per-run diagnostic log."""

    _TERMINAL = frozenset({"run.completed", "run.failed", "run.cancelled"})

    def __init__(
        self,
        logger: Any,
        *,
        cache_fingerprint_context: Mapping[str, Any] | None = None,
        cache_fingerprint_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._logger = logger
        self._fingerprint_context = dict(cache_fingerprint_context or {})
        self._fingerprint_sink = cache_fingerprint_sink
        self._debug_token: Any | None = None

    async def on_event(self, event: Any) -> None:
        event_type = str(getattr(event, "type", "") or "")
        payload = getattr(event, "payload", {})
        payload = payload if isinstance(payload, Mapping) else {}
        if event_type == "run.started":
            self._logger.start_new_run()
            self._debug_token = set_llm_debug_sink(
                self._logger.log_llm_debug_record
            )
            return
        if event_type == "model.requested":
            request = payload.get("request")
            if not isinstance(request, Mapping):
                return
            messages = [
                Message(
                    role=str(item.get("role", "user")),
                    content=item.get("content", ""),
                    name=item.get("name"),
                )
                for item in request.get("messages", ())
                if isinstance(item, Mapping)
            ]
            tools = [
                _SchemaTool(item)
                for item in request.get("tools", ())
                if isinstance(item, Mapping)
            ]
            fingerprint = build_cache_fingerprint(
                messages=messages,
                tools=tools,
                context=self._fingerprint_context,
            )
            if self._fingerprint_sink is not None:
                try:
                    self._fingerprint_sink(fingerprint)
                except Exception:
                    pass
            self._logger.log_request(messages, tools, fingerprint)
            return
        if event_type == "model.response.completed":
            self._logger.log_response(
                content=str(payload.get("content", "") or ""),
                thinking=str(payload.get("thinking", "") or "") or None,
                finish_reason=str(payload.get("finish_reason", "stop") or "stop"),
                usage=payload.get("usage"),
                provider_request_id=payload.get("provider_request_id"),
            )
            return
        if event_type == "tool.call.completed":
            error = payload.get("error")
            self._logger.log_tool_result(
                tool_name=str(payload.get("tool_name", "") or ""),
                arguments=dict(payload.get("arguments", {}) or {}),
                result_success=payload.get("status") == "succeeded",
                result_content=str(payload.get("content", "") or "") or None,
                result_error=(
                    str(error.get("message", "") or "")
                    if isinstance(error, Mapping)
                    else str(error or "")
                )
                or None,
                raw_output=(
                    dict(payload["output"])
                    if isinstance(payload.get("output"), Mapping)
                    else None
                ),
            )
            return
        if event_type in self._TERMINAL and self._debug_token is not None:
            reset_llm_debug_sink(self._debug_token)
            self._debug_token = None


__all__ = ["AgentLoggerHook"]
