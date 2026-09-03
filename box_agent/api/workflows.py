"""Serializable values exchanged with workflow policy plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class WorkflowCheckpointUpdate:
    """A workflow checkpoint plus evidence recovered from persisted state."""

    text: str
    changed: bool
    recovered_evidence_urls: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class WorkflowAction:
    """A deterministic tool action requested by a trusted workflow policy."""

    action_id: str
    capability: str
    tool_name: str
    arguments: dict[str, Any]


__all__ = ["WorkflowAction", "WorkflowCheckpointUpdate"]
