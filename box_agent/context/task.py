"""Host-neutral task identity carried across sessions, turns, and artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def normalize_task_id(value: object) -> str | None:
    """Return one safe host-owned task id or ``None`` for invalid input."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if _TASK_ID_RE.fullmatch(normalized) else None


@dataclass(frozen=True, slots=True)
class TaskContext:
    """Stable logical task identity plus the current host turn."""

    session_id: str
    task_id: str
    turn_id: str


__all__ = ["TaskContext", "normalize_task_id"]
