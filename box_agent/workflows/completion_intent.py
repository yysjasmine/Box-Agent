"""Host-neutral intent predicates shared by completion workflow plugins."""

from __future__ import annotations

from typing import Final


PENDING_GATE_CANCEL_PHRASES: Final[tuple[str, ...]] = (
    "取消",
    "不用继续",
    "不要继续",
    "不做这个",
    "先不做",
    "换个任务",
    "新任务",
    "stop",
    "cancel",
    "never mind",
)


def cancels_pending_completion_gate(user_text: str) -> bool:
    """Return whether the user explicitly abandons a retained deliverable."""

    normalized = " ".join(user_text.casefold().split())
    return bool(normalized) and any(
        phrase in normalized for phrase in PENDING_GATE_CANCEL_PHRASES
    )


__all__ = ["PENDING_GATE_CANCEL_PHRASES", "cancels_pending_completion_gate"]
