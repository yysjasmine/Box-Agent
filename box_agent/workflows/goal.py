"""Session-scoped Goal workflow plugins.

This is the first extraction of Goal state from ``Agent``.  It intentionally
keeps the legacy tool names and payload keys so a host can opt into the plugin
without changing its ACP/UI protocol.  Autopilot continuation is exposed as
an explicit native SPI, while automatic host promotion remains gated by
parity fixtures.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..tools.base import Tool, ToolResult
from ..tools.runtime_context import current_runtime_invocation
from ..context import ContextItem
from ..api import Message, WorkflowContinuation
from ..api.controls import ControlCommand
from .contract import is_natural_end_reason


def _now() -> str:
    return datetime.now().isoformat()


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _items(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _extend(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def goal_items(value: object) -> list[str]:
    """Normalize a legacy Goal list field into detached non-empty strings."""

    return _items(value)


def extend_goal_items(target: list[str], values: object) -> None:
    """Append normalized Goal values while preserving insertion order."""

    _extend(target, _items(values))


@dataclass
class GoalState:
    """Canonical, serializable state for one session's durable goal."""

    objective: str
    status: str
    created_at: str
    updated_at: str
    evidence: list[str] = field(default_factory=list)
    progress: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    completed_by: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "status": self.status,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "evidence": list(self.evidence),
            "progress": list(self.progress),
            "blockedReason": self.blocked_reason,
            "completedBy": self.completed_by,
        }


def goal_payload(goal: GoalState | None) -> dict[str, Any] | None:
    """Return the canonical detached payload used by legacy and native hosts."""

    return goal.to_payload() if goal is not None else None


def goal_snapshot(
    goal: GoalState | None,
    *,
    action: str | None = None,
) -> dict[str, Any]:
    """Return the host-facing Goal tool snapshot envelope."""

    payload: dict[str, Any] = {
        "type": "goal_snapshot",
        "goal": goal_payload(goal),
    }
    if action is not None:
        payload["action"] = action
    return payload


def goal_state_from_payload(payload: object) -> GoalState | None:
    """Restore a GoalState from either legacy or snake_case payload keys."""

    if not isinstance(payload, dict):
        return None
    objective = _text(payload.get("objective"))
    if not objective:
        return None
    now = _now()
    return GoalState(
        objective=objective,
        status=_text(payload.get("status")) or "active",
        created_at=(
            _text(payload.get("createdAt"))
            or _text(payload.get("created_at"))
            or now
        ),
        updated_at=(
            _text(payload.get("updatedAt"))
            or _text(payload.get("updated_at"))
            or now
        ),
        evidence=_items(payload.get("evidence")),
        progress=_items(payload.get("progress")),
        blocked_reason=(
            _text(payload.get("blockedReason"))
            or _text(payload.get("blocked_reason"))
            or None
        ),
        completed_by=(
            _text(payload.get("completedBy"))
            or _text(payload.get("completed_by"))
            or None
        ),
    )


def goal_action_from_metadata(metadata: object) -> dict[str, Any] | None:
    """Translate host session metadata into the canonical Goal command."""

    if not isinstance(metadata, Mapping):
        return None
    raw_goal = metadata.get("goal")
    if isinstance(raw_goal, str):
        objective = raw_goal.strip()
        return {"action": "set", "objective": objective} if objective else None
    if isinstance(raw_goal, Mapping):
        request = dict(raw_goal)
        if "action" not in request and isinstance(request.get("objective"), str):
            request["action"] = "set"
        return request
    raw_objective = metadata.get("goal_objective") or metadata.get("goalObjective")
    if isinstance(raw_objective, str) and raw_objective.strip():
        return {"action": "set", "objective": raw_objective.strip()}
    return None


def _copy_state(goal: GoalState | None) -> GoalState | None:
    if goal is None:
        return None
    return GoalState(
        objective=goal.objective,
        status=goal.status,
        created_at=goal.created_at,
        updated_at=goal.updated_at,
        evidence=list(goal.evidence),
        progress=list(goal.progress),
        blocked_reason=goal.blocked_reason,
        completed_by=goal.completed_by,
    )


