"""Named ACP and CLI façades over :class:`ServiceAdapter`.

The classes intentionally add no policy. Their names make the migration seam
obvious to host code while keeping transport dependencies optional.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .service import ServiceAdapter


class ACPServiceAdapter(ServiceAdapter):
    """ACP-facing JSON adapter; transport framing remains owned by ACP."""

    async def handle_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        operation = str(payload.get("operation", ""))
        if operation == "session.open":
            return {"session": await self.open_session(payload)}
        if operation == "session.load":
            return {"session": await self.load_session(payload)}
        if operation == "run.start":
            return {"run_id": await self.start(payload)}
        if operation == "run.events":
            events = [
                event
                async for event in self.events(
                    str(payload["run_id"]),
                    after_sequence=int(payload.get("after_sequence", 0)),
                )
            ]
            return {"events": events}
        if operation == "run.wait":
            # Waiting is an observation operation.  Taking a worker lease here
            # would turn a passive ACP client into a competing executor after a
            # process restart.  Hosts that explicitly want takeover must call
            # ``AgentService.resume`` through their control plane.
            handle = await self._service.attach(str(payload["run_id"]))
            return {"result": (await handle.wait()).to_dict()}
        if operation == "run.status":
            status = await self._service.get_status(
                str(payload["session_id"]),
                str(payload["run_id"]) if payload.get("run_id") else None,
            )
            return {"status": status.to_dict()}
        if operation == "session.close":
            await self._service.close_session(str(payload["session_id"]))
            return {"closed": True}
        if operation == "run.control":
            return {"ack": await self.control(payload)}
        raise ValueError(f"unsupported adapter operation: {operation}")


class CLIServiceAdapter(ServiceAdapter):
    """CLI renderer that prints only serialized events and final result."""

    async def run_json(
        self,
        payload: Mapping[str, Any],
        write: Callable[[str], Awaitable[None] | None],
    ) -> dict[str, Any]:
        async def sink(event: dict[str, Any]) -> None:
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
            value = write(line)
            if hasattr(value, "__await__"):
                await value

        return await self.run_to_sink(payload, sink)


class SDKServiceAdapter(ServiceAdapter):
    """SDK-facing façade with the same neutral contracts and no rendering."""

    async def run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Collect a run into one serializable result for library callers."""

        return await self.run_to_sink(payload, lambda _event: None)


__all__ = ["ACPServiceAdapter", "CLIServiceAdapter", "SDKServiceAdapter"]
