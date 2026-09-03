"""Mutable Goal state bridge for the mature ``Agent.goal`` attribute."""

from __future__ import annotations

from ..workflows.goal import GoalState, GoalStore


class LegacyGoalStore(GoalStore):
    """Canonical GoalStore with the legacy facade's mutable identity semantics."""

    def compatibility_state(self, session_id: str = "") -> GoalState | None:
        return self._goals.get(str(session_id or ""))

    def set_compatibility_state(
        self,
        session_id: str,
        goal: GoalState | None,
    ) -> None:
        key = str(session_id or "")
        if goal is None:
            self._goals.pop(key, None)
        else:
            self._goals[key] = goal


__all__ = ["LegacyGoalStore"]
