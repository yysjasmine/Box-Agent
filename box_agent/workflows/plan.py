"""Session-scoped Plan workflow plugins built on the legacy plan contract."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from collections.abc import Mapping
from typing import Any

from ..context import ContextItem
from ..api import (
    ToolCallResult,
    attach_plan_approval_payload,
    plan_approval_is_approved as _plan_approval_is_approved,
    plan_start_payload as _plan_start_payload,
)
from ..api.controls import ControlCommand
from ..tools.base import Tool, ToolResult
from ..tools.plan_tool import PlanReadTool, PlanStore, PlanWriteTool
from ..tools.runtime_context import current_runtime_invocation
from .turn_policy import text_is_short_non_task_reply, text_requests_plan_start


_FORCED_PLAN_GUIDANCE = (
    "Host UI requires a structured execution plan for this turn. "
    "Before giving the substantive answer, call `plan_write` with action `set` "
    "to publish the task objective, scope, steps, verification, risks, and assumptions. "
    "Keep the plan concise and relevant to the user's latest request."
)
_FORCED_PLAN_RETRY_GUIDANCE = (
    "The host is still waiting for the structured plan card. "
    "Call `plan_write` with action `set` now before continuing the answer."
)
_FORCED_PLAN_APPROVAL_GUIDANCE = (
    "Host UI requires an explicit user approval before execution. "
    "Call `plan_write` with action `set` to publish the task objective, scope, "
    "steps, verification, risks, and assumptions. Do not call execution tools "
    "such as file, bash, code, or sub-agent tools in this turn. After publishing "
    "the plan, stop and wait for the host to approve it."
)


class SessionPlanStore:
    """Keep one legacy-compatible ``PlanStore`` per session identifier."""

    def __init__(self) -> None:
        self._stores: dict[str, PlanStore] = {}

    def for_session(self, session_id: str) -> PlanStore:
        key = str(session_id or "")
        return self._stores.setdefault(key, PlanStore())

    def get(self, session_id: str = "") -> dict[str, Any] | None:
        return self.for_session(session_id).get()

    def snapshot(self, session_id: str = "") -> dict[str, Any] | None:
        """Return a detached plan payload for durable session state."""

        plan = self.get(session_id)
        return deepcopy(plan) if plan is not None else None

    def clear(self, session_id: str = "") -> None:
        self.for_session(session_id).clear()

    def set(self, session_id: str, **values: Any) -> dict[str, Any]:
        return self.for_session(session_id).set(**values)

    def restore(self, session_id: str, plan: dict[str, Any] | None) -> dict[str, Any] | None:
        """Restore a persisted plan through the same normalization path."""

        return self.for_session(session_id).restore(plan)


def _session_id(explicit: str | None) -> str:
    return str(explicit or current_runtime_invocation().session_id or "")


class SessionPlanWriteTool(Tool):
    """Registry-ready ``plan_write`` preserving the legacy schema/payload."""

    def __init__(self, store: SessionPlanStore, *, session_id: str | None = None) -> None:
        self._store = store
        self._session_id = session_id

    @property
    def name(self) -> str:
        return "plan_write"

    @property
    def description(self) -> str:
        return PlanWriteTool(self._store.for_session(_session_id(self._session_id))).description

    @property
    def parameters(self) -> dict[str, Any]:
        return PlanWriteTool(self._store.for_session(_session_id(self._session_id))).parameters

    async def execute(self, **arguments: Any) -> ToolResult:
        tool = PlanWriteTool(self._store.for_session(_session_id(self._session_id)))
        return await tool.execute(**arguments)


class SessionPlanReadTool(Tool):
    """Registry-ready ``plan_read`` preserving the legacy schema/payload."""

    def __init__(self, store: SessionPlanStore, *, session_id: str | None = None) -> None:
        self._store = store
        self._session_id = session_id

    @property
    def name(self) -> str:
        return "plan_read"

    @property
    def description(self) -> str:
        return PlanReadTool(self._store.for_session(_session_id(self._session_id))).description

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def compaction_state(self) -> tuple[str, str]:
        plan = self._store.get(_session_id(self._session_id))
        text = "No current plan."
        if plan is not None:
            text = PlanReadTool._format_plan(plan)
        return "Plan", text

    async def execute(self) -> ToolResult:
        tool = PlanReadTool(self._store.for_session(_session_id(self._session_id)))
        return await tool.execute()


class PlanWorkflowPolicy:
    """Session-scoped Plan policy with an optional approval preflight gate."""

    kind = "plan"
    checkpoint_injection_id = "workflow:plan"
    evidence_read_batch_size = 0

    def __init__(
        self,
        store: SessionPlanStore,
        *,
        session_id: str | None = None,
        require_plan_approval: bool = False,
        plan_approval: dict[str, Any] | None = None,
        force_plan_start: bool = False,
        plan_start_text: str | None = None,
        pause_after_plan_write: bool = False,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.require_plan_approval = bool(require_plan_approval)
        self.plan_approval = dict(plan_approval or {})
        self.force_plan_start = bool(force_plan_start)
        self.plan_start_text = str(plan_start_text or "")
        self.pause_after_plan_write = bool(pause_after_plan_write)
        self._approval_request_id = ""
        self._approval_state = (
            "approved" if _plan_approval_is_approved(self.plan_approval) else "none"
        )
        self._force_guidance_emitted = False

    def for_run(self, request: Any, bundle: Any | None = None) -> "PlanWorkflowPolicy":
        session_id = getattr(request, "session_id", self.session_id)
        options = getattr(request, "options", None)
        workflow_options = getattr(options, "workflow_options", {})
        if not isinstance(workflow_options, dict):
            workflow_options = {}
        require_approval = workflow_options.get(
            "require_plan_approval", self.require_plan_approval
        )
        raw_approval = workflow_options.get("plan_approval", self.plan_approval)
        approval = dict(raw_approval) if isinstance(raw_approval, dict) else {}
        force_plan_start = workflow_options.get(
            "force_plan_start",
            workflow_options.get("forcePlanStart", self.force_plan_start),
        )
        plan_start_text = workflow_options.get(
            "plan_start_text",
            workflow_options.get("planStartText", self.plan_start_text),
        )
        pause_after_plan_write = workflow_options.get(
            "pause_after_plan_write",
            workflow_options.get("pauseAfterPlanWrite", self.pause_after_plan_write),
        )
        bound = type(self)(
            self.store,
            session_id=session_id,
            require_plan_approval=bool(require_approval),
            plan_approval=approval,
            force_plan_start=bool(force_plan_start),
            plan_start_text=str(plan_start_text or ""),
            pause_after_plan_write=bool(pause_after_plan_write),
        )
        request_id = str(getattr(request, "request_id", "") or "")
        bound._approval_request_id = "plan-" + hashlib.sha1(
            f"{session_id}:{request_id}".encode("utf-8", errors="ignore")
        ).hexdigest()[:10]
        if approval.get("request_id") not in (None, "", bound._approval_request_id):
            bound._approval_state = "pending"
        elif _plan_approval_is_approved(approval):
            bound._approval_state = "approved"
        elif bound.require_plan_approval or bound.pause_after_plan_write:
            bound._approval_state = "pending"
        recovered_payload = _recovery_workflow_payload(request, bundle, "plan")
        if isinstance(recovered_payload, dict):
            recovered_approval = recovered_payload.get("approval")
            if isinstance(recovered_approval, dict):
                bound._approval_state = str(
                    recovered_approval.get("state", bound._approval_state)
                )
                recovered_request_id = recovered_approval.get("request_id")
                if isinstance(recovered_request_id, str) and recovered_request_id:
                    bound._approval_request_id = recovered_request_id
            recovered_guidance = recovered_payload.get("force_guidance_emitted")
            if isinstance(recovered_guidance, bool):
                bound._force_guidance_emitted = recovered_guidance
        if bound.store.get(session_id or "") is None:
            recovered_plan = recovered_payload
            if isinstance(recovered_payload, dict) and "plan" in recovered_payload:
                recovered_plan = recovered_payload.get("plan")
            if isinstance(recovered_plan, dict):
                bound.store.restore(session_id or "", recovered_plan)
        return bound

    def initial_events(self, context: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
        """Emit the legacy-compatible draft card before the first model call.

        This is an optional workflow event seam.  The Kernel only publishes
        the namespaced facts returned by the policy; it does not know what a
        plan card means or how a host renders it.
        """

        tool_names = context.get("tool_names", ())
        if "plan_write" not in tool_names:
            return ()
        latest_text = str(
            context.get("plan_start_text")
            or self.plan_start_text
            or context.get("latest_user_text", "")
        )
        force = bool(context.get("force_plan_start", self.force_plan_start))
        if not force and (
            text_is_short_non_task_reply(latest_text)
            or not text_requests_plan_start(latest_text)
        ):
            return ()
        recovery_events = context.get("recovery_events", ())
        if any(
            (
                getattr(event, "type", None)
                if not isinstance(event, Mapping)
                else event.get("type")
            )
            == "workflow.plan.snapshot"
            for event in recovery_events or ()
        ):
            return ()
        return (
            {
                "type": "workflow.plan.snapshot",
                "payload": _plan_start_payload(
                    request_id=self._approval_request_id,
                    approval_required=(
                        (self.require_plan_approval or self.pause_after_plan_write)
                        and self._approval_state != "approved"
                    ),
                ),
            },
        )

    def build_checkpoint(self) -> str | None:
        plan = self.store.get(self.session_id or "")
        if plan is None:
            if self.force_plan_start and self._force_guidance_emitted:
                return (
                    "Host UI requires a structured execution plan. "
                    "The plan guidance was delivered; waiting for `plan_write`."
                )
            return None
        lines = [f"Plan #{plan['id']}: {plan['title']} [{plan['status']}]"]
        if plan.get("objective"):
            lines.append(f"Objective: {plan['objective']}")
        if plan.get("scope"):
            lines.append(f"Scope: {plan['scope']}")
        for step in plan.get("steps", []):
            detail = f" - {step['details']}" if step.get("details") else ""
            lines.append(f"  {step['id']}. {step['title']}{detail}")
        return "\n".join(lines)

    def context_items(self, context: Any) -> tuple[ContextItem, ...]:
        """Expose approval instructions through the normal context boundary."""

        del context
        items: list[ContextItem] = []
        if self.force_plan_start and self.store.get(self.session_id or "") is None:
            if self._force_guidance_emitted:
                guidance = _FORCED_PLAN_RETRY_GUIDANCE
            elif self.require_plan_approval and self._approval_state != "approved":
                guidance = _FORCED_PLAN_APPROVAL_GUIDANCE
            else:
                guidance = _FORCED_PLAN_GUIDANCE
            self._force_guidance_emitted = True
            items.append(
                ContextItem(
                    item_id=f"workflow:plan-guidance:{self.session_id or 'session'}",
                    kind="workflow",
                    content=guidance,
                    priority=910,
                    pinned=True,
                    metadata={
                        "role": "system",
                        "workflow_context": True,
                        "workflow": self.kind,
                        "guidance": "force_plan_start",
                    },
                )
            )
        if (
            not (self.require_plan_approval or self.pause_after_plan_write)
            or self._approval_state == "approved"
        ):
            return tuple(items)
        items.append(
            ContextItem(
                item_id=f"workflow:plan-approval:{self.session_id or 'session'}",
                kind="workflow",
                content=(
                    "## Plan approval required\n"
                    "Create or update a plan with `plan_write` before executing "
                    "other tools. Execution remains paused until the host sends "
                    "an approved plan decision."
                ),
                priority=900,
                pinned=True,
                metadata={
                    "role": "system",
                    "workflow_context": True,
                    "workflow": self.kind,
                    "approval_request_id": self._approval_request_id,
                },
            )
        )
        return tuple(items)

    def decide(self, context: Any) -> None:
        """Plan state is exposed through tools/checkpoints, not auto-actions."""

        del context
        return None

    def build_checkpoint_payload(self) -> dict[str, Any]:
        """Return the canonical state embedded in a durable checkpoint event."""

        payload: dict[str, Any] = {"plan": self.store.snapshot(self.session_id or "")}
        payload["force_guidance_emitted"] = self._force_guidance_emitted
        if (
            self.require_plan_approval
            or self.pause_after_plan_write
            or self._approval_state != "none"
        ):
            payload["approval"] = {
                "required": self.require_plan_approval or self.pause_after_plan_write,
                "state": self._approval_state,
                "request_id": self._approval_request_id,
            }
        return {self.kind: payload}

    def on_event(self, event: Any) -> None:
        """Hydrate Plan state from replayed checkpoint/tool result events."""

        event_type = getattr(event, "type", None)
        payload = getattr(event, "payload", None)
        event_session_id = getattr(event, "session_id", "")
        if isinstance(event, dict):
            event_type = event.get("type", event_type)
            payload = event.get("payload", payload)
            event_session_id = event.get("session_id", event_session_id)
        if not isinstance(payload, dict):
            return
        plan_payload: object = None
        if event_type == "workflow.plan.snapshot":
            if payload.get("type") != "plan_snapshot":
                return
            plan_payload = payload.get("plan")
        elif event_type == "workflow.checkpoint":
            state = payload.get("workflow_state")
            if isinstance(state, dict):
                value = state.get("plan")
                if isinstance(value, dict):
                    plan_payload = value.get("plan")
                    guidance_emitted = value.get("force_guidance_emitted")
                    if isinstance(guidance_emitted, bool):
                        self._force_guidance_emitted = guidance_emitted
                    approval = value.get("approval")
                    if isinstance(approval, dict):
                        self._approval_state = str(
                            approval.get("state", self._approval_state)
                        )
                        request_id = approval.get("request_id")
                        if isinstance(request_id, str) and request_id:
                            self._approval_request_id = request_id
                else:
                    plan_payload = value
        elif event_type == "tool.call.completed":
            output = payload.get("output")
            if isinstance(output, dict) and output.get("type") == "plan_snapshot":
                plan_payload = output.get("plan")
                if (
                    self.require_plan_approval or self.pause_after_plan_write
                ) and payload.get("status") == "succeeded":
                    self._approval_state = "pending"
            else:
                return
        else:
            return
        session_id = self.session_id or event_session_id
        if plan_payload is None:
            self.store.clear(session_id or "")
        elif isinstance(plan_payload, dict):
            self.store.restore(session_id or "", plan_payload)

    def update_checkpoint(self, checkpoint_text: str) -> Any:
        from .contract import WorkflowCheckpointUpdate

        return WorkflowCheckpointUpdate(text=checkpoint_text, changed=False)

    def next_deterministic_action(self) -> None:
        return None

    def plan_scope_error(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        del arguments
        if (
            not (self.require_plan_approval or self.pause_after_plan_write)
            or self._approval_state == "approved"
        ):
            return None
        if tool_name in {"plan_write", "plan_read"}:
            return None
        return (
            "Plan approval is required before executing this tool. "
            f"Ask the host to approve plan request {self._approval_request_id}."
        )

    def tool_call_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        verified_evidence_urls: set[str],
        parallel: bool = False,
    ) -> str | None:
        del tool_name, arguments, verified_evidence_urls, parallel
        return None

    def record_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        *,
        executed: bool = True,
    ) -> None:
        del arguments
        succeeded = getattr(result, "success", None)
        if succeeded is None:
            succeeded = getattr(result, "status", None) == "succeeded"
        if (
            (self.require_plan_approval or self.pause_after_plan_write)
            and tool_name == "plan_write"
            and executed
            and bool(succeeded)
        ):
            self._approval_state = "pending"
        return None

    def result_output(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolCallResult,
    ) -> dict[str, Any] | None:
        """Decorate a native plan result with the pending approval envelope."""

        del arguments
        result_status = getattr(result, "status", None)
        if result_status is None:
            result_status = "succeeded" if bool(getattr(result, "success", False)) else "failed"
        if (
            tool_name != "plan_write"
            or result_status != "succeeded"
            or not (self.require_plan_approval or self.pause_after_plan_write)
            or self._approval_state == "approved"
        ):
            return None
        raw_value = getattr(result, "output", None)
        if raw_value is None:
            raw_value = getattr(result, "raw_output", None)
        return attach_plan_approval_payload(
            raw_value if isinstance(raw_value, Mapping) else None,
            request_id=self._approval_request_id,
        )

    def pause_after_tool(
        self,
        tool_name: str,
        result: ToolCallResult,
    ) -> str | None:
        """Request a durable pause after publishing a newly written plan."""

        result_status = getattr(result, "status", None)
        if result_status is None:
            result_status = "succeeded" if bool(getattr(result, "success", False)) else "failed"
        if (
            tool_name == "plan_write"
            and result_status == "succeeded"
            and (self.require_plan_approval or self.pause_after_plan_write)
            and self._approval_state != "approved"
        ):
            return "计划已生成，等待用户确认后再执行。"
        return None

    def terminal_metadata(
        self,
        stop_reason: str,
        final_content: str,
    ) -> dict[str, Any]:
        """Expose a recoverable approval pause to host adapters."""

        del final_content
        if stop_reason != "checkpoint_paused":
            return {}
        return {
            "planApproval": {
                "required": True,
                "state": self._approval_state,
                "requestId": self._approval_request_id,
            },
            "runStatus": "paused",
            "recoverable": True,
        }

    def handle_control(self, command: ControlCommand) -> dict[str, Any] | None:
        """Apply an operator approval without coupling the Kernel to Plan."""

        if command.kind not in {
            "workflow.plan.approve",
            "workflow.plan.approval",
            "plan.approve",
        }:
            return None
        payload = dict(command.payload)
        request_id = str(payload.get("request_id") or "")
        if request_id and request_id != self._approval_request_id:
            return None
        decision = str(payload.get("decision") or "").strip().lower()
        if _plan_approval_is_approved(payload):
            self._approval_state = "approved"
        elif decision in {
            "reject",
            "rejected",
            "deny",
            "denied",
            "cancel",
            "cancelled",
        }:
            self._approval_state = "rejected"
        else:
            return None
        self.plan_approval = payload
        return {
            "workflow": self.kind,
            "state": self._approval_state,
            "request_id": self._approval_request_id,
        }

    def exempts_tool_budget(self, tool_name: str) -> bool:
        return False

    def uses_evidence_read_budget(self, tool_name: str) -> bool:
        return False

    def is_direct_evidence_read_tool(self, tool_name: str) -> bool:
        return False

    def direct_evidence_url(
        self, tool_name: str, arguments: dict[str, Any], result: ToolResult
    ) -> str | None:
        return None

    def allows_completion_continuation(self) -> bool:
        return False

    def suppresses_generic_final_summary(self) -> bool:
        return False


def build_plan_tools(
    store: SessionPlanStore, *, session_id: str | None = None
) -> tuple[Tool, Tool]:
    return (
        SessionPlanWriteTool(store, session_id=session_id),
        SessionPlanReadTool(store, session_id=session_id),
    )


def _recovery_workflow_state(
    request: Any,
    bundle: Any | None,
    kind: str,
) -> dict[str, Any] | None:
    events: list[Any] = []
    if bundle is not None:
        raw_events = (
            bundle.get("events", ())
            if isinstance(bundle, dict)
            else getattr(bundle, "events", ())
        )
        events.extend(raw_events or ())
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
    """Return the raw plugin payload without unwrapping nested state."""

    events: list[Any] = []
    if bundle is not None:
        raw_events = (
            bundle.get("events", ())
            if isinstance(bundle, dict)
            else getattr(bundle, "events", ())
        )
        events.extend(raw_events or ())
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
        event_type = getattr(event, "type", None)
        if not isinstance(event_type, str) and isinstance(event, dict):
            event_type = event.get("type")
        if event_type == "workflow.plan.snapshot":
            if payload.get("type") == "plan_snapshot":
                return dict(payload)
            continue
        state = payload.get("workflow_state")
        if not isinstance(state, dict):
            continue
        value = state.get(kind)
        if isinstance(value, dict):
            return value
    return None


__all__ = [
    "PlanWorkflowPolicy",
    "SessionPlanReadTool",
    "SessionPlanStore",
    "SessionPlanWriteTool",
    "build_plan_tools",
]