class GoalStore:
    """In-memory session store with the legacy Goal lifecycle semantics."""

    def __init__(self) -> None:
        self._goals: dict[str, GoalState] = {}

    def get(self, session_id: str = "") -> GoalState | None:
        return _copy_state(self._goals.get(str(session_id or "")))

    def snapshot(self, session_id: str = "") -> dict[str, Any] | None:
        """Return a detached, durable payload suitable for event/session storage."""

        goal = self.get(session_id)
        return goal.to_payload() if goal is not None else None

    def set(
        self,
        session_id: str,
        objective: str,
        *,
        evidence: object = None,
        progress: object = None,
        blocked_reason: str | None = None,
        completed_by: str | None = None,
    ) -> GoalState:
        objective = _text(objective)
        if not objective:
            raise ValueError("Goal objective cannot be empty.")
        now = _now()
        goal = GoalState(
            objective=objective,
            status="active",
            created_at=now,
            updated_at=now,
            evidence=_items(evidence),
            progress=_items(progress),
            blocked_reason=_text(blocked_reason) or None,
            completed_by=_text(completed_by) or None,
        )
        self._goals[str(session_id or "")] = goal
        return _copy_state(goal)  # type: ignore[return-value]

    def pause(self, session_id: str) -> GoalState | None:
        goal = self._goals.get(str(session_id or ""))
        if goal is None:
            return None
        goal.status = "paused"
        goal.updated_at = _now()
        return _copy_state(goal)


    def resume(self, session_id: str) -> GoalState | None:
        goal = self._goals.get(str(session_id or ""))
        if goal is None:
            return None
        goal.status = "active"
        goal.blocked_reason = None
        goal.updated_at = _now()
        return _copy_state(goal)

    def complete(
        self,
        session_id: str,
        *,
        evidence: object = None,
        progress: object = None,
        completed_by: str | None = None,
    ) -> GoalState | None:
        goal = self._goals.get(str(session_id or ""))
        if goal is None:
            return None
        goal.status = "complete"
        _extend(goal.evidence, _items(evidence))
        _extend(goal.progress, _items(progress))
        goal.blocked_reason = None
        if _text(completed_by):
            goal.completed_by = _text(completed_by)
        goal.updated_at = _now()
        return _copy_state(goal)

    def progress(
        self,
        session_id: str,
        values: object,
        *,
        evidence: object = None,
    ) -> GoalState | None:
        goal = self._goals.get(str(session_id or ""))
        if goal is None:
            return None
        _extend(goal.progress, _items(values))
        _extend(goal.evidence, _items(evidence))
        goal.updated_at = _now()
        return _copy_state(goal)

    def block(
        self,
        session_id: str,
        reason: str,
        *,
        evidence: object = None,
        progress: object = None,
    ) -> GoalState | None:
        goal = self._goals.get(str(session_id or ""))
        if goal is None:
            return None
        reason = _text(reason)
        if not reason:
            raise ValueError("Goal blocked_reason cannot be empty.")
        goal.status = "blocked"
        goal.blocked_reason = reason
        _extend(goal.evidence, _items(evidence))
        _extend(goal.progress, _items(progress))
        goal.updated_at = _now()
        return _copy_state(goal)

    def clear(self, session_id: str) -> GoalState | None:
        return _copy_state(self._goals.pop(str(session_id or ""), None))

    def restore(self, session_id: str, payload: object) -> GoalState | None:
        goal = goal_state_from_payload(payload)
        key = str(session_id or "")
        if goal is None:
            self._goals.pop(key, None)
            return None
        self._goals[key] = goal
        return _copy_state(goal)


class GoalToolStore(Protocol):
    """Minimal store contract consumed by Goal read/write Tool plugins."""

    def get(self, session_id: str = "") -> GoalState | None: ...

    def set(
        self,
        session_id: str,
        objective: str,
        *,
        evidence: object = None,
        progress: object = None,
        blocked_reason: str | None = None,
        completed_by: str | None = None,
    ) -> GoalState: ...

    def pause(self, session_id: str) -> GoalState | None: ...

    def resume(self, session_id: str) -> GoalState | None: ...

    def complete(
        self,
        session_id: str,
        *,
        evidence: object = None,
        progress: object = None,
        completed_by: str | None = None,
    ) -> GoalState | None: ...

    def progress(
        self,
        session_id: str,
        values: object,
        *,
        evidence: object = None,
    ) -> GoalState | None: ...

    def block(
        self,
        session_id: str,
        reason: str,
        *,
        evidence: object = None,
        progress: object = None,
    ) -> GoalState | None: ...

    def clear(self, session_id: str) -> GoalState | None: ...

    def restore(self, session_id: str, payload: object) -> GoalState | None: ...


