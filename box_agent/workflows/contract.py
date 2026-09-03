"""Stable contract between the Agent Loop Kernel and workflow plugins."""

from __future__ import annotations

from typing import Final

from ..api.ports import WorkflowPolicy
from ..api.workflows import WorkflowAction, WorkflowCheckpointUpdate


NATURAL_END_STOP_REASONS: Final[frozenset[str]] = frozenset({"end_turn", "stop"})


def is_natural_end_reason(stop_reason: str | None) -> bool:
    """Treat OpenAI ``stop`` and ACP ``end_turn`` as one turn boundary."""

    return isinstance(stop_reason, str) and stop_reason in NATURAL_END_STOP_REASONS


__all__ = [
    "NATURAL_END_STOP_REASONS",
    "WorkflowAction",
    "WorkflowCheckpointUpdate",
    "WorkflowPolicy",
    "is_natural_end_reason",
]
