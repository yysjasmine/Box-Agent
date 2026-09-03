"""Persist stable AgentEvent facts as per-Session JSONL traces."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from .session_trace import SessionTraceWriter


class SessionTraceHook:
    """Translate Kernel events into the existing trace-file contract."""

    _TERMINAL = frozenset({"run.completed", "run.failed", "run.cancelled"})

    def __init__(
        self,
        writer_factory: Callable[[str, str], SessionTraceWriter] | None = None,
    ) -> None:
        self._writer_factory = writer_factory or (
            lambda session_id, runtime_session_id: SessionTraceWriter(
                session_id=session_id,
                acp_session_id=runtime_session_id,
            )
        )
        self._writers: dict[str, SessionTraceWriter] = {}
        self._started_sessions: set[str] = set()
        self._content: dict[tuple[str, str], list[str]] = {}

    async def on_event(self, event: Any) -> None:
        event_type = str(getattr(event, "type", "") or "")
        session_id = str(getattr(event, "session_id", "") or "")
        run_id = str(getattr(event, "run_id", "") or "")
        turn_id = str(getattr(event, "turn_id", "") or "")
        payload = getattr(event, "payload", {})
        if not session_id or not run_id or not isinstance(payload, Mapping):
            return
        key = (session_id, run_id)

        if event_type == "run.started":
            correlation = payload.get("correlation", {})
            correlation = correlation if isinstance(correlation, Mapping) else {}
            upstream_session = str(correlation.get("session_id", "") or session_id)
            turn_id = str(correlation.get("turn_id", "") or turn_id)
            writer = self._writers.get(session_id)
            if writer is None:
                writer = self._writer_factory(upstream_session, session_id)
                self._writers[session_id] = writer
            if session_id not in self._started_sessions:
                await self._write(writer, "session.start", turn_id=turn_id)
                self._started_sessions.add(session_id)
            await self._write(
                writer,
                "turn.input",
                turn_id=turn_id,
                data={"message": payload.get("user_input")},
            )
            self._content[key] = []
            return

        writer = self._writers.get(session_id)
        if writer is None:
            return
        if event_type == "model.content.delta":
            self._content.setdefault(key, []).append(str(payload.get("content", "") or ""))
            return
        if event_type == "model.requested":
            await self._write(
                writer,
                "llm.request",
                turn_id=turn_id,
                data=dict(payload),
            )
            return
        if event_type == "model.response.completed":
            await self._write(
                writer,
                "llm.response",
                turn_id=turn_id,
                data=dict(payload),
            )
            return
        if event_type == "tool.call.requested":
            await self._write(
                writer,
                "tool.request",
                turn_id=turn_id,
                tool_call_id=str(payload.get("call_id", "") or "") or None,
                data=dict(payload),
            )
            return
        if event_type == "tool.call.completed":
            trace_payload = {
                **dict(payload),
                "success": payload.get("status") == "succeeded",
            }
            if "raw_output" not in trace_payload:
                trace_payload["raw_output"] = payload.get("output")
            await self._write(
                writer,
                "tool.response",
                turn_id=turn_id,
                tool_call_id=str(payload.get("call_id", "") or "") or None,
                data=trace_payload,
            )
            return
        if event_type not in self._TERMINAL:
            return
        content = str(payload.get("final_content", "") or "") or "".join(
            self._content.pop(key, [])
        )
        await self._write(
            writer,
            "turn.output",
            turn_id=turn_id,
            data={"content": content},
        )
        await self._write(
            writer,
            "turn.end",
            turn_id=turn_id,
            data={
                "status": event_type.removeprefix("run."),
                "stop_reason": payload.get("stop_reason"),
            },
        )

    async def _write(
        self,
        writer: SessionTraceWriter,
        event: str,
        **kwargs: Any,
    ) -> None:
        await asyncio.to_thread(writer.write, event, **kwargs)


__all__ = ["SessionTraceHook"]
