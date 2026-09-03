"""Canonical registry-backed Tool Engine boundary.

All hosts reach this engine through the Kernel. It validates schema and
permission before execution and returns stable API values rather than
tool-implementation-specific results.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Iterable, Mapping
from typing import Any

from box_agent.api import (
    ErrorCode,
    ErrorInfo,
    PermissionDecision,
    PermissionRequest,
    ToolCallRequest,
    ToolCallResult,
)
from box_agent.plugins import TypedRegistry

from .runtime_context import scoped_runtime_invocation
from .model_tool_context import reset_model_tool_context, set_model_tool_context


_REGISTRATION_METADATA_KEYS = (
    "registration_source",
    "registration_version",
    "registration_state_schema",
    "tool_id",
    "mcp_server",
)


async def _publish_permission_requested(
    context: Any | None,
    request: PermissionRequest,
) -> None:
    """Use the Kernel-owned typed event seam when a run context provides it."""

    publish = getattr(context, "publish_permission_requested", None)
    if not callable(publish):
        return
    value = publish(request)
    if inspect.isawaitable(value):
        await value


async def _publish_permission_resolved(
    context: Any | None,
    decision: PermissionDecision,
) -> None:
    """Publish a decision before entering the Tool executor."""

    publish = getattr(context, "publish_permission_resolved", None)
    if not callable(publish):
        return
    value = publish(decision)
    if inspect.isawaitable(value):
        await value


class RegistryToolEngine:
    """Resolve a Tool plugin by scope, validate, execute, and normalize output."""

    def __init__(
        self,
        registry: TypedRegistry,
        *,
        descriptor_registry: TypedRegistry | None = None,
        permission_gateway: Any | None = None,
        effect_ledger: Any | None = None,
        hooks: Iterable[Any] = (),
        defer_effect_prepare: bool = False,
        defer_effect_completion: bool = False,
    ) -> None:
        self._registry = registry
        self._descriptor_registry = descriptor_registry
        self._permission_gateway = permission_gateway
        self._effect_ledger = effect_ledger
        self._hooks = tuple(hook for hook in hooks if hook is not None)
        # Native Kernel services set this flag so the initial prepared/running
        # fence is staged in memory and committed with ``tool.call.requested``.
        # Standalone Tool Engine callers retain the historical immediate
        # ledger writes unless explicitly opted into the durable boundary.
        self._defer_effect_prepare = bool(defer_effect_prepare)
        # Native Kernel services set this flag so the terminal effect
        # transition is committed together with ``tool.call.completed``. A
        # standalone Tool Engine keeps the historical immediate-completion
        # behavior unless explicitly opted into the durable boundary.
        self._defer_effect_completion = bool(defer_effect_completion)
        self._pending_effects: dict[str, dict[str, Any]] = {}
        self._prepared_invocations: dict[str, dict[str, Any]] = {}

    def schemas(self) -> tuple[dict[str, Any], ...]:
        """Return provider-neutral schemas for all visible registered tools."""

        schemas: list[dict[str, Any]] = []
        seen: set[str] = set()
        keys = [registration.key for registration in self._registry.registrations()]
        if self._descriptor_registry is not None:
            keys.extend(
                registration.key
                for registration in self._descriptor_registry.registrations()
            )
        for key in keys:
            tool = self._registry.resolve(key, scope="run")
            descriptor = (
                self._descriptor_registry.resolve(key, scope="run")
                if self._descriptor_registry is not None
                else None
            )
            if tool is None and descriptor is None:
                continue
            name = str(
                getattr(tool, "name", None)
                or getattr(descriptor, "name", None)
                or (descriptor.get("name") if isinstance(descriptor, Mapping) else None)
                or key
            )
            if name in seen:
                continue
            seen.add(name)
            schema_source = descriptor if descriptor is not None else tool
            to_schema = getattr(schema_source, "to_schema", None)
            if callable(to_schema):
                schema = dict(to_schema())
            elif isinstance(schema_source, Mapping):
                schema = dict(schema_source)
            else:
                schema = {
                    "name": name,
                    "description": str(
                        getattr(schema_source, "description", "")
                    ),
                    "input_schema": dict(
                        getattr(schema_source, "parameters", {}) or {}
                    ),
                }
            schemas.append(schema)
        return tuple(schemas)

    def supports_workflow_action(self, tool_name: str, capability: str) -> bool:
        """Require an executor to opt in to trusted, model-bypassing actions."""

        tool = self._resolve_tool(tool_name)
        declared = getattr(tool, "runtime_workflow_actions", ()) if tool is not None else ()
        return isinstance(declared, (set, frozenset, tuple, list)) and capability in declared

    async def end_run(
        self,
        *,
        session_id: str,
        run_id: str,
        metadata: Mapping[str, Any],
    ) -> None:
        """Run each resolved Tool's cleanup contract exactly once."""

        tools: list[Any] = []
        seen: set[int] = set()
        for registration in self._registry.registrations():
            tool = self._registry.resolve(registration.key, scope="run")
            if tool is None or id(tool) in seen:
                continue
            seen.add(id(tool))
            tools.append(tool)
        failures: list[str] = []
        with scoped_runtime_invocation(
            session_id=session_id,
            run_id=run_id,
            metadata=metadata,
        ):
            for tool in tools:
                cleanup = getattr(tool, "end_run", None)
                if not callable(cleanup):
                    continue
                try:
                    value = cleanup()
                    if inspect.isawaitable(value):
                        await value
                except Exception as exc:
                    failures.append(
                        f"{getattr(tool, 'name', type(tool).__name__)}: "
                        f"{type(exc).__name__}: {exc}"
                    )
        if failures:
            raise RuntimeError("tool run cleanup failed: " + "; ".join(failures))

    async def execute(
        self,
        request: ToolCallRequest,
        *,
        context: Any | None = None,
    ) -> ToolCallResult:
        prepared = self._prepared_invocations.pop(request.call_id, None)
        if prepared is None:
            # Keep malformed/unknown calls out of the effect ledger. Validation
            # is side-effect free and must happen before preparing a replay
            # fence; otherwise a bad model call could leave an unresolved
            # ``running`` effect that blocks future legitimate retries.
            tool, validation_error = self._resolve_and_validate(request)
            if validation_error is not None:
                return validation_error
            request = self._bind_registration_provenance(request)
            # Permission preflight is deliberately outside the effect ledger
            # boundary. A denied/malformed model call must not create a
            # running effect before the executor is even eligible to run.
            preflight_result, permission_decision = await self._preflight(
                tool,
                request,
                context=context,
            )
            if preflight_result is not None:
                return preflight_result
            original_request = request
            request, intercepted = await self._before_tool_hooks(request, context=context)
            if intercepted is not None:
                return intercepted
            # If an interceptor rewrote arguments, run preflight against the
            # final call too. This keeps permission coverage conservative while
            # still allowing redaction/defaulting hooks after the initial check.
            if (
                request.tool_name != original_request.tool_name
                or dict(request.arguments) != dict(original_request.arguments)
            ):
                tool, validation_error = self._resolve_and_validate(request)
                if validation_error is not None:
                    return validation_error
                request = self._bind_registration_provenance(request)
                preflight_result, permission_decision = await self._preflight(
                    tool,
                    request,
                    context=context,
                )
                if preflight_result is not None:
                    return preflight_result
            effect = await self._prepare_effect(request)
        else:
            # ``prepare_call`` has already executed all side-effect-free gates
            # and staged the initial effect fence. Do not repeat hooks,
            # permission checks, or ledger transitions before invoking.
            request = prepared["request"]
            tool = prepared["tool"]
            permission_decision = prepared.get("permission_decision")
            effect = prepared.get("effect")
        if isinstance(effect, ToolCallResult):
            return effect
        result = await self._execute_untracked(
            request,
            context=context,
            tool=tool,
            validated=True,
            preflighted=True,
            permission_decision=permission_decision,
        )
        result = await self._after_tool_hooks(request, result, context=context)
        if effect is not None:
            effect_id, _ = effect
            if result.status == "permission_required":
                # Permission is a pause, not a terminal effect outcome.
                if self._defer_effect_completion:
                    self._stage_effect(
                        request.call_id,
                        effect_id=effect_id,
                        status="prepared",
                        result=None,
                        run_id=str(request.metadata.get("run_id", "")),
                        idempotency_key=str(request.metadata.get("idempotency_key", "")),
                        request_digest=str(effect[1]),
                    )
                else:
                    try:
                        await self._effect_ledger.complete(
                            effect_id=effect_id, status="prepared", result=None
                        )
                    except Exception:
                        pass
                return result
            status = "succeeded" if result.status == "succeeded" else "failed"
            if self._defer_effect_completion:
                self._stage_effect(
                    request.call_id,
                    effect_id=effect_id,
                    status=status,
                    result=result.to_dict(),
                    run_id=str(request.metadata.get("run_id", "")),
                    idempotency_key=str(request.metadata.get("idempotency_key", "")),
                    request_digest=str(effect[1]),
                )
            else:
                try:
                    await self._effect_ledger.complete(
                        effect_id=effect_id,
                        status=status,
                        result=result.to_dict(),
                    )
                except Exception as exc:
                    return ToolCallResult(
                        call_id=request.call_id,
                        status="failed",
                        error=ErrorInfo(
                            code=ErrorCode.INTERNAL_ERROR,
                            category="effect",
                            message=f"effect ledger completion failed: {type(exc).__name__}: {exc}",
                            details={"exception_type": type(exc).__name__},
                        ),
                    )
        return result

    async def prepare_call(
        self,
        request: ToolCallRequest,
        *,
        context: Any | None = None,
    ) -> tuple[ToolCallRequest, ToolCallResult | None]:
        """Run all pre-executor gates and stage a durable effect fence.

        The Kernel calls this before publishing ``tool.call.requested``. When
        effect preparation is deferred, the staged ``running`` transition is
        then committed by the Service in the same SQLite transaction as that
        event, checkpoint, and outbox record. The returned request is the
        post-hook canonical call that the executor must receive.
        """

        tool, validation_error = self._resolve_and_validate(request)
        if validation_error is not None:
            return request, validation_error
        request = self._bind_registration_provenance(request)
        preflight_result, permission_decision = await self._preflight(
            tool,
            request,
            context=context,
        )
        if preflight_result is not None:
            return request, preflight_result
        original_request = request
        request, intercepted = await self._before_tool_hooks(request, context=context)
        if intercepted is not None:
            return request, intercepted
        if (
            request.tool_name != original_request.tool_name
            or dict(request.arguments) != dict(original_request.arguments)
        ):
            tool, validation_error = self._resolve_and_validate(request)
            if validation_error is not None:
                return request, validation_error
            request = self._bind_registration_provenance(request)
            preflight_result, permission_decision = await self._preflight(
                tool,
                request,
                context=context,
            )
            if preflight_result is not None:
                return request, preflight_result
        effect = await self._prepare_effect(request)
        if isinstance(effect, ToolCallResult):
            return request, effect
        self._prepared_invocations[request.call_id] = {
            "request": request,
            "tool": tool,
            "permission_decision": permission_decision,
            "effect": effect,
        }
        return request, None

    def _bind_registration_provenance(
        self,
        request: ToolCallRequest,
    ) -> ToolCallRequest:
        """Attach registry-owned identity to the durable Tool call fact."""

        request = self.canonicalize_call(request)
        tool = self._resolve_tool(request.tool_name)

        resolve_registration = getattr(
            self._registry,
            "resolve_registration",
            None,
        )
        registration = (
            resolve_registration(request.tool_name, scope="run")
            if callable(resolve_registration)
            else None
        )
        metadata = dict(request.metadata)
        for key in _REGISTRATION_METADATA_KEYS:
            metadata.pop(key, None)
        if registration is not None:
            source = str(registration.source)
            metadata.update(
                {
                    "registration_source": source,
                    "registration_version": str(registration.version),
                    "registration_state_schema": str(
                        registration.state_schema
                    ),
                }
            )
            if source.startswith("mcp.server:"):
                metadata["mcp_server"] = source.removeprefix("mcp.server:")
            tool_id = str(getattr(tool, "mcp_tool_id", "") or "").strip()
            if tool_id:
                metadata["tool_id"] = tool_id
        return ToolCallRequest(
            call_id=request.call_id,
            tool_name=request.tool_name,
            arguments=request.arguments,
            metadata=metadata,
        )

    def canonicalize_call(self, request: ToolCallRequest) -> ToolCallRequest:
        """Resolve an accepted alias to the Tool's single protocol identity."""

        tool = self._resolve_tool(request.tool_name)
        canonical = str(getattr(tool, "name", "") or "").strip()
        if not canonical or canonical == request.tool_name:
            return request
        metadata = dict(request.metadata)
        metadata.setdefault("requested_tool_name", request.tool_name)
        return ToolCallRequest(
            call_id=request.call_id,
            tool_name=canonical,
            arguments=request.arguments,
            metadata=metadata,
        )

    def pending_effects_for_call(
        self, call_id: str
    ) -> tuple[tuple[str, str, str, dict[str, Any] | None, str, str, str], ...]:
        """Return the staged effect transition for a Tool boundary event.

        The return value is deliberately data-only so the Kernel and Service
        can hand it to any durable store without importing this engine's
        implementation details.
        """

        pending = self._pending_effects.get(call_id)
        if pending is None:
            return ()
        return (
            (
                call_id,
                str(pending["effect_id"]),
                str(pending["status"]),
                pending.get("result"),
                str(pending.get("run_id", "")),
                str(pending.get("idempotency_key", "")),
                str(pending.get("request_digest", "")),
            ),
        )

    def acknowledge_effect_boundary(
        self, call_id: str, *, event_type: str = "tool.call.completed"
    ) -> None:
        """Forget terminal staged state after its durable event commits.

        The initial ``tool.call.requested`` boundary intentionally retains the
        running fence until the later completion event.
        """

        if event_type == "tool.call.requested":
            return
        self._pending_effects.pop(call_id, None)

    async def finalize_effect_boundary(
        self,
        call_id: str,
        *,
        event_type: str = "tool.call.completed",
    ) -> None:
        """Fallback for stores that predate transactional effect support."""

        pending = self._pending_effects.get(call_id)
        if pending is None or self._effect_ledger is None:
            return
        effect_id = str(pending["effect_id"])
        status = str(pending["status"])
        result = pending.get("result")
        if event_type == "tool.call.requested":
            await self._effect_ledger.prepare(
                effect_id=effect_id,
                run_id=str(pending.get("run_id", "")),
                idempotency_key=str(pending.get("idempotency_key", "")),
                request_digest=str(pending.get("request_digest", "")),
            )
            if status == "running":
                await self._effect_ledger.complete(
                    effect_id=effect_id, status="running", result=None
                )
            return
        await self._effect_ledger.complete(effect_id=effect_id, status=status, result=result)
        self.acknowledge_effect_boundary(call_id, event_type=event_type)

    def _stage_effect(
        self,
        call_id: str,
        *,
        effect_id: str,
        status: str,
        result: dict[str, Any] | None,
        run_id: str = "",
        idempotency_key: str = "",
        request_digest: str = "",
    ) -> None:
        self._pending_effects[call_id] = {
            "effect_id": effect_id,
            "status": status,
            "result": result,
            "run_id": run_id,
            "idempotency_key": idempotency_key,
            "request_digest": request_digest,
        }

    async def _before_tool_hooks(
        self,
        request: ToolCallRequest,
        *,
        context: Any | None,
    ) -> tuple[ToolCallRequest, ToolCallResult | None]:
        """Apply optional tool interceptors after initial permission preflight."""

        current = request
        for hook in self._hooks:
            callback = getattr(hook, "before_tool", None)
            legacy_callback = getattr(hook, "on_tool_start", None)
            if not callable(callback) and not callable(legacy_callback):
                continue
            try:
                if callable(callback):
                    try:
                        value = callback(current, context=context)
                    except TypeError as exc:
                        if "context" not in str(exc):
                            raise
                        value = callback(current)
                else:
                    value = legacy_callback(
                        tool_call_id=current.call_id,
                        tool_name=current.tool_name,
                        arguments=dict(current.arguments),
                    )
                if inspect.isawaitable(value):
                    value = await value
            except Exception as exc:
                return current, ToolCallResult(
                    call_id=current.call_id,
                    status="failed",
                    error=ErrorInfo(
                        code=ErrorCode.INTERNAL_ERROR,
                        category="hook",
                        message=f"tool before-hook failed: {type(exc).__name__}: {exc}",
                        details={"exception_type": type(exc).__name__},
                    ),
                )
            if value is None:
                continue
            if isinstance(value, ToolCallResult):
                return current, value
            if isinstance(value, ToolCallRequest):
                current = value
                continue
            if isinstance(value, Mapping):
                # Legacy on_tool_start returns the replacement arguments map.
                arguments = value.get("arguments") if "arguments" in value else value
                if isinstance(arguments, Mapping):
                    current = ToolCallRequest(
                        call_id=current.call_id,
                        tool_name=str(value.get("tool_name", current.tool_name)),
                        arguments=dict(arguments),
                        metadata=dict(current.metadata),
                    )
        return current, None

    async def _after_tool_hooks(
        self,
        request: ToolCallRequest,
        result: ToolCallResult,
        *,
        context: Any | None,
    ) -> ToolCallResult:
        """Apply optional result interceptors before effect completion."""

        current = result
        for hook in self._hooks:
            callback = getattr(hook, "after_tool", None)
            legacy_callback = getattr(hook, "on_tool_result", None)
            if not callable(callback) and not callable(legacy_callback):
                continue
            try:
                if callable(callback):
                    try:
                        value = callback(request, current, context=context)
                    except TypeError as exc:
                        if "context" not in str(exc):
                            raise
                        value = callback(request, current)
                else:
                    value = legacy_callback(
                        tool_call_id=request.call_id,
                        tool_name=request.tool_name,
                        success=current.status == "succeeded",
                        content=current.content,
                        error=current.error.message if current.error else None,
                    )
                if inspect.isawaitable(value):
                    value = await value
            except Exception as exc:
                return ToolCallResult(
                    call_id=request.call_id,
                    status="failed",
                    error=ErrorInfo(
                        code=ErrorCode.INTERNAL_ERROR,
                        category="hook",
                        message=f"tool after-hook failed: {type(exc).__name__}: {exc}",
                        details={"exception_type": type(exc).__name__},
                    ),
                )
            if value is None:
                continue
            if isinstance(value, ToolCallResult):
                current = value
            elif isinstance(value, Mapping):
                current = _tool_result_from_dict(request.call_id, dict(value))
            elif isinstance(value, tuple) and len(value) == 2:
                content, error = value
                current = ToolCallResult(
                    call_id=current.call_id,
                    status=current.status,
                    content=str(content or ""),
                    output=current.output,
                    error=(
                        ErrorInfo(
                            code=current.error.code if current.error else ErrorCode.INTERNAL_ERROR,
                            category=current.error.category if current.error else "tool",
                            message=str(error),
                            retryable=current.error.retryable if current.error else False,
                            details=current.error.details if current.error else {},
                        )
                        if error
                        else None
                    ),
                    permission_request=current.permission_request,
                    permission_decision=current.permission_decision,
                )
        return current

    async def _prepare_effect(
        self, request: ToolCallRequest
    ) -> tuple[str, str] | ToolCallResult | None:
        if self._effect_ledger is None:
            return None
        effect_id = request.metadata.get("effect_id")
        idempotency_key = request.metadata.get("idempotency_key")
        if not effect_id and not idempotency_key:
            return None
        if not effect_id or not idempotency_key:
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.INVALID_REQUEST,
                    category="effect",
                    message="effect_id and idempotency_key must be provided together",
                ),
            )
        request_digest = str(request.metadata.get("request_digest") or "")
        if not request_digest:
            request_digest = hashlib.sha256(
                json.dumps(
                    {"tool_name": request.tool_name, "arguments": dict(request.arguments)},
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode()
            ).hexdigest()
        try:
            if self._defer_effect_prepare:
                reconcile = getattr(self._effect_ledger, "reconcile", None)
                record = await reconcile(str(effect_id)) if callable(reconcile) else None
            else:
                record = await self._effect_ledger.prepare(
                    effect_id=str(effect_id),
                    run_id=str(request.metadata.get("run_id", "")),
                    idempotency_key=str(idempotency_key),
                    request_digest=request_digest,
                )
        except Exception as exc:
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    category="effect",
                    message=f"effect ledger prepare failed: {type(exc).__name__}: {exc}",
                    details={"exception_type": type(exc).__name__},
                ),
            )
        raw_status = getattr(record, "status", None) if record is not None else None
        if record is not None and raw_status is None:
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    category="effect",
                    message="effect ledger returned an invalid record",
                ),
            )
        status = getattr(raw_status, "value", raw_status)
        if status in {"succeeded", "failed"}:
            stored = getattr(record, "result", None)
            if isinstance(stored, dict):
                return _tool_result_from_dict(request.call_id, stored)
            return ToolCallResult(
                call_id=request.call_id,
                status="succeeded" if status == "succeeded" else "failed",
                content="effect already completed" if status == "succeeded" else "effect already failed",
            )
        if status in {"running", "unknown"}:
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.EFFECT_REQUIRES_RECONCILIATION,
                    category="effect",
                    message="effect is unresolved; reconcile before retrying",
                ),
            )
        if self._defer_effect_prepare:
            self._stage_effect(
                request.call_id,
                effect_id=str(effect_id),
                status="running",
                result=None,
                run_id=str(request.metadata.get("run_id", "")),
                idempotency_key=str(idempotency_key),
                request_digest=request_digest,
            )
            return str(effect_id), request_digest
        try:
            await self._effect_ledger.complete(
                effect_id=str(effect_id), status="running"
            )
        except Exception as exc:
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    category="effect",
                    message=f"effect ledger transition failed: {type(exc).__name__}: {exc}",
                    details={"exception_type": type(exc).__name__},
                ),
            )
        return str(effect_id), request_digest

    async def _execute_untracked(
        self,
        request: ToolCallRequest,
        *,
        context: Any | None = None,
        tool: Any | None = None,
        validated: bool = False,
        preflighted: bool = False,
        permission_decision: dict[str, Any] | None = None,
    ) -> ToolCallResult:
        tool = tool if tool is not None else self._resolve_tool(request.tool_name)
        invoke, request_style = _execution_callable(tool)
        if invoke is None:
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.TOOL_NOT_FOUND,
                    category="tool",
                    message=f"tool '{request.tool_name}' is not registered",
                ),
            )

        # Validation is deliberately a separate, synchronous gate.  It must
        # run before permission preflight so malformed model output cannot
        # trigger host prompts or any other policy lookup. ``execute`` already
        # performs this gate before effect preparation; keep the fallback here
        # for direct/internal callers of ``_execute_untracked``.
        if not validated:
            _, validation_error = self._resolve_and_validate(request, tool=tool)
            if validation_error is not None:
                return validation_error

        if not preflighted:
            preflight_result, permission_decision = await self._preflight(
                tool,
                request,
                context=context,
            )
            if preflight_result is not None:
                return preflight_result

        try:
            result = await self._invoke_tool(
                invoke,
                request,
                context=context,
                request_style=request_style,
            )
        except Exception as exc:
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    category="tool",
                    message=f"tool invocation failed: {type(exc).__name__}: {exc}",
                    details={"exception_type": type(exc).__name__},
                ),
            )
        if isinstance(result, ToolCallResult):
            if permission_decision is not None and result.permission_decision is None:
                return ToolCallResult(
                    call_id=result.call_id,
                    status=result.status,
                    content=result.content,
                    output=result.output,
                    error=result.error,
                    permission_request=result.permission_request,
                    permission_decision=permission_decision,
                )
            return result
        if not hasattr(result, "permission_request") or not hasattr(result, "success"):
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    category="tool",
                    message="tool must return ToolResult or ToolCallResult",
                ),
            )
        if result.permission_request is not None and not result.success:
            # Permission plugins need the host/session identity to correlate a
            # reverse-RPC decision with the active Run.  Keep the tool's
            # original request fields intact and add only runtime metadata;
            # callers that do not use it remain fully compatible.
            permission_payload = {
                **dict(result.permission_request),
                "session_id": request.metadata.get("session_id", ""),
                "run_id": request.metadata.get("run_id", ""),
                "call_id": request.call_id,
                "tool_name": request.tool_name,
            }
            if self._permission_gateway is not None:
                try:
                    decision = await self._permission_gateway.decide(
                        PermissionRequest(
                            scope=str(permission_payload.get("scope", "tool")),
                            requested_scope=str(
                                permission_payload.get("requested_scope", "")
                            ),
                            reason=str(permission_payload.get("reason", ""))
                            or "tool requested permission",
                            resource=str(permission_payload.get("path", "")),
                            metadata=permission_payload,
                        )
                    )
                except Exception as exc:
                    return ToolCallResult(
                        call_id=request.call_id,
                        status="failed",
                        output=result.raw_output,
                        permission_decision={
                            "granted": False,
                            "reason": "permission gateway failed",
                            "metadata": {"exception_type": type(exc).__name__},
                        },
                        error=ErrorInfo(
                            code=ErrorCode.PERMISSION_TIMEOUT,
                            category="permission",
                            message=f"permission gateway failed: {type(exc).__name__}: {exc}",
                            details={"exception_type": type(exc).__name__},
                        ),
                    )
                if isinstance(decision, PermissionDecision) and decision.granted:
                    approve = getattr(tool, "approve_permission_request", None)
                    if callable(approve):
                        approve(permission_payload)
                    result = await self._invoke_tool(
                        invoke,
                        request,
                        context=context,
                        request_style=request_style,
                    )
                    if result.success:
                        return ToolCallResult(
                            call_id=request.call_id,
                            status="succeeded",
                            content=result.content,
                            output=result.raw_output,
                            permission_decision=decision.to_dict()
                            if isinstance(decision, PermissionDecision)
                            else None,
                        )
                else:
                    denied_decision = (
                        decision.to_dict()
                        if isinstance(decision, PermissionDecision)
                        else {"granted": False, "reason": "permission gateway denied"}
                    )
                    return ToolCallResult(
                        call_id=request.call_id,
                        status="failed",
                        output=result.raw_output,
                        permission_decision=denied_decision,
                        error=ErrorInfo(
                            code=ErrorCode.PERMISSION_DENIED,
                            category="permission",
                            message=(
                                "operation requires approval and the permission "
                                "gateway denied the request"
                            ),
                        ),
                    )
            return ToolCallResult(
                call_id=request.call_id,
                status="permission_required",
                content=result.content,
                output=result.raw_output,
                # Preserve the tool's public permission payload when no
                # gateway is installed.  Runtime identity is only an
                # internal hint for an actual gateway and must not change
                # existing host-visible results.
                permission_request=(
                    result.permission_request
                    if self._permission_gateway is None
                    else permission_payload
                ),
                permission_decision=permission_decision,
            )
        if result.success:
            return ToolCallResult(
                call_id=request.call_id,
                status="succeeded",
                content=result.content,
                output=result.raw_output,
                permission_decision=permission_decision,
            )

        raw_code = (result.raw_output or {}).get("code")
        code = raw_code if isinstance(raw_code, str) else ErrorCode.TOOL_INVALID_ARGUMENTS
        return ToolCallResult(
            call_id=request.call_id,
            status="failed",
            content=result.content,
            output=result.raw_output,
            error=ErrorInfo(
                code=code,
                category="tool",
                message=result.error or "tool execution failed",
            ),
        )

    def _resolve_and_validate(
        self,
        request: ToolCallRequest,
        *,
        tool: Any | None = None,
    ) -> tuple[Any | None, ToolCallResult | None]:
        """Resolve a tool and run its side-effect-free validation gate."""

        resolved = tool if tool is not None else self._resolve_tool(request.tool_name)
        invoke, _ = _execution_callable(resolved)
        if invoke is None:
            return resolved, ToolCallResult(
                call_id=request.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.TOOL_NOT_FOUND,
                    category="tool",
                    message=f"tool '{request.tool_name}' is not registered",
                ),
            )
        validate = getattr(resolved, "validate", None)
        if not callable(validate):
            return resolved, None
        try:
            validation = validate(dict(request.arguments))
        except Exception as exc:
            return resolved, ToolCallResult(
                call_id=request.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    category="tool",
                    message=f"tool argument validation failed: {type(exc).__name__}: {exc}",
                    details={"exception_type": type(exc).__name__},
                ),
            )
        if validation is None:
            return resolved, None
        if isinstance(validation, ToolCallResult):
            return resolved, validation
        return resolved, _tool_result_to_call_result(request.call_id, validation)

    async def _preflight(
        self,
        tool: Any,
        request: ToolCallRequest,
        *,
        context: Any | None,
    ) -> tuple[ToolCallResult | None, dict[str, Any] | None]:
        """Resolve a tool's permission intent before entering its executor.

        ``Tool.preflight`` is an additive SPI. Legacy tools that do not
        override it return ``None`` and retain their historical invocation
        path; permission-aware plugins can therefore expose a request without
        performing filesystem, process, network, or other side effects first.
        """

        preflight = getattr(tool, "preflight", None)
        if not callable(preflight):
            return None, None
        try:
            with scoped_runtime_invocation(
                session_id=str(request.metadata.get("session_id", "")),
                run_id=str(request.metadata.get("run_id", "")),
                metadata=request.metadata,
            ):
                result = preflight(dict(request.arguments), context=context)
                if inspect.isawaitable(result):
                    result = await result
        except Exception as exc:
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    category="permission",
                    message=f"tool permission preflight failed: {type(exc).__name__}: {exc}",
                    details={"exception_type": type(exc).__name__},
                ),
            ), None
        if result is None:
            return None, None
        if isinstance(result, ToolCallResult):
            return (result, None) if result.status != "succeeded" else (None, None)
        if not hasattr(result, "permission_request") or not hasattr(result, "success"):
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    category="permission",
                    message="tool preflight must return ToolResult or ToolCallResult",
                ),
            ), None
        if result.success:
            return None, None
        if result.permission_request is None:
            raw_code = (result.raw_output or {}).get("code")
            code = raw_code if isinstance(raw_code, str) else ErrorCode.TOOL_INVALID_ARGUMENTS
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                content=result.content,
                output=result.raw_output,
                error=ErrorInfo(
                    code=code,
                    category="permission",
                    message=result.error or "tool permission preflight failed",
                ),
            ), None

        permission_payload = {
            **dict(result.permission_request),
            "session_id": request.metadata.get("session_id", ""),
            "run_id": request.metadata.get("run_id", ""),
            "call_id": request.call_id,
            "tool_name": request.tool_name,
        }
        permission_request = PermissionRequest(
            scope=str(permission_payload.get("scope", "tool")),
            requested_scope=str(permission_payload.get("requested_scope", "")),
            reason=str(permission_payload.get("reason", ""))
            or "tool requested permission",
            resource=str(permission_payload.get("path", "")),
            metadata=permission_payload,
        )
        await _publish_permission_requested(context, permission_request)
        if self._permission_gateway is None:
            return ToolCallResult(
                call_id=request.call_id,
                status="permission_required",
                content=result.content,
                output=result.raw_output,
                permission_request=permission_payload,
            ), None
        try:
            decision = await self._permission_gateway.decide(
                permission_request
            )
        except Exception as exc:
            failed_decision = PermissionDecision(
                granted=False,
                reason="permission gateway failed",
                metadata={"exception_type": type(exc).__name__},
            )
            await _publish_permission_resolved(context, failed_decision)
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                output=result.raw_output,
                permission_decision=failed_decision.to_dict(),
                error=ErrorInfo(
                    code=ErrorCode.PERMISSION_TIMEOUT,
                    category="permission",
                    message=f"permission gateway failed: {type(exc).__name__}: {exc}",
                    details={"exception_type": type(exc).__name__},
                ),
            ), None
        if not isinstance(decision, PermissionDecision) or not decision.granted:
            normalized_decision = (
                decision
                if isinstance(decision, PermissionDecision)
                else PermissionDecision(
                    granted=False,
                    reason="permission gateway returned an invalid decision",
                )
            )
            await _publish_permission_resolved(context, normalized_decision)
            return ToolCallResult(
                call_id=request.call_id,
                status="failed",
                output=result.raw_output,
                permission_decision=normalized_decision.to_dict(),
                error=ErrorInfo(
                    code=ErrorCode.PERMISSION_DENIED,
                    category="permission",
                    message=(
                        "operation requires approval and the permission gateway "
                        "denied the request"
                    ),
                ),
            ), None
        await _publish_permission_resolved(context, decision)
        approve = getattr(tool, "approve_permission_request", None)
        if callable(approve):
            approve({**permission_payload, **dict(decision.metadata)})
        return None, decision.to_dict()

    async def _invoke_tool(
        self,
        invoke: Any,
        request: ToolCallRequest,
        *,
        context: Any | None,
        request_style: bool = False,
    ) -> Any:
        """Invoke a tool while binding non-schema runtime identity.

        The fallback keeps compatibility with third-party ``invoke(arguments)``
        implementations that predate the optional context parameter.
        """

        with scoped_runtime_invocation(
            session_id=str(request.metadata.get("session_id", "")),
            run_id=str(request.metadata.get("run_id", "")),
            metadata=request.metadata,
        ):
            model_token = set_model_tool_context(
                model=getattr(context, "model", ""),
                max_output_tokens=getattr(context, "max_output_tokens", 0),
            )
            try:
                try:
                    if request_style:
                        result = invoke(request, context=context)
                    else:
                        result = invoke(dict(request.arguments), context=context)
                except TypeError as exc:
                    if "context" not in str(exc):
                        raise
                    result = (
                        invoke(request)
                        if request_style
                        else invoke(dict(request.arguments))
                    )
                if inspect.isawaitable(result):
                    result = await result
                return result
            finally:
                reset_model_tool_context(model_token)

    def _resolve_tool(self, name: str) -> Any | None:
        """Resolve by registry key first, then declared name/aliases.

        Registry keys are host-owned wiring identifiers.  Tool names are the
        model-facing protocol identifiers and therefore must remain stable
        even when a plugin uses a namespaced internal key.
        """

        value = self._registry.resolve(name, scope="run")
        if value is not None:
            return value
        normalized = str(name).strip()
        if not normalized:
            return None
        for registration in self._registry.registrations():
            candidate = self._registry.resolve(registration.key, scope="run")
            declared = (
                getattr(candidate, "name", None),
                *(getattr(candidate, "aliases", ()) or ()),
            )
            for item in declared:
                text = str(item)
                if (
                    text == normalized
                    or text.replace("_", "-") == normalized
                    or text.replace("-", "_") == normalized
                ):
                    return candidate
        return None


