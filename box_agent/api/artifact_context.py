"""Artifact publication contract shared by Kernel and persistence plugins."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ArtifactPublishRequest:
    """One Tool artifact awaiting durable enrichment and publication."""

    session_id: str
    run_id: str
    turn_id: str
    call_id: str
    artifact: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, name in (
            (self.session_id, "session_id"),
            (self.run_id, "run_id"),
            (self.turn_id, "turn_id"),
            (self.call_id, "call_id"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.artifact, Mapping):
            raise ValueError("artifact must be a mapping")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        object.__setattr__(self, "artifact", dict(self.artifact))
        object.__setattr__(self, "metadata", dict(self.metadata))


class ArtifactProcessor(Protocol):
    """Own durable Run/output publication boundaries.

    ``begin_run`` and ``end_run`` are optional at runtime for processors that
    only enrich individual artifacts.  A durable registry implementation uses
    all three methods so a successful terminal event cannot precede registry
    persistence.
    """

    def begin_run(self, request: Any) -> Any:
        ...

    def process(self, request: ArtifactPublishRequest) -> Mapping[str, Any]:
        ...

    def end_run(
        self,
        request: Any,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> Any:
        ...


__all__ = ["ArtifactProcessor", "ArtifactPublishRequest"]
