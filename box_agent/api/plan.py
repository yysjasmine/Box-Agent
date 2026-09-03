"""Stable host-facing Plan snapshot and approval payload protocol."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


_APPROVED_DECISIONS = frozenset(
    {
        "approve",
        "approved",
        "accept",
        "accepted",
        "confirm",
        "confirmed",
        "execute",
        "proceed",
        "yes",
    }
)


def plan_approval_is_approved(value: object) -> bool:
    """Return whether a host approval payload authorizes execution."""

    if not isinstance(value, Mapping):
        return False
    return str(value.get("decision") or "").strip().lower() in _APPROVED_DECISIONS


def plan_approval_payload(
    *,
    request_id: str,
    state: str,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Build the approval envelope attached to a Plan snapshot."""

    payload: dict[str, Any] = {
        "required": True,
        "state": state,
        "request_id": request_id,
    }
    if plan_id:
        payload["plan_id"] = plan_id
    return payload


def attach_plan_approval_payload(
    raw_output: Mapping[str, Any] | None,
    *,
    request_id: str,
    state: str = "pending",
) -> dict[str, Any]:
    """Attach approval state to a Plan tool result without mutating it."""

    output = dict(raw_output or {})
    if output.get("type") != "plan_snapshot":
        output = {
            "type": "plan_snapshot",
            "version": 1,
            "action": "set",
            "plan": None,
            "summary": {
                "steps": 0,
                "verification": 0,
                "risks": 0,
                "assumptions": 0,
            },
        }

    plan = output.get("plan")
    plan_id: str | None = None
    if isinstance(plan, Mapping):
        normalized_plan = dict(plan)
        normalized_plan["status"] = (
            "draft"
            if state == "pending"
            else str(normalized_plan.get("status") or "active")
        )
        output["plan"] = normalized_plan
        raw_plan_id = normalized_plan.get("id")
        if raw_plan_id is not None:
            plan_id = str(raw_plan_id)

    output["approval"] = plan_approval_payload(
        request_id=request_id,
        state=state,
        plan_id=plan_id,
    )
    return output


def plan_start_payload(
    *,
    approval: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    approval_required: bool = False,
) -> dict[str, Any]:
    """Build the draft Plan card emitted before model output."""

    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "type": "plan_snapshot",
        "version": 1,
        "action": "start",
        "plan": {
            "id": "pending",
            "title": "正在制定执行方案",
            "objective": "根据当前请求梳理目标、范围、步骤、验证方式和风险。",
            "scope": "",
            "status": "draft",
            "steps": [],
            "verification": [],
            "risks": [],
            "assumptions": [],
            "created_at": now,
            "updated_at": now,
        },
        "summary": {
            "steps": 0,
            "verification": 0,
            "risks": 0,
            "assumptions": 0,
        },
    }
    if approval is not None:
        payload["approval"] = dict(approval)
    elif approval_required:
        payload["approval"] = plan_approval_payload(
            request_id=request_id or "",
            state="drafting",
            plan_id="pending",
        )
    return payload


__all__ = [
    "attach_plan_approval_payload",
    "plan_approval_is_approved",
    "plan_approval_payload",
    "plan_start_payload",
]
