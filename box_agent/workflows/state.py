"""Data-only workflow state recovery shared by workflow plugins."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def workflow_state_from_recovery(
    bundle: Any | None,
    kind: str,
) -> Mapping[str, Any] | None:
    """Return the newest persisted state mapping for one workflow kind."""

    if bundle is None:
        return None
    events = (
        bundle.get("events", ())
        if isinstance(bundle, Mapping)
        else getattr(bundle, "events", ())
    )
    for event in reversed(tuple(events or ())):
        payload = (
            event.get("payload")
            if isinstance(event, Mapping)
            else getattr(event, "payload", None)
        )
        if not isinstance(payload, Mapping):
            continue
        state = payload.get("workflow_state")
        if not isinstance(state, Mapping):
            continue
        value = state.get(kind)
        if isinstance(value, Mapping):
            return value
    return None


__all__ = ["workflow_state_from_recovery"]
