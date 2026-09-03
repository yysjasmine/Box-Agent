"""Kernel-owned composition of independent workflow policy plugins.

The Kernel owns one workflow port, while a real run may need several
independent policies (for example Goal + Plan + controlled PPT).  This
adapter fans lifecycle calls out to registered policies and keeps the Kernel
unaware of their concrete types.  A policy that does not implement an
optional method is simply treated as having no opinion.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..api import ContextItem
from ..api.contracts import WorkflowContinuation
from ..api.controls import ControlCommand
from ..api.workflows import WorkflowAction, WorkflowCheckpointUpdate


class _PolicyToolResultView:
    """Compatibility view for legacy policies receiving API ToolCallResult."""

    __slots__ = (
        "success",
        "content",
        "error",
        "model_context",
        "raw_output",
        "permission_request",
        "permission_decision",
    )

    def __init__(self, result: Any) -> None:
        self.success = getattr(result, "status", "") == "succeeded"
        self.content = str(getattr(result, "content", "") or "")
        error = getattr(result, "error", None)
        self.error = getattr(error, "message", error)
        self.model_context = getattr(result, "model_context", None)
        output = getattr(result, "output", None)
        self.raw_output = dict(output) if isinstance(output, Mapping) else output
        permission_request = getattr(result, "permission_request", None)
        self.permission_request = (
            dict(permission_request)
            if isinstance(permission_request, Mapping)
            else permission_request
        )
        permission_decision = getattr(result, "permission_decision", None)
        self.permission_decision = (
            dict(permission_decision)
            if isinstance(permission_decision, Mapping)
            else permission_decision
        )


def _policy_result(result: Any) -> Any:
    """Keep direct composite calls compatible with both result contracts."""

    if hasattr(result, "success"):
        return result
    if hasattr(result, "status"):
        return _PolicyToolResultView(result)
    return result


class CompositeWorkflowPolicy:
    """Delegate the stable :class:`WorkflowPolicy` contract to many plugins."""

    kind = "composite"
    checkpoint_injection_id = "workflow:composite"
    evidence_read_batch_size = 0

    def __init__(self, policies: Iterable[Any] = ()) -> None:
        self._policies = tuple(policy for policy in policies if policy is not None)
        sizes = [
            int(getattr(policy, "evidence_read_batch_size", 0) or 0)
            for policy in self._policies
        ]
        self.evidence_read_batch_size = max(sizes, default=0)

    @property
    def policies(self) -> tuple[Any, ...]:
        """Concrete policies, exposed read-only for diagnostics and tests."""

        return self._policies

    def for_run(self, request: Any, bundle: Any | None = None) -> "CompositeWorkflowPolicy":
        """Create run-local policy views when a plugin supports the seam."""

        values: list[Any] = []
        for policy in self._policies:
            factory = getattr(policy, "for_run", None)
            if not callable(factory):
                values.append(policy)
                continue
            try:
                value = factory(request, bundle)
            except TypeError:
                value = factory(request)
            values.append(value if value is not None else policy)
        return type(self)(values)

    def build_checkpoint(self) -> str | None:
        parts: list[str] = []
        for policy in self._policies:
            method = getattr(policy, "build_checkpoint", None)
            if not callable(method):
                continue
            value = method()
            if value:
                label = str(getattr(policy, "kind", "workflow"))
                parts.append(f"[{label}]\n{value}")
        return "\n\n".join(parts) if parts else None

    def context_items(self, context: Any) -> tuple[Any, ...]:
        """Collect model-context contributions from all workflow plugins."""

        values: list[Any] = []
        for policy in self._policies:
            method = getattr(policy, "context_items", None)
            if callable(method):
                try:
                    value = method(context)
                except Exception:
                    continue
            else:
                # Keep older policies usable while they adopt the richer
                # ``context_items`` SPI.  Without this fallback a composite
                # policy would hide a valid checkpoint from the model even
                # though the same policy works when installed directly.
                checkpoint = getattr(policy, "build_checkpoint", None)
                if not callable(checkpoint):
                    continue
                try:
                    value = checkpoint()
                except Exception:
                    continue
                if isinstance(value, str) and value.strip():
                    value = (
                        ContextItem(
                            item_id=f"workflow:{getattr(policy, 'kind', 'workflow')}",
                            kind="workflow",
                            content=value,
                            priority=900,
                            pinned=True,
                            metadata={
                                "role": "system",
                                "workflow_context": True,
                                "workflow": str(
                                    getattr(policy, "kind", "workflow")
                                ),
                            },
                        ),
                    )
                else:
                    value = ()
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                values.extend(value)
            else:
                values.append(value)
        return tuple(values)

    def initial_events(self, context: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        """Collect optional namespaced facts emitted at a run boundary."""

        values: list[Mapping[str, Any]] = []
        for policy in self._policies:
            method = getattr(policy, "initial_events", None)
            if not callable(method):
                continue
            try:
                result = method(context)
            except Exception:
                continue
            if isinstance(result, Mapping):
                result = (result,)
            if not isinstance(result, (list, tuple)):
                continue
            values.extend(
                item
                for item in result
                if isinstance(item, Mapping)
                and isinstance(item.get("type"), str)
                and item.get("type", "").strip()
                and isinstance(item.get("payload", {}), Mapping)
            )
        return tuple(values)

    def hidden_tool_names(self) -> frozenset[str]:
        """Union stage-hidden tools from every composed policy."""

        hidden: set[str] = set()
        for policy in self._policies:
            method = getattr(policy, "hidden_tool_names", None)
            if not callable(method):
                continue
            try:
                values = method()
            except Exception:
                continue
            hidden.update(
                str(name)
                for name in (values or ())
                if isinstance(name, str) and name.strip()
            )
        return frozenset(hidden)

    def filter_tool_schemas(
        self,
        schemas: Sequence[Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], ...]:
        """Apply catalog filters in policy order without mutating the host registry."""

        current: tuple[Mapping[str, Any], ...] = tuple(schemas or ())
        for policy in self._policies:
            method = getattr(policy, "filter_tool_schemas", None)
            if not callable(method):
                continue
            try:
                value = method(current)
            except Exception:
                continue
            if isinstance(value, (list, tuple)) and all(
                isinstance(item, Mapping) for item in value
            ):
                current = tuple(value)
        return current

    def required_tool_names(self) -> frozenset[str]:
        """Union required tool names exposed by pending workflow gates."""

        required: set[str] = set()
        for policy in self._policies:
            method = getattr(policy, "required_tool_names", None)
            if not callable(method):
                continue
            try:
                values = method()
            except Exception:
                continue
            required.update(
                str(name)
                for name in (values or ())
                if isinstance(name, str) and name.strip()
            )
        return frozenset(required)

    @property
    def restrict_tools_until_required_succeed(self) -> bool:
        """Keep a restricted catalog while any composed gate requires it."""

        return any(
            bool(getattr(policy, "restrict_tools_until_required_succeed", False))
            for policy in self._policies
        )

    @property
    def max_tool_calls(self) -> int | None:
        """Use the strictest positive workflow default across policies."""

        values = [
            value
            for policy in self._policies
            for value in [getattr(policy, "max_tool_calls", None)]
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        ]
        return min(values) if values else None

    def build_checkpoint_payload(self) -> dict[str, Any]:
        """Merge policy-owned checkpoint envelopes for durable replay."""

        state: dict[str, Any] = {}
        for policy in self._policies:
            method = getattr(policy, "build_checkpoint_payload", None)
            if not callable(method):
                continue
            value = method()
            if isinstance(value, Mapping):
                state.update({str(key): item for key, item in value.items()})
        return state

    async def on_event(self, event: Any) -> None:
        """Forward optional event-driven state hydration to each policy."""

        import inspect

        for policy in self._policies:
            callback = getattr(policy, "on_event", None)
            if not callable(callback):
                continue
            value = callback(event)
            if inspect.isawaitable(value):
                await value

    def update_checkpoint(self, checkpoint_text: str) -> WorkflowCheckpointUpdate:
        changed = False
        recovered: set[str] = set()
        for policy in self._policies:
            method = getattr(policy, "update_checkpoint", None)
            if not callable(method):
                continue
            value = method(self._policy_checkpoint(policy, checkpoint_text))
            if isinstance(value, WorkflowCheckpointUpdate):
                changed = changed or value.changed
                recovered.update(value.recovered_evidence_urls)
        return WorkflowCheckpointUpdate(
            text=checkpoint_text,
            changed=changed,
            recovered_evidence_urls=frozenset(recovered),
        )

    def next_deterministic_action(self) -> WorkflowAction | None:
        for policy in self._policies:
            method = getattr(policy, "next_deterministic_action", None)
            if not callable(method):
                continue
            value = method()
            if value is not None:
                return value
        return None

    def plan_scope_error(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        return self._first_text("plan_scope_error", tool_name, arguments)

    def tool_call_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        verified_evidence_urls: set[str],
        parallel: bool = False,
    ) -> str | None:
        for policy in self._policies:
            method = getattr(policy, "tool_call_error", None)
            if not callable(method):
                continue
            try:
                value = method(
                    tool_name,
                    arguments,
                    verified_evidence_urls=verified_evidence_urls,
                    parallel=parallel,
                )
            except TypeError as exc:
                if "parallel" not in str(exc):
                    raise
                value = method(
                    tool_name,
                    arguments,
                    verified_evidence_urls=verified_evidence_urls,
                )
            if isinstance(value, str) and value.strip():
                return value
        return None

    def record_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        *,
        executed: bool = True,
    ) -> None:
        policy_result = _policy_result(result)
        for policy in self._policies:
            method = getattr(policy, "record_tool_result", None)
            if callable(method):
                try:
                    method(tool_name, arguments, policy_result, executed=executed)
                except Exception:
                    continue

    def result_output(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> Mapping[str, Any] | None:
        """Return the first workflow-owned output decoration, if any."""

        for policy in self._policies:
            method = getattr(policy, "result_output", None)
            if not callable(method):
                continue
            try:
                value = method(tool_name, arguments, _policy_result(result))
            except Exception:
                continue
            if isinstance(value, Mapping):
                return dict(value)
        return None

    def pause_after_tool(
        self,
        tool_name: str,
        result: Any,
    ) -> str | None:
        """Return the first plugin-requested pause after a tool boundary."""

        policy_result = _policy_result(result)
        for policy in self._policies:
            method = getattr(policy, "pause_after_tool", None)
            if not callable(method):
                continue
            try:
                value = method(tool_name, policy_result)
            except Exception:
                continue
            if isinstance(value, str) and value.strip():
                return value
        return None

    def terminal_metadata(
        self,
        stop_reason: str,
        final_content: str,
    ) -> Mapping[str, Any]:
        """Merge workflow-owned terminal facts without naming a workflow."""

        metadata: dict[str, Any] = {}
        for policy in self._policies:
            method = getattr(policy, "terminal_metadata", None)
            if not callable(method):
                continue
            try:
                value = method(stop_reason, final_content)
            except Exception:
                continue
            if isinstance(value, Mapping):
                metadata.update(dict(value))
        return metadata

    def terminal_message(self, stop_reason: str, final_content: str) -> str | None:
        """Return the first workflow-owned recoverable terminal message."""

        for policy in self._policies:
            method = getattr(policy, "terminal_message", None)
            if not callable(method):
                continue
            try:
                value = method(stop_reason, final_content)
            except Exception:
                continue
            if isinstance(value, str) and value.strip():
                return value
        return None

    def record_visible_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        visible_content: str,
    ) -> None:
        """Forward the exact model-visible result to every interested policy."""

        policy_result = _policy_result(result)
        for policy in self._policies:
            method = getattr(policy, "record_visible_tool_result", None)
            if callable(method):
                try:
                    method(tool_name, arguments, policy_result, visible_content)
                except Exception:
                    continue

    def evidence_urls(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> tuple[str, ...]:
        """Collect URL provenance emitted by composed evidence policies."""

        policy_result = _policy_result(result)
        values: list[str] = []
        for policy in self._policies:
            method = getattr(policy, "evidence_urls", None)
            if callable(method):
                try:
                    urls = method(tool_name, arguments, policy_result)
                except Exception:
                    continue
            else:
                direct_check = getattr(policy, "is_direct_evidence_read_tool", None)
                direct_url = getattr(policy, "direct_evidence_url", None)
                if not callable(direct_check) or not callable(direct_url):
                    continue
                try:
                    if not direct_check(tool_name):
                        continue
                    urls = direct_url(tool_name, arguments, policy_result)
                except Exception:
                    continue
            if isinstance(urls, str):
                urls = (urls,)
            if isinstance(urls, (list, tuple, set, frozenset)):
                values.extend(
                    url for url in urls if isinstance(url, str) and url.strip()
                )
        return tuple(values)

    def begin_tool_decision(self, decision_id: int | str) -> None:
        """Notify all policies that a new model tool-decision boundary began."""

        for policy in self._policies:
            method = getattr(policy, "begin_tool_decision", None)
            if callable(method):
                try:
                    method(decision_id)
                except Exception:
                    continue

    def next_continuation(
        self,
        *,
        stop_reason: str,
        final_content: str,
        step: int,
    ) -> WorkflowContinuation | None:
        """Return the first continuation requested by a workflow plugin."""

        for policy in self._policies:
            method = getattr(policy, "next_continuation", None)
            if not callable(method):
                continue
            value = method(
                stop_reason=stop_reason,
                final_content=final_content,
                step=step,
            )
            if isinstance(value, WorkflowContinuation):
                return value
            if value is not None:
                # Keep malformed third-party values from changing loop
                # semantics; a plugin must return the typed contract.
                continue
        return None

    def record_model_response(
        self,
        *,
        content: str,
        finish_reason: str,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        """Notify every policy of the normalized model boundary."""

        for policy in self._policies:
            method = getattr(policy, "record_model_response", None)
            if not callable(method):
                continue
            try:
                method(
                    content=content,
                    finish_reason=finish_reason,
                    usage=usage,
                )
            except Exception:
                continue

    async def handle_control(
        self, command: ControlCommand
    ) -> Mapping[str, Any] | bool | None:
        """Apply one accepted host control to the first interested policy."""

        import inspect

        for policy in self._policies:
            method = getattr(policy, "handle_control", None)
            if not callable(method):
                continue
            value = method(command)
            if inspect.isawaitable(value):
                value = await value
            if value is True or isinstance(value, Mapping):
                return value
        return None

    def exempts_tool_budget(self, tool_name: str) -> bool:
        return any(
            bool(method(tool_name))
            for policy in self._policies
            for method in [getattr(policy, "exempts_tool_budget", None)]
            if callable(method)
        )

    def uses_evidence_read_budget(self, tool_name: str) -> bool:
        return any(
            bool(method(tool_name))
            for policy in self._policies
            for method in [getattr(policy, "uses_evidence_read_budget", None)]
            if callable(method)
        )

    def is_direct_evidence_read_tool(self, tool_name: str) -> bool:
        return any(
            bool(method(tool_name))
            for policy in self._policies
            for method in [getattr(policy, "is_direct_evidence_read_tool", None)]
            if callable(method)
        )

    def direct_evidence_url(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> str | None:
        policy_result = _policy_result(result)
        for policy in self._policies:
            method = getattr(policy, "direct_evidence_url", None)
            if not callable(method):
                continue
            try:
                value = method(tool_name, arguments, policy_result)
            except Exception:
                continue
            if isinstance(value, str) and value.strip():
                return value
        return None

    def allows_completion_continuation(self) -> bool:
        values = [
            bool(method())
            for policy in self._policies
            for method in [getattr(policy, "allows_completion_continuation", None)]
            if callable(method)
        ]
        return any(values) if values else False

    def suppresses_generic_final_summary(self) -> bool:
        return any(
            bool(method())
            for policy in self._policies
            for method in [getattr(policy, "suppresses_generic_final_summary", None)]
            if callable(method)
        )

    def _first_text(self, method_name: str, *args: Any) -> str | None:
        for policy in self._policies:
            method = getattr(policy, method_name, None)
            if not callable(method):
                continue
            value = method(*args)
            if isinstance(value, str) and value.strip():
                return value
        return None

    @staticmethod
    def _policy_checkpoint(policy: Any, combined: str) -> str:
        """Extract a policy's labeled section from a composite checkpoint."""

        label = f"[{getattr(policy, 'kind', 'workflow')}]\n"
        if not combined.startswith(label) and label not in combined:
            return combined
        start = combined.find(label) + len(label)
        tail = combined[start:]
        next_section = tail.find("\n\n[")
        return tail if next_section < 0 else tail[:next_section]


__all__ = ["CompositeWorkflowPolicy"]
