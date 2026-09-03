"""Stable event envelope emitted by the Agent Loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """A serializable, ordered fact consumed by ACP, CLI, and SDK clients."""

    event_id: str
    sequence: int
    session_id: str
    run_id: str
    type: str
    payload: Mapping[str, Any]
    turn_id: str | None = None
    audience: tuple[str, ...] = ()
    occurred_at: str | None = None
    correlation_id: str | None = None
    protocol_version: str = "1"

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.event_id, "event_id"),
            (self.session_id, "session_id"),
            (self.run_id, "run_id"),
            (self.type, "type"),
            (self.protocol_version, "protocol_version"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "audience", tuple(self.audience))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "type": self.type,
            "audience": list(self.audience),
            "occurred_at": self.occurred_at,
            "correlation_id": self.correlation_id,
            "payload": dict(self.payload),
        }
