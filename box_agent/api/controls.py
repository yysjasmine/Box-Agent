"""Control commands accepted by an Agent Run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ErrorInfo


@dataclass(frozen=True, slots=True)
class ControlCommand:
    """An idempotent command applied by the Loop at a checkpoint."""

    command_id: str
    session_id: str
    run_id: str
    kind: str
    payload: Mapping[str, Any]
    source: str
    issued_at: str | None = None
    protocol_version: str = "1"

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.command_id, "command_id"),
            (self.session_id, "session_id"),
            (self.run_id, "run_id"),
            (self.kind, "kind"),
            (self.source, "source"),
            (self.protocol_version, "protocol_version"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        object.__setattr__(self, "payload", dict(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "command_id": self.command_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "payload": dict(self.payload),
            "source": self.source,
            "issued_at": self.issued_at,
        }

    def canonical(self) -> "ControlCommand":
        """Normalize compatibility spellings at the stable API boundary.

        Protocol v1 used ``message`` for injection text while ACP historically
        used camelCase identifiers. The Loop and all adapters consume one
        canonical payload, so compatibility translation cannot diverge across
        implementations.
        """

        payload = dict(self.payload)
        if self.kind == "run.inject":
            text = payload.get("text", payload.get("message"))
            injection_id = payload.get(
                "injection_id",
                payload.get("injectionId", self.command_id),
            )
            payload = {"injection_id": injection_id, "text": text}
        elif self.kind == "run.cancel_inject":
            injection_id = payload.get("injection_id", payload.get("injectionId"))
            payload = {"injection_id": injection_id}
        if payload == dict(self.payload):
            return self
        return ControlCommand(
            command_id=self.command_id,
            session_id=self.session_id,
            run_id=self.run_id,
            kind=self.kind,
            payload=payload,
            source=self.source,
            issued_at=self.issued_at,
            protocol_version=self.protocol_version,
        )


@dataclass(frozen=True, slots=True)
class CommandAck:
    """Structured acknowledgement for a submitted command."""

    command_id: str
    accepted: bool
    status: str
    error: ErrorInfo | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "accepted": self.accepted,
            "status": self.status,
            "error": self.error.to_dict() if self.error else None,
        }
