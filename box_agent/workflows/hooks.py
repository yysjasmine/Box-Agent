"""Hook adapters used while workflow behavior is migrated out of the loop."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any


class WorkflowEventHook:
    """Forward Kernel events to policies that opt into event-driven behavior.

    The hook deliberately does not interpret event payloads.  A Goal/Plan/PPT
    plugin may implement ``on_event(event)`` to maintain receipts, schedule a
    continuation, or persist a checkpoint; the Kernel remains responsible for
    ordering and terminal behavior.
    """

    def __init__(self, policies: Iterable[Any] = ()) -> None:
        self._policies = tuple(policy for policy in policies if policy is not None)

    async def on_event(self, event: Any) -> None:
        for policy in self._policies:
            callback = getattr(policy, "on_event", None)
            if not callable(callback):
                continue
            result = callback(event)
            if inspect.isawaitable(result):
                await result


__all__ = ["WorkflowEventHook"]
