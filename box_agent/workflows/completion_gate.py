"""Evidence-backed completion gate workflow plugin.

Successful tool facts and filesystem evidence are evaluated at a terminal
model boundary. An explicit bounded ``WorkflowContinuation`` is returned when
requirements are still missing. The Kernel remains unaware of gate fields or
artifact policy details.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from ..api import ControlCommand, Message, WorkflowContinuation
from ..context import ContextItem
from .guards import (
    CompletionGate,
    completion_gate_gaps,
    completion_budget_reserve_text,
    completion_gate_text,
    completion_gate_tool_satisfies_requirements,
    delegated_tool_call_budget_message,
    delegated_tool_call_budget_wrapup_text,
    near_limit_wrapup_text,
    search_files_result_is_empty,
    tool_call_budget_message,
    tool_call_budget_wrapup_text,
    total_tool_call_budget_wrapup_text,
)
from .contract import is_natural_end_reason


def _string_items(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    )


def completion_gate_from_payload(
    payload: object,
    *,
    fallback: CompletionGate | None = None,
) -> CompletionGate:
    """Build a CompletionGate from the public data-only workflow envelope."""

    gate = fallback or CompletionGate()
    if not isinstance(payload, Mapping):
        return gate
    changes: dict[str, Any] = {}
    for name in (
        "required_tools",
        "success_report_artifact_suffixes",
        "budget_exempt_tools",
        "pause_tools",
    ):
        if name in payload:
            changes[name] = frozenset(_string_items(payload[name]))
    for name in (
        "required_artifacts",
        "required_changed_artifact_globs",
        "required_success_report_globs",
    ):
        if name in payload:
            changes[name] = _string_items(payload[name])
    for name in (
        "execution_result_criteria_count",
        "max_continuations",
        "max_tool_calls",
        "max_delegated_tool_calls",
        "web_search_total_limit",
        "completion_reserve_tool_calls",
    ):
        value = payload.get(name)
        if value is None and name in payload:
            changes[name] = None
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            changes[name] = value
    deadline = payload.get("deadline_seconds")
    if deadline is None and "deadline_seconds" in payload:
        changes["deadline_seconds"] = None
    elif isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
        changes["deadline_seconds"] = max(0.0, float(deadline))
    restricted = payload.get("restrict_tools_until_required_succeed")
    if isinstance(restricted, bool):
        changes["restrict_tools_until_required_succeed"] = restricted
    checkpoint_kind = payload.get("workflow_checkpoint_kind")
    if checkpoint_kind is None and "workflow_checkpoint_kind" in payload:
        changes["workflow_checkpoint_kind"] = None
    elif isinstance(checkpoint_kind, str):
        changes["workflow_checkpoint_kind"] = checkpoint_kind.strip() or None
    workflow_options = payload.get("workflow_options")
    if isinstance(workflow_options, Mapping):
        changes["workflow_options"] = dict(workflow_options)
    for name in (
        "baseline_artifact_signatures",
        "baseline_success_report_signatures",
    ):
        raw = payload.get(name)
        if not isinstance(raw, Mapping):
            continue
        normalized: dict[str, tuple[int, int]] = {}
        for path, signature in raw.items():
            if (
                isinstance(path, str)
                and isinstance(signature, (list, tuple))
                and len(signature) == 2
                and all(
                    isinstance(item, int) and not isinstance(item, bool)
                    for item in signature
                )
            ):
                normalized[path] = (signature[0], signature[1])
        changes[name] = normalized
    return replace(gate, **changes)


def completion_gate_to_payload(gate: CompletionGate) -> dict[str, Any]:
    """Serialize a CompletionGate into its stable data-only run envelope."""

    return {
        "required_tools": sorted(gate.required_tools),
        "execution_result_criteria_count": gate.execution_result_criteria_count,
        "restrict_tools_until_required_succeed": (
            gate.restrict_tools_until_required_succeed
        ),
        "required_artifacts": list(gate.required_artifacts),
        "required_changed_artifact_globs": list(
            gate.required_changed_artifact_globs
        ),
        "baseline_artifact_signatures": {
            path: list(signature)
            for path, signature in gate.baseline_artifact_signatures.items()
        },
        "required_success_report_globs": list(gate.required_success_report_globs),
        "success_report_artifact_suffixes": sorted(
            gate.success_report_artifact_suffixes
        ),
        "baseline_success_report_signatures": {
            path: list(signature)
            for path, signature in gate.baseline_success_report_signatures.items()
        },
        "max_continuations": gate.max_continuations,
        "deadline_seconds": gate.deadline_seconds,
        "max_tool_calls": gate.max_tool_calls,
        "max_delegated_tool_calls": gate.max_delegated_tool_calls,
        "web_search_total_limit": gate.web_search_total_limit,
        "budget_exempt_tools": sorted(gate.budget_exempt_tools),
        "completion_reserve_tool_calls": gate.completion_reserve_tool_calls,
        "pause_tools": sorted(gate.pause_tools),
        "workflow_checkpoint_kind": gate.workflow_checkpoint_kind,
        "workflow_options": dict(gate.workflow_options),
    }


@dataclass
class CompletionGateWorkflowPolicy:
    """Run-scoped, bounded completion gate for native Kernel runs."""

    gate: CompletionGate
    workspace_dir: str | Path | None = None
    kind = "completion_gate"
    checkpoint_injection_id = "workflow:completion-gate"
    evidence_read_batch_size = 0
    _succeeded_tools: set[str] = field(default_factory=set, init=False)
    _continuation_count: int = field(default=0, init=False)
    _paused: bool = field(default=False, init=False)
    _released: bool = field(default=False, init=False)
    _elapsed_before_resume: float = field(default=0.0, init=False)
    _started_at: float = field(default_factory=time.monotonic, init=False)
    _delegated_tool_call_total: int = field(default=0, init=False)
    _web_search_call_total: int = field(default=0, init=False)
    _tool_call_total: int = field(default=0, init=False)
    _completion_reserve_injected: bool = field(default=False, init=False)
    _counted_delegation_call_ids: set[str] = field(default_factory=set, init=False)
    _counted_web_search_call_ids: set[str] = field(default_factory=set, init=False)
    _counted_budget_call_ids: set[str] = field(default_factory=set, init=False)
    _max_steps: int = field(default=8, init=False)
    _effective_tool_call_limit: int | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        # Hosts may use a policy directly in a registry without calling
        # ``for_run`` first. Preserve the gate's configured default in that
        # source-compatible construction path.
        if self._effective_tool_call_limit is None:
            self._effective_tool_call_limit = self.gate.max_tool_calls

    def for_run(self, request: Any, bundle: Any | None = None) -> "CompletionGateWorkflowPolicy":
        """Bind a fresh policy to one Run and replay durable gate facts."""

        metadata = getattr(request, "metadata", {})
        workspace = self.workspace_dir
        if isinstance(metadata, Mapping) and isinstance(metadata.get("workspace_dir"), str):
            workspace = metadata["workspace_dir"] or workspace
        options = getattr(request, "options", None)
        workflow_options = getattr(options, "workflow_options", {})
        gate_payload = (
            workflow_options.get("completion_gate")
            if isinstance(workflow_options, Mapping)
            else None
        )
        bound = type(self)(
            completion_gate_from_payload(gate_payload, fallback=self.gate),
            workspace,
        )
        requested_steps = getattr(options, "max_steps", None)
        if isinstance(requested_steps, int) and not isinstance(requested_steps, bool):
            bound._max_steps = max(1, requested_steps)
        requested_tool_limit = getattr(options, "max_tool_calls", None)
        if isinstance(requested_tool_limit, int) and not isinstance(
            requested_tool_limit, bool
        ):
            bound._effective_tool_call_limit = requested_tool_limit
        else:
            bound._effective_tool_call_limit = bound.gate.max_tool_calls
        if isinstance(bundle, Mapping):
            events = tuple(bundle.get("events", ()) or ())
        else:
            events = tuple(getattr(bundle, "events", ()) or ()) if bundle is not None else ()
        for event in events:
            event_type = getattr(event, "type", None)
            payload = getattr(event, "payload", None)
            if isinstance(event, Mapping):
                event_type = event.get("type", event_type)
                payload = event.get("payload", payload)
            if not isinstance(payload, Mapping):
                continue
            if event_type == "tool.call.requested":
                bound._replay_budget_request(payload)
                continue
            if (
                event_type == "tool.call.completed"
                and payload.get("status") == "succeeded"
                and str(payload.get("content", "") or "").strip()
                and not (
                    payload.get("tool_name") == "search_files"
                    and search_files_result_is_empty(
                        SimpleNamespace(
                            success=True,
                            content=payload.get("content", ""),
                            raw_output=payload.get("output"),
                        )
                    )
                )
            ):
                tool_name = payload.get("tool_name")
                arguments = payload.get("arguments", {})
                if isinstance(tool_name, str) and isinstance(arguments, Mapping):
                    if completion_gate_tool_satisfies_requirements(
                        self.gate,
                        tool_name,
                        dict(arguments),
                    ):
                        bound._succeeded_tools.add(tool_name)
                    if tool_name in self.gate.pause_tools:
                        bound._paused = True
                    bound._replay_budget_result(tool_name, payload)
            elif event_type == "workflow.continuation.requested":
                bound._continuation_count += 1
            elif event_type == "workflow.checkpoint":
                state = payload.get("workflow_state")
                if isinstance(state, Mapping):
                    value = state.get(self.kind)
                    if isinstance(value, Mapping):
                        bound._restore_state(value)
        return bound

    def _replay_budget_request(self, payload: Mapping[str, Any]) -> None:
        """Restore reservation counters from a durable pre-execution fact."""

        tool_name = str(payload.get("tool_name", "") or "")
        call_id = str(payload.get("call_id", "") or "")
        if call_id and call_id in self._counted_budget_call_ids:
            return
        if tool_name not in self.gate.budget_exempt_tools:
            self._tool_call_total += 1
        if call_id:
            self._counted_budget_call_ids.add(call_id)
        if tool_name == "web_search":
            self._web_search_call_total += 1
            if call_id:
                self._counted_web_search_call_ids.add(call_id)

    def _replay_budget_result(
        self,
        tool_name: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Restore workflow-owned budget counters from durable tool facts."""

        call_id = str(payload.get("call_id", "") or "")
        if call_id and call_id not in self._counted_budget_call_ids:
            if tool_name not in self.gate.budget_exempt_tools:
                self._tool_call_total += 1
            self._counted_budget_call_ids.add(call_id)
        if tool_name == "web_search":
            if call_id and call_id in self._counted_web_search_call_ids:
                return
            self._web_search_call_total += 1
            if call_id:
                self._counted_web_search_call_ids.add(call_id)
            return
        if tool_name != "sub_agent":
            return
        output = payload.get("output")
        if not isinstance(output, Mapping) or output.get("type") != "sub_agent_delegation":
            return
        nested = output.get("tool_calls")
        if isinstance(nested, bool) or not isinstance(nested, int) or nested <= 0:
            return
        if call_id and call_id in self._counted_delegation_call_ids:
            return
        self._delegated_tool_call_total += nested
        if call_id:
            self._counted_delegation_call_ids.add(call_id)

    def _elapsed(self) -> float:
        return self._elapsed_before_resume + max(0.0, time.monotonic() - self._started_at)

    def _restore_state(self, state: Mapping[str, Any]) -> None:
        tools = state.get("succeeded_tools")
        if isinstance(tools, (list, tuple, set)):
            self._succeeded_tools.update(
                str(tool) for tool in tools if isinstance(tool, str) and tool
            )
        count = state.get("continuations")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            self._continuation_count = max(self._continuation_count, count)
        delegated = state.get("delegated_tool_calls")
        if isinstance(delegated, int) and not isinstance(delegated, bool) and delegated >= 0:
            self._delegated_tool_call_total = max(
                self._delegated_tool_call_total,
                delegated,
            )
        searches = state.get("web_search_calls")
        if isinstance(searches, int) and not isinstance(searches, bool) and searches >= 0:
            self._web_search_call_total = max(self._web_search_call_total, searches)
        total = state.get("tool_calls")
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            self._tool_call_total = max(self._tool_call_total, total)
        self._completion_reserve_injected = bool(
            state.get("completion_reserve_injected", self._completion_reserve_injected)
        )
        self._paused = bool(state.get("paused", self._paused))
        self._released = bool(state.get("released", self._released))
        elapsed = state.get("elapsed_seconds")
        if isinstance(elapsed, (int, float)) and elapsed >= 0:
            self._elapsed_before_resume = float(elapsed)
            self._started_at = time.monotonic()

    def build_checkpoint(self) -> str | None:
        gaps = completion_gate_gaps(
            self.gate,
            self._succeeded_tools,
            str(self.workspace_dir) if self.workspace_dir is not None else None,
        )
        if not gaps:
            return "Completion gate satisfied."
        return "Completion gate pending:\n" + "\n".join(f"- {gap}" for gap in gaps)

    def build_checkpoint_payload(self) -> dict[str, Any]:
        return {
            self.kind: {
                "succeeded_tools": sorted(self._succeeded_tools),
                "continuations": self._continuation_count,
                "delegated_tool_calls": self._delegated_tool_call_total,
                "web_search_calls": self._web_search_call_total,
                "tool_calls": self._tool_call_total,
                "completion_reserve_injected": self._completion_reserve_injected,
                "paused": self._paused,
                "released": self._released,
                "elapsed_seconds": self._elapsed(),
            }
        }

    def required_tool_names(self) -> frozenset[str]:
        """Expose the gate's required tool set to the generic Kernel.

        This is intentionally a tiny capability query rather than a
        ``CompletionGate`` special case in the loop.  Other workflow plugins
        can implement the same method when they need stage-specific schema
        visibility.
        """

        return frozenset(self.gate.required_tools) - self._succeeded_tools

    @property
    def restrict_tools_until_required_succeed(self) -> bool:
        return bool(self.gate.restrict_tools_until_required_succeed)

    @property
    def max_tool_calls(self) -> int | None:
        """Expose the gate's default cap when a host did not override it."""

        return self._effective_tool_call_limit

    def filter_tool_schemas(self, schemas: Any) -> tuple[Mapping[str, Any], ...]:
        """Return the optional restricted catalog without mutating the host."""

        values = tuple(item for item in (schemas or ()) if isinstance(item, Mapping))
        if not self.restrict_tools_until_required_succeed:
            return values
        pending = set(self.gate.required_tools) - self._succeeded_tools
        if not pending:
            return values
        allowed = pending | {"tool_search"}
        result: list[Mapping[str, Any]] = []
        for schema in values:
            function = schema.get("function")
            name = schema.get("name")
            if isinstance(function, Mapping):
                name = name or function.get("name")
            if str(name or "") in allowed:
                result.append(schema)
        return tuple(result)

    def on_event(self, event: Any) -> None:
        """Rehydrate the same facts when installed through ``WorkflowEventHook``."""

        event_type = getattr(event, "type", None)
        payload = getattr(event, "payload", None)
        if isinstance(event, Mapping):
            event_type = event.get("type", event_type)
            payload = event.get("payload", payload)
        if not isinstance(payload, Mapping):
            return
        if (
            event_type == "tool.call.completed"
            and payload.get("status") == "succeeded"
            and str(payload.get("content", "") or "").strip()
            and not (
                payload.get("tool_name") == "search_files"
                and search_files_result_is_empty(
                    SimpleNamespace(
                        success=True,
                        content=payload.get("content", ""),
                        raw_output=payload.get("output"),
                    )
                )
            )
        ):
            tool_name = payload.get("tool_name")
            arguments = payload.get("arguments", {})
            if isinstance(tool_name, str) and isinstance(arguments, Mapping):
                if completion_gate_tool_satisfies_requirements(
                    self.gate, tool_name, dict(arguments)
                ):
                    self._succeeded_tools.add(tool_name)
                if tool_name in self.gate.pause_tools:
                    self._paused = True
        elif event_type == "workflow.continuation.requested":
            metadata = payload.get("metadata")
            count = metadata.get("continuation") if isinstance(metadata, Mapping) else None
            if isinstance(count, int) and not isinstance(count, bool):
                self._continuation_count = max(self._continuation_count, count)
        elif event_type == "workflow.checkpoint":
            state = payload.get("workflow_state")
            if isinstance(state, Mapping):
                value = state.get(self.kind)
                if isinstance(value, Mapping):
                    self._restore_state(value)

    def context_items(self, context: Any) -> tuple[ContextItem, ...]:
        if self._released or self._paused:
            return ()
        gaps = completion_gate_gaps(
            self.gate,
            self._succeeded_tools,
            str(self.workspace_dir) if self.workspace_dir is not None else None,
        )
        if not gaps:
            return ()
        items = [
            ContextItem(
                item_id="workflow:completion-gate",
                kind="workflow",
                content=(
                    "A completion gate is active. Verify concrete tool/artifact "
                    "facts before claiming completion."
                ),
                priority=890,
                pinned=True,
                metadata={
                    "role": "system",
                    "workflow_context": True,
                    "workflow": self.kind,
                },
            ),
        ]
        reserve = max(0, int(self.gate.completion_reserve_tool_calls))
        limit = self.max_tool_calls
        if (
            reserve > 0
            and limit is not None
            and self._tool_call_total >= max(0, limit - reserve)
            and not self._completion_reserve_injected
            and self.gate.pause_tools.isdisjoint(self._succeeded_tools)
        ):
            self._completion_reserve_injected = True
            items.append(
                ContextItem(
                    item_id="workflow:completion-gate:reserve",
                    kind="workflow",
                    content=completion_budget_reserve_text(gaps, reserve),
                    priority=895,
                    pinned=True,
                    metadata={
                        "role": "system",
                        "workflow_context": True,
                        "workflow": self.kind,
                        "budget_guidance": True,
                    },
                )
            )

        step = context.get("step") if isinstance(context, Mapping) else None
        if isinstance(step, int) and step > 0:
            # Keep the near-limit nudge workflow-owned, observable, and
            # replayable without teaching the Kernel what a completion gate
            # means.
            remaining_steps = self._max_steps - step
            if self._max_steps > 3 and remaining_steps <= 2:
                items.append(
                    ContextItem(
                        item_id="workflow:completion-gate:near-limit",
                        kind="workflow",
                        content=near_limit_wrapup_text(step - 1, self._max_steps),
                        priority=896,
                        pinned=True,
                        metadata={
                            "role": "system",
                            "workflow_context": True,
                            "workflow": self.kind,
                            "budget_guidance": True,
                        },
                    )
                )

        total_limit = self.max_tool_calls
        if total_limit is not None and self._tool_call_total >= total_limit:
            items.append(
                ContextItem(
                    item_id="workflow:completion-gate:tool-budget",
                    kind="workflow",
                    content=total_tool_call_budget_wrapup_text(total_limit),
                    priority=897,
                    pinned=True,
                    metadata={
                        "role": "system",
                        "workflow_context": True,
                        "workflow": self.kind,
                        "budget_guidance": True,
                    },
                )
            )
        search_limit = self.gate.web_search_total_limit
        if search_limit is not None and self._web_search_call_total >= search_limit:
            items.append(
                ContextItem(
                    item_id="workflow:completion-gate:web-search-budget",
                    kind="workflow",
                    content=tool_call_budget_wrapup_text("web_search", search_limit),
                    priority=898,
                    pinned=True,
                    metadata={
                        "role": "system",
                        "workflow_context": True,
                        "workflow": self.kind,
                        "budget_guidance": True,
                    },
                )
            )
        delegated_limit = self.gate.max_delegated_tool_calls
        if (
            delegated_limit is not None
            and self._delegated_tool_call_total >= delegated_limit
        ):
            items.append(
                ContextItem(
                    item_id="workflow:completion-gate:delegated-budget",
                    kind="workflow",
                    content=delegated_tool_call_budget_wrapup_text(delegated_limit),
                    priority=899,
                    pinned=True,
                    metadata={
                        "role": "system",
                        "workflow_context": True,
                        "workflow": self.kind,
                        "budget_guidance": True,
                    },
                )
            )
        return tuple(items)

    def record_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        *,
        executed: bool = True,
    ) -> None:
        if (
            not executed
            or not bool(getattr(result, "success", False))
            or not str(getattr(result, "content", "") or "").strip()
            or (
                tool_name == "search_files"
                and search_files_result_is_empty(result)
            )
        ):
            return
        if completion_gate_tool_satisfies_requirements(
            self.gate, tool_name, dict(arguments)
        ):
            self._succeeded_tools.add(tool_name)
        if tool_name in self.gate.pause_tools:
            self._paused = True
        if tool_name == "sub_agent":
            raw_output = getattr(result, "raw_output", None)
            if isinstance(raw_output, Mapping) and raw_output.get("type") == "sub_agent_delegation":
                nested = raw_output.get("tool_calls")
                if (
                    isinstance(nested, int)
                    and not isinstance(nested, bool)
                    and nested > 0
                ):
                    self._delegated_tool_call_total += nested

    def next_continuation(
        self,
        *,
        stop_reason: str,
        final_content: str,
        step: int,
    ) -> WorkflowContinuation | None:
        del final_content, step
        if not is_natural_end_reason(stop_reason) or self._released or self._paused:
            return None
        if self.gate.deadline_seconds is not None and self._elapsed() >= self.gate.deadline_seconds:
            return None
        if self._continuation_count >= max(0, int(self.gate.max_continuations)):
            return None
        gaps = completion_gate_gaps(
            self.gate,
            self._succeeded_tools,
            str(self.workspace_dir) if self.workspace_dir is not None else None,
        )
        if not gaps:
            return None
        self._continuation_count += 1
        return WorkflowContinuation(
            continuation_id=f"completion-gate:{self._continuation_count}",
            message=Message.user(completion_gate_text(gaps)),
            reason="completion_gate",
            metadata={
                "workflow": self.kind,
                "continuation": self._continuation_count,
                "max_continuations": self.gate.max_continuations,
                "gaps": list(gaps),
            },
        )

    def terminal_metadata(
        self,
        stop_reason: str,
        final_content: str,
    ) -> dict[str, Any]:
        """Expose bounded gate state without changing terminal semantics."""

        del final_content
        gaps = completion_gate_gaps(
            self.gate,
            self._succeeded_tools,
            str(self.workspace_dir) if self.workspace_dir is not None else None,
        )
        max_continuations = max(0, int(self.gate.max_continuations))
        tool_budget_exhausted = bool(
            self.max_tool_calls is not None
            and self._tool_call_total >= self.max_tool_calls
        )
        deadline_exhausted = bool(
            self.gate.deadline_seconds is not None
            and self._elapsed() >= self.gate.deadline_seconds
        )
        return {
            "completionGate": {
                "continuations": self._continuation_count,
                "maxContinuations": max_continuations,
                "budgetExhausted": (
                    max_continuations == 0
                    or self._continuation_count >= max_continuations
                    or tool_budget_exhausted
                ),
                "timeBudgetExhausted": deadline_exhausted,
                "released": self._released,
                "paused": self._paused,
                "gaps": list(gaps),
                "toolCalls": self._tool_call_total,
                "delegatedToolCalls": self._delegated_tool_call_total,
                "webSearchCalls": self._web_search_call_total,
                "lastStopReason": stop_reason,
            }
        }

    def terminal_message(self, stop_reason: str, final_content: str) -> str | None:
        """Return the recoverable pause message at a bounded gate boundary."""

        del final_content
        if not is_natural_end_reason(stop_reason) or self._released or self._paused:
            return None
        gaps = completion_gate_gaps(
            self.gate,
            self._succeeded_tools,
            str(self.workspace_dir) if self.workspace_dir is not None else None,
        )
        if not gaps:
            return None
        continuation_exhausted = (
            self._continuation_count
            >= max(0, int(self.gate.max_continuations))
            or (
                self.max_tool_calls is not None
                and self._tool_call_total >= self.max_tool_calls
            )
        )
        deadline_exhausted = bool(
            self.gate.deadline_seconds is not None
            and self._elapsed() >= self.gate.deadline_seconds
        )
        if not continuation_exhausted and not deadline_exhausted:
            return None
        return (
            "The recoverable workflow reached its bounded continuation boundary "
            "with delivery work remaining. Progress was saved to a durable "
            "workspace checkpoint; continue this task to resume from canonical "
            "artifacts."
        )

    def handle_control(self, command: ControlCommand) -> dict[str, Any] | None:
        if command.kind not in {"workflow.completion_gate.release", "completion_gate.release"}:
            return None
        self._released = True
        return {"workflow": self.kind, "state": "released"}

    def decide(self, context: Any) -> None:
        del context
        return None

    def update_checkpoint(self, checkpoint_text: str) -> Any:
        from .contract import WorkflowCheckpointUpdate

        return WorkflowCheckpointUpdate(text=checkpoint_text, changed=True)

    def next_deterministic_action(self) -> None:
        return None

    def plan_scope_error(self, tool_name: str, arguments: dict[str, Any]) -> None:
        del tool_name, arguments
        return None

    def tool_call_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        verified_evidence_urls: set[str] | None = None,
        parallel: bool = False,
    ) -> str | None:
        """Apply workflow-specific delegated/search budgets before execution."""

        del arguments, verified_evidence_urls, parallel
        if (
            tool_name == "sub_agent"
            and self.gate.max_delegated_tool_calls is not None
            and self._delegated_tool_call_total >= self.gate.max_delegated_tool_calls
        ):
            return delegated_tool_call_budget_message(
                self.gate.max_delegated_tool_calls
            )
        if (
            tool_name == "web_search"
            and self.gate.web_search_total_limit is not None
            and self._web_search_call_total >= self.gate.web_search_total_limit
        ):
            return tool_call_budget_message(
                tool_name,
                self.gate.web_search_total_limit,
            )
        if tool_name == "web_search" and self.gate.web_search_total_limit is not None:
            # Reserve at preflight so parallel batches cannot oversubscribe.
            self._web_search_call_total += 1
        if (
            tool_name not in self.gate.budget_exempt_tools
            and self.max_tool_calls is not None
            and self._tool_call_total < self.max_tool_calls
        ):
            self._tool_call_total += 1
        return None

    def exempts_tool_budget(self, tool_name: str) -> bool:
        return tool_name in self.gate.budget_exempt_tools

    def uses_evidence_read_budget(self, tool_name: str) -> bool:
        del tool_name
        return False

    def is_direct_evidence_read_tool(self, tool_name: str) -> bool:
        del tool_name
        return False

    def direct_evidence_url(self, tool_name: str, arguments: dict[str, Any], result: Any) -> str | None:
        del tool_name, arguments, result
        return None

    def allows_completion_continuation(self) -> bool:
        return True

    def suppresses_generic_final_summary(self) -> bool:
        return False


__all__ = ["CompletionGateWorkflowPolicy"]