def _execution_callable(tool: Any | None) -> tuple[Any | None, bool]:
    """Return an executor and whether it consumes a full ToolCallRequest.

    ``ToolPlugin`` implementations expose ``invoke(arguments, context=...)``;
    the lower-level ``ToolExecutor`` port exposes ``execute(call, context=...)``.
    Prefer the former when both exist (the built-in ``Tool`` base class does),
    and keep the latter available for lightweight third-party plugins.
    """

    invoke = getattr(tool, "invoke", None)
    if callable(invoke):
        return invoke, False
    execute = getattr(tool, "execute", None)
    if callable(execute):
        return execute, True
    if callable(tool):
        return tool, True
    return None, False

def _tool_result_to_call_result(call_id: str, result: Any) -> ToolCallResult:
    """Normalize a legacy ``ToolResult`` returned by validation."""

    raw_output = getattr(result, "raw_output", None)
    raw_code = raw_output.get("code") if isinstance(raw_output, dict) else None
    code = raw_code if isinstance(raw_code, str) else ErrorCode.TOOL_INVALID_ARGUMENTS
    return ToolCallResult(
        call_id=call_id,
        status="succeeded" if bool(getattr(result, "success", False)) else "failed",
        content=str(getattr(result, "content", "") or ""),
        output=raw_output,
        error=(
            None
            if bool(getattr(result, "success", False))
            else ErrorInfo(
                code=code,
                category="tool",
                message=str(getattr(result, "error", None) or "tool validation failed"),
            )
        ),
        permission_request=getattr(result, "permission_request", None),
    )


def _tool_result_from_dict(call_id: str, data: dict[str, Any]) -> ToolCallResult:
    error_data = data.get("error")
    error = None
    if isinstance(error_data, dict):
        error = ErrorInfo(
            code=error_data.get("code", ErrorCode.INTERNAL_ERROR),
            category=str(error_data.get("category", "tool")),
            message=str(error_data.get("message", "tool execution failed")),
            retryable=bool(error_data.get("retryable", False)),
            details=dict(error_data.get("details", {})),
        )
    return ToolCallResult(
        call_id=call_id,
        status=str(data.get("status", "failed")),
        content=str(data.get("content", "") or ""),
        output=data.get("output"),
        error=error,
        permission_request=data.get("permission_request"),
        permission_decision=data.get("permission_decision"),
    )


__all__ = ["RegistryToolEngine"]