def apply_goal_action(
    store: GoalToolStore,
    session_id: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Apply one host Goal command through the canonical lifecycle contract.

    ACP, CLI, SDK, and third-party adapters may translate their protocol into
    this small command envelope.  The function owns validation and stable
    result/error keys; adapters must not mutate ``GoalState`` themselves.
    """

    action = _text(params.get("action")).lower() or "get"
    action = {"status": "get", "create": "set"}.get(action, action)
    evidence = params.get("evidence")
    progress = params.get("progress")
    blocked_reason = params.get("blocked_reason") or params.get("blockedReason")
    completed_by = params.get("completed_by") or params.get("completedBy")

    def result(goal: GoalState | None) -> dict[str, Any]:
        return {"ok": True, "goal": goal_payload(goal)}

    if action == "get":
        return result(store.get(session_id))

    if action == "set":
        objective = _text(params.get("objective"))
        if not objective:
            return {"error": "empty_objective"}
        status = _text(params.get("status")).lower() or "active"
        if status not in {"active", "paused", "complete", "blocked"}:
            return {"error": f"invalid_status: {status}"}
        reason = _text(blocked_reason)
        if status == "blocked" and not reason:
            return {"error": "empty_blocked_reason"}
        goal = store.set(
            session_id,
            objective,
            evidence=evidence,
            progress=progress,
            blocked_reason=reason or None,
            completed_by=_text(completed_by) or None,
        )
        if status == "paused":
            goal = store.pause(session_id)
        elif status == "complete":
            goal = store.complete(
                session_id,
                evidence=evidence,
                progress=progress,
                completed_by=_text(completed_by) or None,
            )
        elif status == "blocked":
            goal = store.block(
                session_id,
                reason,
                evidence=evidence,
                progress=progress,
            )
        return result(goal)

    if action == "pause":
        goal = store.pause(session_id)
    elif action == "resume":
        goal = store.resume(session_id)
    elif action == "complete":
        goal = store.complete(
            session_id,
            evidence=evidence,
            progress=progress,
            completed_by=_text(completed_by) or None,
        )
    elif action == "progress":
        goal = store.progress(session_id, progress, evidence=evidence)
    elif action == "block":
        reason = _text(blocked_reason)
        if not reason:
            return {"error": "empty_blocked_reason"}
        try:
            goal = store.block(
                session_id,
                reason,
                evidence=evidence,
                progress=progress,
            )
        except ValueError:
            return {"error": "empty_blocked_reason"}
    elif action == "clear":
        store.clear(session_id)
        return result(None)
    else:
        return {"error": f"unknown_action: {action}"}

    if goal is None:
        return {"error": "goal_not_found"}
    return result(goal)


def _session_id(explicit: str | None) -> str:
    return str(explicit or current_runtime_invocation().session_id or "")


def _snapshot(goal: GoalState | None, *, action: str | None = None) -> dict[str, Any]:
    return goal_snapshot(goal, action=action)


def should_continue_goal_autopilot(goal: GoalState | None, stop_reason: str | None) -> bool:
    """Return whether an active Goal may safely continue after a turn."""

    return (
        goal is not None
        and goal.status == "active"
        and is_natural_end_reason(stop_reason)
    )


def goal_autopilot_progress_signature(goal: GoalState | None) -> tuple[Any, ...] | None:
    """Return the durable fields that count as autopilot progress."""

    if goal is None:
        return None
    return (
        goal.objective,
        goal.status,
        tuple(goal.progress),
        tuple(goal.evidence),
        goal.blocked_reason,
        goal.completed_by,
    )


def _serialize_autopilot_signature(signature: tuple[Any, ...] | None) -> list[Any] | None:
    """Encode the progress signature into JSON-safe checkpoint state."""

    if signature is None:
        return None
    return [
        signature[0],
        signature[1],
        list(signature[2]),
        list(signature[3]),
        signature[4],
        signature[5],
    ]


def _restore_autopilot_signature(value: object) -> tuple[Any, ...] | None:
    """Decode a persisted Goal progress signature defensively."""

    if not isinstance(value, (list, tuple)) or len(value) != 6:
        return None
    progress = value[2]
    evidence = value[3]
    if not isinstance(progress, (list, tuple)) or not isinstance(
        evidence, (list, tuple)
    ):
        return None
    return (
        str(value[0]),
        str(value[1]),
        tuple(str(item) for item in progress),
        tuple(str(item) for item in evidence),
        _text(value[4]) or None,
        _text(value[5]) or None,
    )


def goal_autopilot_prompt(
    goal: GoalState,
    continuation: int,
    max_continuations: int,
) -> str:
    """Build the deterministic Goal continuation prompt."""

    progress = "\n".join(f"- {item}" for item in goal.progress[-5:])
    evidence = "\n".join(f"- {item}" for item in goal.evidence[-5:])
    context_parts = []
    if progress:
        context_parts.append(f"Recent recorded progress:\n{progress}")
    if evidence:
        context_parts.append(f"Recent evidence:\n{evidence}")
    context = "\n\n".join(context_parts)
    context_block = f"\n\n{context}" if context else ""
    return (
        f"Goal autopilot continuation {continuation}/{max_continuations}.\n"
        "The previous turn ended while the durable goal is still active. Continue "
        "working from the current conversation and workspace state without waiting "
        "for another user instruction. Verify concrete state before claiming the "
        "goal is done. If the goal is satisfied, call `goal_write` with action "
        "`complete` and non-empty `evidence`. If an external dependency blocks "
        "progress, such as missing credentials, authorization, rate limits, a "
        "third-party service outage, or required user input, call `goal_write` "
        "with action `block` and a clear `blocked_reason`. If you make verified "
        "partial progress but the goal is still not done, call `goal_write` with "
        "action `progress` before ending. Avoid retrying the same failing external "
        "operation repeatedly without new evidence or a different approach."
        f"{context_block}"
    )


def goal_autopilot_terminal_message(
    *,
    enabled: bool,
    stop_cause: str | None,
    continuations: int,
    no_progress_turns: int | None = None,
) -> str:
    """Render the stable host-facing message for an autopilot stop.

    ``continuations`` counts all follow-up turns, while ``no_progress_turns``
    counts only consecutive turns without a durable Goal update.  The legacy
    CLI reports the latter for the no-progress guard; keeping the formatter in
    the workflow package lets ACP, CLI, and future SDK adapters share that
    observable contract.
    """

    if not enabled:
        return ""
    count = max(0, int(continuations))
    cause = str(stop_cause or "")
    if cause == "no_progress":
        progress_count = count if no_progress_turns is None else max(
            0, int(no_progress_turns)
        )
        return (
            "⚠️ Goal autopilot stopped after "
            f"{progress_count} continuation(s) without recorded goal progress; "
            "goal remains active."
        )
    if cause in {"continuation_budget", "time_budget"}:
        return (
            "⚠️ Goal autopilot stopped after "
            f"{count} continuation(s); goal remains active."
        )
    return ""


class GoalReadTool(Tool):
    """Read the current session Goal without exposing session_id in schema."""

    def __init__(self, store: GoalToolStore, *, session_id: str | None = None) -> None:
        self._store = store
        self._session_id = session_id

    @property
    def name(self) -> str:
        return "goal_read"

    @property
    def description(self) -> str:
        return (
            "Read the current durable session goal. Use this to check whether a goal "
            "is active, paused, complete, or unset before deciding whether to continue."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def compaction_state(self) -> tuple[str, str]:
        return "Goal", json.dumps(
            _snapshot(self._store.get(_session_id(self._session_id))),
            ensure_ascii=False,
        )

    async def execute(self) -> ToolResult:
        goal = self._store.get(_session_id(self._session_id))
        if goal is None:
            return ToolResult(success=True, content="No current goal.", raw_output=_snapshot(None))
        return ToolResult(
            success=True,
            content=f"Goal is {goal.status}: {goal.objective}",
            raw_output=_snapshot(goal),
        )


class GoalWriteTool(Tool):
    """Apply one validated lifecycle operation to the current session Goal."""

    def __init__(self, store: GoalToolStore, *, session_id: str | None = None) -> None:
        self._store = store
        self._session_id = session_id

    @property
    def name(self) -> str:
        return "goal_write"

    @property
    def description(self) -> str:
        return (
            "Update the durable session goal. Call action='complete' yourself when the "
            "active goal has been satisfied, and include evidence entries that name the "
            "files, tests, logs, command output, or artifacts proving completion; do not "
            "ask the user to run a slash command for completion. Use set/pause/resume/clear "
            "only when the user explicitly requests that lifecycle change. Use action='progress' "
            "to record verified progress, and action='block' with blocked_reason when external "
            "input is required."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["set", "pause", "resume", "complete", "clear", "progress", "block"],
                    "description": "Goal lifecycle operation.",
                },
                "objective": {
                    "type": "string",
                    "description": "Goal objective. Required for action='set'.",
                },
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Evidence for completion or progress, such as tests run, files changed, logs, or artifacts.",
                },
                "progress": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Verified progress updates to append to the goal snapshot.",
                },
                "blocked_reason": {
                    "type": "string",
                    "description": "Reason the goal is blocked. Required for action='block'.",
                },
                "completed_by": {
                    "type": "string",
                    "description": "Who or what completed the goal, for example 'model' or 'cli'.",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        objective: str | None = None,
        evidence: object = None,
        progress: object = None,
        blocked_reason: str | None = None,
        completed_by: str | None = None,
    ) -> ToolResult:
        action = _text(action).lower()
        session_id = _session_id(self._session_id)
        evidence_items = _items(evidence)
        progress_items = _items(progress)
        if action == "set":
            if not _text(objective):
                return ToolResult(success=False, error="'objective' is required for set.")
            goal = self._store.set(
                session_id,
                _text(objective),
                evidence=evidence_items,
                progress=progress_items,
                blocked_reason=blocked_reason,
                completed_by=completed_by,
            )
            return ToolResult(success=True, content=f"Set goal: {goal.objective}", raw_output=_snapshot(goal, action="set"))
        if action == "pause":
            goal = self._store.pause(session_id)
            return ToolResult(success=goal is not None, content="Paused the current goal." if goal else "", error=None if goal else "No goal to pause.", raw_output=_snapshot(goal, action="pause") if goal else None)
        if action == "resume":
            goal = self._store.resume(session_id)
            return ToolResult(success=goal is not None, content="Resumed the current goal." if goal else "", error=None if goal else "No goal to resume.", raw_output=_snapshot(goal, action="resume") if goal else None)
        if action == "complete":
            if not evidence_items:
                return ToolResult(success=False, error="'evidence' is required for complete. Include files, tests, logs, command output, or artifacts.")
            goal = self._store.complete(session_id, evidence=evidence_items, progress=progress_items, completed_by=completed_by or "model")
            return ToolResult(success=goal is not None, content="Marked the current goal complete." if goal else "", error=None if goal else "No goal to complete.", raw_output=_snapshot(goal, action="complete") if goal else None)
        if action == "progress":
            if not progress_items:
                return ToolResult(success=False, error="'progress' is required for progress.")
            goal = self._store.progress(session_id, progress_items, evidence=evidence_items)
            return ToolResult(success=goal is not None, content="Updated goal progress." if goal else "", error=None if goal else "No goal to update.", raw_output=_snapshot(goal, action="progress") if goal else None)
        if action == "block":
            if not _text(blocked_reason):
                return ToolResult(success=False, error="'blocked_reason' is required for block.")
            goal = self._store.block(session_id, _text(blocked_reason), evidence=evidence_items, progress=progress_items)
            return ToolResult(success=goal is not None, content=f"Marked goal blocked: {blocked_reason}" if goal else "", error=None if goal else "No goal to block.", raw_output=_snapshot(goal, action="block") if goal else None)
        if action == "clear":
            self._store.clear(session_id)
            return ToolResult(success=True, content="Cleared the current goal.", raw_output=_snapshot(None, action="clear"))
        return ToolResult(success=False, error=f"Unknown action: {action}")


class GoalWorkflowPolicy:
    """Session-scoped Goal policy with opt-in native continuation."""

    kind = "goal"
    checkpoint_injection_id = "workflow:goal"
    evidence_read_batch_size = 0

    def __init__(
        self,
        store: GoalStore,
        *,
        session_id: str | None = None,
        autopilot_enabled: bool = False,
        max_continuations: int = 0,
        no_progress_turns: int = 0,
        max_seconds: float | None = None,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.autopilot_enabled = bool(autopilot_enabled)
        self.max_continuations = max(0, int(max_continuations))
        self.no_progress_turns = max(0, int(no_progress_turns))
        self.max_seconds = (
            max(0.0, float(max_seconds)) if max_seconds is not None else None
        )
        self._started_at = time.monotonic()
        self._last_stop_reason: str | None = None
        self._autopilot_stop_cause: str | None = None
        self._last_signature: tuple[Any, ...] | None = None
        self._no_progress_count = 0
        self._continuation_count = 0

    def for_run(self, request: Any, bundle: Any | None = None) -> "GoalWorkflowPolicy":
        session_id = getattr(request, "session_id", self.session_id)
        options = getattr(request, "options", None)
        workflow_options = getattr(options, "workflow_options", {})
        if not isinstance(workflow_options, dict):
            workflow_options = {}
        autopilot_enabled = workflow_options.get(
            "autopilot_enabled", self.autopilot_enabled
        )
        max_continuations = workflow_options.get(
            "max_continuations", self.max_continuations
        )
        no_progress_turns = workflow_options.get(
            "no_progress_turns", self.no_progress_turns
        )
        max_seconds = workflow_options.get("max_seconds", self.max_seconds)
        bound = type(self)(
            self.store,
            session_id=session_id,
            autopilot_enabled=bool(autopilot_enabled),
            max_continuations=int(max_continuations),
            no_progress_turns=int(no_progress_turns),
            max_seconds=float(max_seconds) if max_seconds is not None else None,
        )
        recovery_events: list[Any] = []
        if bundle is not None:
            recovery_events.extend(getattr(bundle, "events", ()) or ())
        bound._continuation_count = sum(
            1
            for event in recovery_events
            if (
                getattr(event, "type", None)
                if not isinstance(event, dict)
                else event.get("type")
            )
            == "workflow.continuation.requested"
        )
        if bound.store.get(session_id or "") is None:
            recovered = _recovery_workflow_payload(request, bundle, "goal")
            if recovered is not None:
                goal_payload = recovered
                if (
                    isinstance(recovered.get("goal"), dict)
                    and "objective" not in recovered
                ):
                    goal_payload = recovered["goal"]
                bound.store.restore(session_id or "", goal_payload)
        recovered = _recovery_workflow_payload(request, bundle, "goal")
        if isinstance(recovered, dict):
            autopilot = recovered.get("autopilot")
            if isinstance(autopilot, dict):
                continuation_count = autopilot.get("continuation_count")
                if isinstance(continuation_count, int) and not isinstance(
                    continuation_count, bool
                ) and continuation_count >= 0:
                    bound._continuation_count = max(
                        bound._continuation_count,
                        continuation_count,
                    )
                no_progress_count = autopilot.get("no_progress_count")
                if isinstance(no_progress_count, int) and not isinstance(
                    no_progress_count, bool
                ) and no_progress_count >= 0:
                    bound._no_progress_count = no_progress_count
                stop_reason = autopilot.get("last_stop_reason")
                if isinstance(stop_reason, str) and stop_reason:
                    bound._last_stop_reason = stop_reason
                stop_cause = autopilot.get("stop_cause")
                if isinstance(stop_cause, str) and stop_cause:
                    bound._autopilot_stop_cause = stop_cause
                bound._last_signature = _restore_autopilot_signature(
                    autopilot.get("last_signature")
                )
        return bound

    def should_continue_autopilot(self, stop_reason: str | None) -> bool:
        """Expose a host-owned continuation decision without scheduling it."""

        if not self.autopilot_enabled:
            return False
        if self.max_continuations <= 0:
            return False
        if (
            self.max_seconds is not None
            and self.max_seconds > 0
            and time.monotonic() - self._started_at >= self.max_seconds
        ):
            return False
        if self._no_progress_count >= self.no_progress_turns > 0:
            return False
        return should_continue_goal_autopilot(
            self.store.get(self.session_id or ""), stop_reason
        )

    def continuation_prompt(self, continuation: int) -> str | None:
        goal = self.store.get(self.session_id or "")
        if goal is None or not should_continue_goal_autopilot(goal, "end_turn"):
            return None
        return goal_autopilot_prompt(goal, continuation, self.max_continuations)

    def record_autopilot_turn(
        self,
        *,
        stop_reason: str | None,
        before_signature: tuple[Any, ...] | None = None,
    ) -> None:
        """Record progress facts used by a host-owned autopilot controller."""

        self._last_stop_reason = stop_reason
        after_signature = goal_autopilot_progress_signature(
            self.store.get(self.session_id or "")
        )
        if should_continue_goal_autopilot(
            self.store.get(self.session_id or ""), stop_reason
        ):
            if after_signature == before_signature:
                self._no_progress_count += 1
            else:
                self._no_progress_count = 0
        self._last_signature = after_signature

    def next_continuation(
        self,
        *,
        stop_reason: str,
        final_content: str,
        step: int,
    ) -> WorkflowContinuation | None:
        """Translate Goal autopilot into the generic continuation SPI."""

        del final_content, step
        self._autopilot_stop_cause = None
        self.record_autopilot_turn(
            stop_reason=stop_reason,
            before_signature=self._last_signature,
        )
        if not self.autopilot_enabled:
            self._autopilot_stop_cause = "disabled"
            return None
        if self.max_continuations <= 0:
            self._autopilot_stop_cause = "continuation_budget"
            return None
        if (
            self.max_seconds is not None
            and self.max_seconds > 0
            and time.monotonic() - self._started_at >= self.max_seconds
        ):
            self._autopilot_stop_cause = "time_budget"
            return None
        if self._no_progress_count >= self.no_progress_turns > 0:
            self._autopilot_stop_cause = "no_progress"
            return None
        if not should_continue_goal_autopilot(
            self.store.get(self.session_id or ""), stop_reason
        ):
            self._autopilot_stop_cause = "goal_inactive"
            return None
        if self._continuation_count >= self.max_continuations:
            self._autopilot_stop_cause = "continuation_budget"
            return None
        next_count = self._continuation_count + 1
        prompt = self.continuation_prompt(next_count)
        if not prompt:
            self._autopilot_stop_cause = "goal_inactive"
            return None
        self._continuation_count = next_count
        return WorkflowContinuation(
            continuation_id=f"goal:{self.session_id or 'session'}:{next_count}",
            message=Message.user(prompt),
            reason="goal_autopilot",
            metadata={
                "workflow": self.kind,
                "continuation": next_count,
                "max_continuations": self.max_continuations,
            },
        )

    def terminal_metadata(
        self,
        stop_reason: str,
        final_content: str,
    ) -> dict[str, Any]:
        """Expose terminal Goal state and legacy autopilot facts.

        The legacy ACP/CLI adapters expose the latest Goal snapshot alongside
        the terminal response. Returning the detached store payload keeps that
        host contract available on the Native Kernel path without coupling the
        Kernel to Goal's state model.
        """

        del final_content
        cause = self._autopilot_stop_cause
        metadata: dict[str, Any] = {}
        goal = self.store.snapshot(self.session_id or "")
        if goal is not None:
            metadata["goal"] = goal
        metadata["goalAutopilot"] = {
            "enabled": self.autopilot_enabled,
            "continuations": self._continuation_count,
            "budgetExhausted": cause in {"continuation_budget", "time_budget"},
            "noProgressExhausted": cause == "no_progress",
            "noProgressTurns": self._no_progress_count,
            "lastStopReason": stop_reason or self._last_stop_reason,
            "stopCause": cause,
        }
        return metadata

    def build_checkpoint(self) -> str | None:
        goal = self.store.get(self.session_id or "")
        if goal is None:
            return None
        lines = [
            f"Goal [{goal.status}]: {goal.objective}",
            "Verify concrete files, tests, logs, command output, or artifacts before claiming completion.",
        ]
        if goal.progress:
            lines.append("Recent progress: " + "; ".join(goal.progress[-5:]))
        if goal.evidence:
            lines.append("Recent evidence: " + "; ".join(goal.evidence[-5:]))
        if goal.blocked_reason:
            lines.append("Blocked: " + goal.blocked_reason)
        return "\n".join(lines)

    def context_items(self, context: Any) -> tuple[ContextItem, ...]:
        """Expose the active Goal instruction through the context SPI.

        The legacy Agent prepended this instruction to each user message.  A
        native run keeps the user message data-only and contributes an
        equivalent system item through the normal Context Engine boundary.
        """

        del context
        goal = self.store.get(self.session_id or "")
        if goal is None or goal.status != "active":
            return ()
        text = (
            "## Active Goal\n"
            f"Objective: {goal.objective}\n\n"
            "Work toward this durable goal across turns. Treat completion as "
            "evidence-based: verify the objective against concrete files, tests, "
            "logs, command output, or artifacts before saying it is done. Keep "
            "changes scoped to the goal and the user's latest message. If the goal "
            "is satisfied, call `goal_write` with action `complete` and non-empty "
            "`evidence` before your final answer, then state the evidence that proves "
            "completion. Use `goal_write` action `progress` for verified partial "
            "progress and action `block` with `blocked_reason` when external input "
            "is required. Do not ask the user to run a slash command for this."
        )
        return (
            ContextItem(
                item_id=f"workflow:goal:{self.session_id or 'session'}",
                kind="workflow",
                content=text,
                priority=900,
                pinned=True,
                metadata={"role": "system", "workflow_context": True, "workflow": "goal"},
            ),
        )

    def decide(self, context: Any) -> None:
        """Goal currently contributes context, not deterministic tool actions."""

        del context
        return None

    def build_checkpoint_payload(self) -> dict[str, Any]:
        """Return the canonical state embedded in a durable checkpoint event."""

        return {
            self.kind: {
                "goal": self.store.snapshot(self.session_id or ""),
                "autopilot": {
                    "continuation_count": self._continuation_count,
                    "no_progress_count": self._no_progress_count,
                    "last_stop_reason": self._last_stop_reason,
                    "stop_cause": self._autopilot_stop_cause,
                    "last_signature": _serialize_autopilot_signature(
                        self._last_signature
                    ),
                },
            },
        }

    def on_event(self, event: Any) -> None:
        """Hydrate Goal state from replayed checkpoint/tool result events."""

        event_type = getattr(event, "type", None)
        payload = getattr(event, "payload", None)
        event_session_id = getattr(event, "session_id", "")
        if isinstance(event, dict):
            event_type = event.get("type", event_type)
            payload = event.get("payload", payload)
            event_session_id = event.get("session_id", event_session_id)
        if not isinstance(payload, dict):
            return
        goal_payload: object = None
        if event_type == "workflow.checkpoint":
            state = payload.get("workflow_state")
            if isinstance(state, dict):
                value = state.get("goal")
                goal_payload = value.get("goal") if isinstance(value, dict) else value
        elif event_type == "tool.call.completed":
            output = payload.get("output")
            if isinstance(output, dict) and output.get("type") == "goal_snapshot":
                goal_payload = output.get("goal")
            else:
                return
        else:
            return
        session_id = self.session_id or event_session_id
        if goal_payload is None:
            self.store.clear(session_id or "")
        else:
            self.store.restore(session_id or "", goal_payload)

    def update_checkpoint(self, checkpoint_text: str) -> Any:
        from .contract import WorkflowCheckpointUpdate

        return WorkflowCheckpointUpdate(text=checkpoint_text, changed=False)

    def next_deterministic_action(self) -> None:
        return None

    def plan_scope_error(self, tool_name: str, arguments: dict[str, Any]) -> None:
        return None

    def tool_call_error(self, tool_name: str, arguments: dict[str, Any], *, verified_evidence_urls: set[str], parallel: bool = False) -> None:
        return None

    def record_tool_result(self, tool_name: str, arguments: dict[str, Any], result: ToolResult, *, executed: bool = True) -> None:
        return None

    def handle_control(self, command: ControlCommand) -> dict[str, Any] | None:
        """Expose pause/resume as an optional host control extension."""

        session_id = self.session_id or command.session_id
        if command.kind in {"workflow.goal.pause", "goal.pause"}:
            goal = self.store.pause(session_id)
            return {"workflow": self.kind, "state": goal.status if goal else "missing"}
        if command.kind in {"workflow.goal.resume", "goal.resume"}:
            goal = self.store.resume(session_id)
            return {"workflow": self.kind, "state": goal.status if goal else "missing"}
        return None

    def exempts_tool_budget(self, tool_name: str) -> bool:
        return False

    def uses_evidence_read_budget(self, tool_name: str) -> bool:
        return False

    def is_direct_evidence_read_tool(self, tool_name: str) -> bool:
        return False

    def direct_evidence_url(self, tool_name: str, arguments: dict[str, Any], result: ToolResult) -> str | None:
        return None

    def allows_completion_continuation(self) -> bool:
        return False

    def suppresses_generic_final_summary(self) -> bool:
        return False


def build_goal_tools(store: GoalStore, *, session_id: str | None = None) -> tuple[Tool, Tool]:
    """Return the two Goal tools as a registry-ready plugin bundle."""

    return (
        GoalReadTool(store, session_id=session_id),
        GoalWriteTool(store, session_id=session_id),
    )


def _recovery_workflow_state(
    request: Any,
    bundle: Any | None,
    kind: str,
) -> dict[str, Any] | None:
    """Find the newest structured workflow state in a replayable event list."""

    events: list[Any] = []
    if bundle is not None:
        events.extend(getattr(bundle, "events", ()) or ())
    metadata = getattr(request, "metadata", {}) or {}
    session_events = metadata.get("_session_events", {}) if isinstance(metadata, dict) else {}
    if isinstance(session_events, dict):
        events.extend(session_events.get("events", ()) or ())
    for event in reversed(events):
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict) and isinstance(event, dict):
            payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        state = payload.get("workflow_state")
        if not isinstance(state, dict):
            continue
        value = state.get(kind)
        if isinstance(value, dict) and kind in value:
            value = value.get(kind)
        if isinstance(value, dict):
            return value
    return None


def _recovery_workflow_payload(
    request: Any,
    bundle: Any | None,
    kind: str,
) -> dict[str, Any] | None:
    """Return the raw plugin payload, preserving nested autopilot state."""

    events: list[Any] = []
    if bundle is not None:
        events.extend(getattr(bundle, "events", ()) or ())
    metadata = getattr(request, "metadata", {}) or {}
    session_events = metadata.get("_session_events", {}) if isinstance(metadata, dict) else {}
    if isinstance(session_events, dict):
        events.extend(session_events.get("events", ()) or ())
    for event in reversed(events):
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict) and isinstance(event, dict):
            payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        state = payload.get("workflow_state")
        if not isinstance(state, dict):
            continue
        value = state.get(kind)
        if isinstance(value, dict):
            return value
    return None


__all__ = [
    "extend_goal_items",
    "GoalReadTool",
    "GoalState",
    "GoalStore",
    "GoalToolStore",
    "GoalWorkflowPolicy",
    "GoalWriteTool",
    "build_goal_tools",
    "goal_items",
    "goal_payload",
    "goal_autopilot_progress_signature",
    "goal_autopilot_prompt",
    "goal_state_from_payload",
    "goal_snapshot",
    "should_continue_goal_autopilot",
]
