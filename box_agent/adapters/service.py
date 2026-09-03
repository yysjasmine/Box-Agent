"""Thin host adapters over the stable ``AgentService`` protocol.

These adapters intentionally contain no model, tool, memory, or workflow
policy.  They translate host-shaped JSON/callbacks into the same service calls
used by every integration, making ACP/CLI/SDK rendering replaceable without
forking the Agent Loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

from box_agent.api import (
    AgentService,
    AttachmentRef,
    ControlCommand,
    Message,
    RunOptions,
    RunRequest,
    SessionOpenRequest,
)
from box_agent.adapters.acp_metadata import sanitize_metadata


def request_from_payload(payload: Mapping[str, Any]) -> RunRequest:
    """Decode a host payload at the adapter boundary."""

    user = payload.get("user_input") or payload.get("message") or {}
    if isinstance(user, str):
        user = {"content": user}
    if not isinstance(user, Mapping):
        raise ValueError("user_input/message must be a string or mapping")
    raw_content = user.get("content", "")
    content = (
        tuple(dict(block) for block in raw_content)
        if isinstance(raw_content, list)
        and all(isinstance(block, Mapping) for block in raw_content)
        else str(raw_content)
    )
    options = payload.get("options")
    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise ValueError("options must be a mapping")
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be a mapping")
    raw_attachments = payload.get("attachments", ())
    if not isinstance(raw_attachments, (list, tuple)) or not all(
        isinstance(item, Mapping) for item in raw_attachments
    ):
        raise ValueError("attachments must be a list of mappings")
    return RunRequest(
        request_id=str(payload.get("request_id") or uuid4().hex),
        session_id=str(payload["session_id"]),
        turn_id=str(payload.get("turn_id") or uuid4().hex),
        user_input=Message(
            role=str(user.get("role", "user")),
            content=content,
            name=user.get("name"),
            metadata=user.get("metadata", {}),
        ),
        attachments=tuple(
            AttachmentRef(
                attachment_id=str(item.get("attachment_id", "")),
                kind=str(item.get("kind", "")),
                uri=str(item.get("uri", "")),
                mime_type=str(
                    item.get("mime_type", "application/octet-stream")
                ),
                name=str(item.get("name", "")),
                metadata=item.get("metadata", {}),
            )
            for item in raw_attachments
        ),
        options=RunOptions(
            **{
                key: options[key]
                for key in {
                    "max_steps",
                    "deadline_ms",
                    "provider_stale_seconds",
                    "truncation_continuation_enabled",
                    "max_truncation_continuations",
                    "max_truncated_tool_call_retries",
                    "max_tool_calls",
                    "max_parallel_tools",
                    "thinking_enabled",
                    "workflow_id",
                    "workflow_options",
                    "context_budget",
                    "component_keys",
                }
                if key in options
            }
        ),
        metadata=sanitize_metadata(metadata),
    )


class ServiceAdapter:
    """Protocol-neutral adapter used by ACP, CLI, and SDK frontends."""

    def __init__(self, service: AgentService) -> None:
        self._service = service

    async def open_session(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        session = await self._service.open_session(
            SessionOpenRequest(
                session_id=payload.get("session_id"),
                metadata=sanitize_metadata(payload.get("metadata", {})),
            )
        )
        return session.to_dict()

    async def load_session(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Load an existing durable session without creating a new one."""

        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id is required for session.load")
        loader = getattr(self._service, "load_session", None)
        if not callable(loader):
            raise ValueError("AgentService does not support durable session loading")
        session = await loader(
            SessionOpenRequest(
                session_id=session_id,
                metadata=sanitize_metadata(payload.get("metadata", {})),
            )
        )
        return session.to_dict()

    async def start(self, payload: Mapping[str, Any]) -> str:
        handle = await self._service.start(request_from_payload(payload))
        return handle.run_id

    async def events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[dict[str, Any]]:
        # Event consumers observe a run; only explicit control or resume
        # operations may take a worker lease.
        handle = await self._service.attach(run_id)
        async for event in handle.events(after_sequence=after_sequence):
            yield event.to_dict()

    async def control(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        handle = await self._service.resume(str(payload["run_id"]))
        command = ControlCommand(
            command_id=str(payload.get("command_id") or uuid4().hex),
            session_id=str(payload["session_id"]),
            run_id=str(payload["run_id"]),
            kind=str(payload["kind"]),
            payload=payload.get("payload", {}),
            source=str(payload.get("source", "adapter")),
            issued_at=payload.get("issued_at"),
        )
        return (await handle.send(command)).to_dict()

    async def run_to_sink(
        self,
        payload: Mapping[str, Any],
        sink: Callable[[dict[str, Any]], Awaitable[None] | None],
    ) -> dict[str, Any]:
        """Run once and forward normalized events to a host renderer."""

        # ``AgentService.start`` intentionally rejects unknown sessions.  A
        # one-shot SDK/CLI caller should not have to duplicate the lifecycle
        # handshake, while an ACP caller that already opened the session gets
        # an idempotent lookup here.
        normalized_payload = dict(payload)
        session_id = normalized_payload.get("session_id") or uuid4().hex
        normalized_payload["session_id"] = str(session_id)
        session_metadata = normalized_payload.get("session_metadata", {})
        if not isinstance(session_metadata, Mapping):
            raise ValueError("session_metadata must be a mapping")
        await self._service.open_session(
            SessionOpenRequest(
                session_id=str(session_id),
                metadata=session_metadata,
            )
        )
        handle = await self._service.start(request_from_payload(normalized_payload))
        async for event in handle.events():
            result = sink(event.to_dict())
            if hasattr(result, "__await__"):
                await result
        return (await handle.wait()).to_dict()


__all__ = ["ServiceAdapter", "request_from_payload"]
