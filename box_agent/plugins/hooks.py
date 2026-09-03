"""Adapters that make configured hooks first-class Kernel plugins."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

from box_agent.compat.events import StopReason
from box_agent.schema import LLMResponse, Message


class LifecycleHookAdapter:
    """Map stable events to the established lifecycle callback surface.

    New hooks may implement ``on_event`` directly. Existing configured hooks
    keep their semantic callbacks, and Tool interceptors are forwarded to the
    registry-backed Tool Engine before/after execution.
    """

    def __init__(self, hook: Any) -> None:
        self._hook = hook

    async def on_event(self, event: Any) -> None:
        direct = getattr(self._hook, "on_event", None)
        if callable(direct):
            await _maybe_await(direct(event))

        event_type = str(getattr(event, "type", "") or "")
        payload = getattr(event, "payload", {})
        if not isinstance(payload, Mapping):
            payload = {}

        if event_type == "run.started":
            callback = getattr(self._hook, "on_agent_start", None)
            raw_input = payload.get("user_input", {})
            if callable(callback) and isinstance(raw_input, Mapping):
                await _maybe_await(
                    callback(
                        messages=[
                            Message(
                                role=str(raw_input.get("role", "user")),
                                content=raw_input.get("content", ""),
                            )
                        ],
                        tools={
                            str(name): schema
                            for name, schema in (
                                payload.get("tools", {}) or {}
                            ).items()
                        },
                        max_steps=int(payload.get("max_steps", 0) or 0),
                    )
                )
            return

        if event_type == "step.started":
            callback = getattr(self._hook, "on_step_start", None)
            if callable(callback):
                await _maybe_await(
                    callback(
                        step=int(payload.get("step", 0) or 0),
                        max_steps=int(payload.get("max_steps", 0) or 0),
                    )
                )
            return

        if event_type == "model.response.completed":
            callback = getattr(self._hook, "on_llm_response", None)
            if callable(callback):
                await _maybe_await(
                    callback(
                        response=LLMResponse(
                            content=str(payload.get("content", "") or ""),
                            thinking=str(payload.get("thinking", "") or "") or None,
                            finish_reason=str(
                                payload.get("finish_reason", "stop") or "stop"
                            ),
                        )
                    )
                )
            return

        if event_type == "step.completed":
            callback = getattr(self._hook, "on_step_end", None)
            if callable(callback):
                await _maybe_await(
                    callback(
                        step=int(payload.get("step", 0) or 0),
                        elapsed_seconds=float(
                            payload.get("elapsed_seconds", 0.0) or 0.0
                        ),
                        total_elapsed_seconds=float(
                            payload.get("total_elapsed_seconds", 0.0) or 0.0
                        ),
                    )
                )
            return

        if event_type == "run.failed":
            error = payload.get("error")
            message = (
                str(error.get("message", "") or "")
                if isinstance(error, Mapping)
                else str(error or "")
            )
            callback = getattr(self._hook, "on_error", None)
            if callable(callback):
                await _maybe_await(
                    callback(message=message, is_fatal=True, exception=None)
                )
            await self._done(
                str(payload.get("stop_reason", "error") or "error"),
                message,
            )
            return

        if event_type in {"run.completed", "run.cancelled"}:
            await self._done(
                str(payload.get("stop_reason", "stop") or "stop"),
                str(payload.get("final_content", "") or ""),
            )

    async def on_tool_start(self, **kwargs: Any) -> Any:
        callback = getattr(self._hook, "on_tool_start", None)
        return await _maybe_await(callback(**kwargs)) if callable(callback) else None

    async def on_tool_result(self, **kwargs: Any) -> Any:
        callback = getattr(self._hook, "on_tool_result", None)
        return await _maybe_await(callback(**kwargs)) if callable(callback) else None

    async def _done(self, stop_reason: str, final_content: str) -> None:
        callback = getattr(self._hook, "on_done", None)
        if callable(callback):
            await _maybe_await(
                callback(
                    stop_reason=_legacy_stop_reason(stop_reason),
                    final_content=final_content,
                )
            )


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _legacy_stop_reason(value: str) -> StopReason:
    normalized = str(value or "").lower()
    return {
        "stop": StopReason.END_TURN,
        "completed": StopReason.END_TURN,
        "end_turn": StopReason.END_TURN,
        "max_steps": StopReason.MAX_STEPS,
        "max_tokens": StopReason.MAX_TOKENS,
        "cancelled": StopReason.CANCELLED,
        "checkpoint_paused": StopReason.CHECKPOINT_PAUSED,
    }.get(normalized, StopReason.ERROR)


__all__ = ["LifecycleHookAdapter"]
