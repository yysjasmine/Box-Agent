"""ACP protocol façade for the plugin-composed Agent Service.

The production ACP implementation historically owned a second orchestration
loop.  ``KernelACPAgent`` is the migration target: it only translates ACP
requests/notifications to ``KernelAgentService`` contracts and renders the
normalized ``AgentEvent`` stream.  It deliberately knows nothing about model,
tool, context, memory, or workflow implementations.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
import json
from collections.abc import Callable, Iterable, Mapping
from hashlib import sha256
import mimetypes
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from box_agent.api import (
    AgentEvent,
    ControlCommand,
    PermissionDecision,
    PermissionRequest,
    SessionOpenRequest,
)
from box_agent.adapters.acp_metadata import (
    sanitize_metadata,
    thinking_enabled_hint,
    user_decision_response,
)
from box_agent.adapters.extensions import HostExtensionContext
from box_agent.adapters.acp_projection import ACPEventProjection
from box_agent.adapters.service import request_from_payload
from box_agent.adapters.projections import PostRunProjectionRequest
from box_agent.context.action_hints import ActionHintStreamNormalizer
from box_agent.persistence import parse_session_continuation
from box_agent.tools.skillhub_contributor import skillhub_capabilities
from box_agent.tools.skillhub_install_tool import SKILLHUB_INSTALL_CAPABILITY_VERSION
from box_agent.tools.skillhub_search_tool import SKILLHUB_SEARCH_CAPABILITY_VERSION
from box_agent.workflows.routing import text_requests_native_plan
from box_agent.workflows.goal import (
    GoalToolStore,
    apply_goal_action,
    goal_action_from_metadata,
    goal_autopilot_terminal_message,
    goal_payload,
)


_NATIVE_PLAN_METADATA_KEYS = frozenset(
    {
        "force_plan_start",
        "forcePlanStart",
        "require_plan_approval",
        "requirePlanApproval",
        "pause_after_plan_write",
        "pauseAfterPlanWrite",
        "plan_approval",
        "planApproval",
        "plan_start_text",
        "planStartText",
    }
)

_HOST_IMMUTABLE_METADATA_KEYS = frozenset(
    {"cwd", "workspace_dir", "system_prompt", "_acp_turn_counter"}
)


def _canonical_workspace(value: Any) -> str:
    """Return the durable, absolute workspace identity used by one Session."""

    text = str(value or ".").strip() or "."
    return str(Path(text).expanduser().resolve(strict=False))


def _same_workspace(left: str, right: str) -> bool:
    return os.path.normcase(left) == os.path.normcase(right)


def _host_metadata(value: Any) -> dict[str, Any]:
    """Sanitize host metadata without allowing it to redefine Session identity."""

    metadata = sanitize_metadata(value or {})
    for key in _HOST_IMMUTABLE_METADATA_KEYS:
        metadata.pop(key, None)
    from box_agent.client_info import ClientInfo
    from box_agent.llm.binding import normalize_llm_binding

    binding = normalize_llm_binding(metadata)
    metadata.pop("llmBinding", None)
    if binding is not None:
        metadata["llm_binding"] = binding
    raw_client_info = metadata.pop(
        "clientInfo",
        metadata.get("client_info"),
    )
    client_info = ClientInfo.from_meta(raw_client_info)
    if client_info is None:
        metadata.pop("client_info", None)
    else:
        metadata["client_info"] = {
            "name": client_info.name,
            "platform": client_info.platform,
            "version": client_info.version,
            "os_version": client_info.os_version,
            "channel": client_info.channel,
            "device_id": client_info.device_id,
        }
    return metadata


def _turn_counter(value: Any) -> int:
    """Decode adapter-owned state without trusting arbitrary host JSON types."""

    if isinstance(value, bool):
        return 0
    try:
        counter = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(counter, 0)


def _native_plan_requested(
    prompt: str | list[dict[str, Any]], metadata: Mapping[str, Any] | None
) -> bool:
    """Recognize migrated Plan requests without hijacking rich workflows."""

    text = prompt if isinstance(prompt, str) else " ".join(
        str(block.get("text", ""))
        for block in prompt
        if isinstance(block, Mapping)
    )
    if text_requests_native_plan(text):
        return True
    if isinstance(metadata, Mapping):
        if any(key in metadata for key in _NATIVE_PLAN_METADATA_KEYS):
            return True
        workflow = str(
            metadata.get("workflow_id", metadata.get("workflowId", "")) or ""
        ).strip().lower()
        if workflow == "plan":
            return True
    return False


def _selected_workflow_id(metadata: Mapping[str, Any] | None) -> str:
    """Return the explicit workflow key supplied by the ACP host."""

    if not isinstance(metadata, Mapping):
        return ""
    candidates: list[Mapping[str, Any]] = [metadata]
    nested = metadata.get("options")
    if isinstance(nested, Mapping):
        candidates.append(nested)
    for values in candidates:
        for key in ("workflow_id", "workflowId", "workflow"):
            value = values.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    return ""


def _completion_gate_payload(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(metadata, Mapping):
        return None
    for key in ("completion_gate", "completionGate"):
        value = metadata.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Convert an ACP content block to the neutral message representation."""

    if isinstance(block, Mapping):
        return dict(block)
    model_dump = getattr(block, "model_dump", None)
    if callable(model_dump):
        try:
            return dict(model_dump(by_alias=True, exclude_none=True))
        except TypeError:
            return dict(model_dump())
    return {
        key: value
        for key in ("type", "text", "data", "mimeType", "uri")
        if (value := getattr(block, key, None)) is not None
    }


def _prompt_content(prompt: Any) -> str | list[dict[str, Any]]:
    blocks = list(prompt or ())
    def block_value(block: Any, key: str, default: Any = "") -> Any:
        if isinstance(block, Mapping):
            return block.get(key, default)
        return getattr(block, key, default)

    if all(str(block_value(block, "type")) in {"text", ""} for block in blocks):
        text = "".join(str(block_value(block, "text") or "") for block in blocks)
        if text:
            return text
    return [_block_to_dict(block) for block in blocks]


def _metadata_text(metadata: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _attachment_payloads(
    metadata: Mapping[str, Any],
    *,
    cwd: str,
) -> list[dict[str, Any]]:
    """Translate ACP's legacy path list to the typed Run attachment protocol."""

    raw_values: Any = ()
    for key in ("image_attachment_paths", "imageAttachmentPaths"):
        value = metadata.get(key)
        if isinstance(value, (list, tuple)):
            raw_values = value
            break
    root = Path(cwd or ".").expanduser()
    attachments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_values:
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.resolve(strict=False)
        uri = path.as_uri()
        if uri in seen:
            continue
        seen.add(uri)
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        attachments.append(
            {
                "attachment_id": "attachment-"
                + sha256(uri.encode("utf-8")).hexdigest()[:16],
                "kind": "image",
                "uri": uri,
                "mime_type": mime_type,
                "name": path.name,
                "metadata": {"source": "acp"},
            }
        )
        if len(attachments) >= 6:
            break
    return attachments


def _native_terminal_metadata_text(metadata: Mapping[str, Any] | None) -> str:
    """Render workflow stop facts that have no model content delta."""

    if not isinstance(metadata, Mapping):
        return ""
    autopilot = metadata.get("goalAutopilot")
    if isinstance(autopilot, Mapping) and autopilot.get("enabled"):
        continuations = int(autopilot.get("continuations", 0) or 0)
        no_progress_turns = int(
            autopilot.get("noProgressTurns", continuations) or 0
        )
        cause = str(autopilot.get("stopCause", "") or "")
        message = goal_autopilot_terminal_message(
            enabled=True,
            stop_cause=cause,
            continuations=continuations,
            no_progress_turns=no_progress_turns,
        )
        if message:
            return message
    completion_gate = metadata.get("completionGate")
    if isinstance(completion_gate, Mapping) and (
        completion_gate.get("budgetExhausted")
        or completion_gate.get("timeBudgetExhausted")
    ) and completion_gate.get("gaps"):
        continuations = int(completion_gate.get("continuations", 0) or 0)
        gaps = "; ".join(
            str(gap)
            for gap in completion_gate.get("gaps", ())
            if isinstance(gap, str) and gap.strip()
        )
        if gaps:
            return (
                "⚠️ Completion gate stopped after "
                f"{continuations} continuation(s) with delivery work remaining. "
                f"Gaps: {gaps}. Progress is recoverable from the durable checkpoint."
            )
    return ""


class ACPPermissionGateway:
    """One-shot ACP reverse-RPC permission gateway for native runs.

    The gateway is session-neutral: ``RegistryToolEngine`` includes the active
    ``session_id`` in ``PermissionRequest.metadata``.  This lets one service
    safely host multiple ACP sessions without putting ACP concerns in the
    Kernel or Tool Engine.
    """

    def __init__(self, conn: Any | None = None, *, timeout_seconds: float = 120.0) -> None:
        self._conn = conn
        self._timeout = timeout_seconds
        self._inflight_lock = asyncio.Lock()
        self._prompt_lock = asyncio.Lock()
        self._inflight: dict[tuple[str, str, str, str], asyncio.Task[PermissionDecision]] = {}
        self._waiters: dict[tuple[str, str, str, str], int] = {}

    def bind(self, conn: Any) -> None:
        """Bind the ACP connection once the stdio transport is constructed."""

        self._conn = conn

    async def decide(self, request: PermissionRequest) -> PermissionDecision:
        """Coalesce identical capability requests; safety prompts stay one-shot."""

        session_id = str(request.metadata.get("session_id", ""))
        if not session_id or self._conn is None:
            return PermissionDecision(False, "permission request has no session identity")
        if request.scope == "safety":
            async with self._prompt_lock:
                return await self._decide_once(request, session_id=session_id)
        key = (
            session_id,
            request.scope,
            request.requested_scope,
            self._normalized_resource(request.resource),
        )
        async with self._inflight_lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._decide_serialized(request, session_id))
                self._inflight[key] = task
                task.add_done_callback(
                    lambda completed, request_key=key: self._clear_inflight(
                        request_key, completed
                    )
                )
            self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            return await asyncio.shield(task)
        finally:
            async with self._inflight_lock:
                remaining = self._waiters.get(key, 1) - 1
                if remaining > 0:
                    self._waiters[key] = remaining
                else:
                    self._waiters.pop(key, None)
                    if not task.done():
                        task.cancel()
                    if self._inflight.get(key) is task:
                        self._inflight.pop(key, None)

    async def _decide_serialized(
        self,
        request: PermissionRequest,
        session_id: str,
    ) -> PermissionDecision:
        async with self._prompt_lock:
            return await self._decide_once(request, session_id=session_id)

    async def _decide_once(
        self,
        request: PermissionRequest,
        *,
        session_id: str,
    ) -> PermissionDecision:
        try:
            from acp.schema import (
                AllowedOutcome,
                PermissionOption,
                RequestPermissionRequest,
                ToolCall,
            )

            options = []
            if request.metadata.get("temporary_supported", True) is not False:
                options.append(
                    PermissionOption(
                        optionId="approve", name="仅本次允许", kind="allow_once"
                    )
                )
            if request.metadata.get("persistent_supported", True) is not False:
                options.append(
                    PermissionOption(
                        optionId="approve_session",
                        name=str(request.metadata.get("persistent_label") or "始终允许"),
                        kind="allow_always",
                    )
                )
            options.append(
                PermissionOption(optionId="reject", name="拒绝", kind="reject_once")
            )
            response = await asyncio.wait_for(
                self._conn.requestPermission(
                    RequestPermissionRequest(
                        sessionId=session_id,
                        toolCall=ToolCall(
                            toolCallId=f"perm-{request.metadata.get('call_id', 'unknown')}",
                            rawInput=request.to_dict(),
                        ),
                        options=options,
                    )
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            return PermissionDecision(False, "permission request timed out")
        except Exception as exc:
            return PermissionDecision(
                False,
                f"permission request failed: {type(exc).__name__}: {exc}",
            )
        outcome = getattr(response, "outcome", None)
        if isinstance(outcome, AllowedOutcome) and outcome.optionId in {
            "approve",
            "approve_session",
        }:
            return PermissionDecision(
                True,
                "approved by ACP host",
                metadata={
                    "grant_scope": (
                        "session" if outcome.optionId == "approve_session" else "prompt"
                    )
                },
            )
        return PermissionDecision(False, "denied by ACP host")

    @staticmethod
    def _normalized_resource(value: str) -> str:
        if not value:
            return ""
        try:
            return str(Path(value).expanduser().resolve())
        except (OSError, RuntimeError):
            return value

    def _clear_inflight(
        self,
        key: tuple[str, str, str, str],
        task: asyncio.Task[PermissionDecision],
    ) -> None:
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)


class KernelACPAgent:
    """Minimal ACP server object backed exclusively by ``AgentService``."""

    def __init__(
        self,
        conn: Any,
        service: Any,
        *,
        system_prompt: str = "",
        workspace_dir: str = "",
        goal_store: GoalToolStore | None = None,
        native_workflow_ids: Iterable[str] = (),
        workflow_selector: Callable[
            [str | list[dict[str, Any]], Mapping[str, Any]],
            Mapping[str, Any] | None,
        ]
        | None = None,
        extension_router: Any | None = None,
        projection_manager: Any | None = None,
        session_metadata_contributors: Iterable[Any] = (),
        provider_stale_seconds: float | None = None,
        truncation_continuation_enabled: bool | None = None,
        max_truncation_continuations: int | None = None,
        max_truncated_tool_call_retries: int | None = None,
    ) -> None:
        self._conn = conn
        self._service = service
        self._system_prompt = system_prompt
        self._workspace_dir = workspace_dir
        self._sessions: dict[str, dict[str, Any]] = {}
        self._active: dict[str, Any] = {}
        self._goal_store = goal_store
        self._workflow_selector = workflow_selector
        self._extension_router = extension_router
        self._projection_manager = projection_manager
        self._session_metadata_contributors = tuple(session_metadata_contributors)
        self._native_workflow_ids = frozenset(
            value.strip().lower()
            for value in native_workflow_ids
            if isinstance(value, str) and value.strip()
        )
        self._initialize_metadata: dict[str, Any] = {}
        self._provider_stale_seconds = provider_stale_seconds
        self._truncation_continuation_enabled = truncation_continuation_enabled
        self._max_truncation_continuations = max_truncation_continuations
        self._max_truncated_tool_call_retries = max_truncated_tool_call_retries

    async def initialize(self, params: Any) -> Any:
        from box_agent.acp.protocol import initialize_response

        self._initialize_metadata = _host_metadata(
            getattr(params, "field_meta", None)
        )
        # KernelAgentService reconstructs a Session from its durable stores.
        # Advertise that stable protocol fact from the same source used by the
        # lightweight ACP bootstrap.
        return initialize_response()

    async def newSession(self, params: Any) -> Any:
        from acp import NewSessionResponse

        cwd = _canonical_workspace(
            getattr(params, "cwd", "") or self._workspace_dir
        )
        metadata = {
            **self._initialize_metadata,
            **_host_metadata(getattr(params, "field_meta", None)),
        }
        open_request = SessionOpenRequest(
            metadata={**metadata, "workspace_dir": cwd}
        )
        defaults: dict[str, Any] = {}
        for contributor in self._session_metadata_contributors:
            contribute = getattr(contributor, "contribute", None)
            if not callable(contribute):
                raise TypeError(
                    "session metadata contributor must provide contribute(request)"
                )
            value = contribute(open_request)
            value = await value if inspect.isawaitable(value) else value
            if not isinstance(value, Mapping):
                raise TypeError("session metadata contributor must return a mapping")
            defaults.update(value)
        session = await self._service.open_session(
            SessionOpenRequest(metadata={**defaults, **open_request.metadata})
        )
        persisted_metadata = getattr(session, "metadata", {})
        session_metadata = sanitize_metadata(persisted_metadata)
        session_metadata.pop("cwd", None)
        session_metadata.pop("system_prompt", None)
        session_metadata["workspace_dir"] = cwd
        self._sessions[session.session_id] = {
            "cwd": cwd,
            "metadata": session_metadata,
            "turn_counter": _turn_counter(
                session_metadata.get("_acp_turn_counter", 0)
            ),
            "continuation_applied": False,
        }
        goal_request = goal_action_from_metadata(session_metadata)
        if self._goal_store is not None and goal_request is not None:
            apply_goal_action(
                self._goal_store,
                session.session_id,
                goal_request,
            )
        response_capabilities = {
            "session_continuation_versions": [1],
            "managed_mcp_config_versions": [1],
        }
        skillhub_search, skillhub_install = skillhub_capabilities(session_metadata)
        if skillhub_search:
            response_capabilities["skillhub_search_versions"] = [
                SKILLHUB_SEARCH_CAPABILITY_VERSION
            ]
        if skillhub_install:
            response_capabilities["skillhub_install_versions"] = [
                SKILLHUB_INSTALL_CAPABILITY_VERSION
            ]
        return NewSessionResponse(
            sessionId=session.session_id,
            field_meta={"capabilities": response_capabilities},
        )

    async def loadSession(self, params: Any) -> Any:
        """Reattach an ACP session to the Service's durable session state."""

        from acp import LoadSessionResponse

        session_id = str(getattr(params, "sessionId", "") or "")
        if not session_id:
            raise ValueError("session/load requires a sessionId")
        requested_workspace_value = getattr(params, "cwd", "") or ""
        requested_workspace = (
            _canonical_workspace(requested_workspace_value)
            if str(requested_workspace_value).strip()
            else ""
        )
        metadata = _host_metadata(getattr(params, "field_meta", None))
        session_request = SessionOpenRequest(
            session_id=session_id,
            metadata={},
        )
        load_session = getattr(self._service, "load_session", None)
        session = await (
            load_session(session_request)
            if callable(load_session)
            else self._service.open_session(session_request)
        )
        persisted_metadata = getattr(session, "metadata", {})
        durable_metadata = sanitize_metadata(persisted_metadata)
        persisted_workspace_value = durable_metadata.get(
            "workspace_dir", durable_metadata.get("cwd", "")
        )
        cwd = _canonical_workspace(
            persisted_workspace_value
            or requested_workspace
            or self._workspace_dir
        )
        if requested_workspace and not _same_workspace(requested_workspace, cwd):
            raise ValueError(
                "session workspace cannot be rebound: "
                f"persisted={cwd!r}, requested={requested_workspace!r}"
            )
        needs_workspace_migration = (
            durable_metadata.get("workspace_dir") != cwd
            or "cwd" in durable_metadata
            or "system_prompt" in durable_metadata
        )
        durable_metadata.pop("cwd", None)
        durable_metadata.pop("system_prompt", None)
        durable_metadata["workspace_dir"] = cwd
        if needs_workspace_migration:
            update_session = getattr(self._service, "update_session_metadata", None)
            if callable(update_session):
                session = await update_session(session_id, durable_metadata)
        session_metadata = {**durable_metadata, **metadata}
        self._sessions[session_id] = {
            "cwd": cwd,
            "metadata": session_metadata,
            "turn_counter": _turn_counter(
                session_metadata.get("_acp_turn_counter", 0)
            ),
            "continuation_applied": True,
        }
        goal_request = goal_action_from_metadata(session_metadata)
        if self._goal_store is not None and goal_request is not None:
            apply_goal_action(self._goal_store, session_id, goal_request)
        del session
        return LoadSessionResponse()

    async def prompt(self, params: Any) -> Any:
        from acp import PromptResponse

        session_id = str(params.sessionId)
        state = self._sessions.get(session_id)
        if state is None:
            return PromptResponse(stopReason="refusal")
        if self._projection_manager is not None:
            await self._projection_manager.begin_turn(session_id)
        raw_meta = self._prompt_metadata(session_id, getattr(params, "field_meta", None))
        prompt_content = _prompt_content(params.prompt)
        if not bool(state.get("continuation_applied")):
            continuation = parse_session_continuation(
                raw_meta.get(
                    "session_continuation",
                    raw_meta.get("sessionContinuation"),
                )
            )
            if continuation is not None:
                history = "\n\n".join(
                    f"[{message.role.upper()}]\n{message.content}"
                    for message in continuation.messages
                )
                continuation_envelope = (
                    "[HOST_SESSION_CONTINUATION]\n"
                    f"product_session_id={continuation.product_session_id}\n"
                    f"reason={continuation.reason}\n"
                    f"{history}\n"
                    "[/HOST_SESSION_CONTINUATION]\n\n"
                )
                if isinstance(prompt_content, str):
                    prompt_content = f"{continuation_envelope}{prompt_content}"
                else:
                    prompt_content = [
                        {"type": "text", "text": continuation_envelope},
                        *prompt_content,
                    ]
                state["continuation_applied"] = True
        decision = user_decision_response(raw_meta)
        if decision is not None:
            decision_envelope = (
                "[HOST_USER_DECISION_RESPONSE]\n"
                f"{json.dumps(decision, ensure_ascii=False)}\n"
                "[/HOST_USER_DECISION_RESPONSE]\n\n"
            )
            if isinstance(prompt_content, str):
                prompt_content = f"{decision_envelope}{prompt_content}"
            else:
                prompt_content = [
                    {"type": "text", "text": decision_envelope},
                    *prompt_content,
                ]
        state["turn_counter"] = _turn_counter(state.get("turn_counter", 0)) + 1
        turn_id = _metadata_text(raw_meta, "turn_id", "turnId") or (
            f"{session_id}-turn-{state['turn_counter']}"
        )
        correlation_session_id = _metadata_text(
            raw_meta, "correlation_session_id", "session_id", "sessionId"
        ) or session_id
        title = _metadata_text(
            raw_meta, "title", "session_title", "sessionTitle"
        )
        correlation_turn_id = _metadata_text(
            raw_meta, "correlation_turn_id", "turn_id", "turnId"
        ) or turn_id
        task_id = _metadata_text(raw_meta, "task_id", "taskId") or turn_id
        attachments = _attachment_payloads(raw_meta, cwd=str(state["cwd"]))
        run_metadata = dict(raw_meta)
        for key in (
            "image_attachment_paths",
            "imageAttachmentPaths",
            "turn_id",
            "turnId",
            "session_id",
            "sessionId",
            "session_title",
            "sessionTitle",
            "taskId",
            "cwd",
            "workspace_dir",
            "system_prompt",
            "_acp_turn_counter",
        ):
            run_metadata.pop(key, None)
        run_metadata.update(
            {
                "correlation_session_id": correlation_session_id,
                "correlation_turn_id": correlation_turn_id,
                "task_id": task_id,
                "title": title,
            }
        )
        session_metadata = dict(state.get("metadata", {}) or {})
        session_metadata["_acp_turn_counter"] = state["turn_counter"]
        if "llm_binding" in run_metadata:
            session_metadata["llm_binding"] = run_metadata["llm_binding"]
        if "client_info" in run_metadata:
            session_metadata["client_info"] = run_metadata["client_info"]
        if title:
            session_metadata["title"] = title
        update_session = getattr(self._service, "update_session_metadata", None)
        if callable(update_session):
            await update_session(session_id, session_metadata)
            state["metadata"] = session_metadata
        plan_requested = _native_plan_requested(prompt_content, raw_meta)
        options: dict[str, Any] = {}
        if self._provider_stale_seconds is not None:
            options["provider_stale_seconds"] = self._provider_stale_seconds
        if self._truncation_continuation_enabled is not None:
            options["truncation_continuation_enabled"] = (
                self._truncation_continuation_enabled
            )
        if self._max_truncation_continuations is not None:
            options["max_truncation_continuations"] = (
                self._max_truncation_continuations
            )
        if self._max_truncated_tool_call_retries is not None:
            options["max_truncated_tool_call_retries"] = (
                self._max_truncated_tool_call_retries
            )
        if isinstance(raw_meta, Mapping):
            nested_options = raw_meta.get("options", {})
            if isinstance(nested_options, Mapping):
                options.update(nested_options)
            for key in {
                "max_steps",
                "deadline_ms",
                "provider_stale_seconds",
                "truncation_continuation_enabled",
                "max_truncation_continuations",
                "max_truncated_tool_call_retries",
                "max_tool_calls",
                "max_parallel_tools",
                "thinking_enabled",
                "workflow_id",
                "workflow_options",
                "context_budget",
                "component_keys",
            }:
                if key in raw_meta:
                    options[key] = raw_meta[key]
            # ACP clients historically send ``deep_think`` (and some send
            # camelCase ``deepThink``) instead of the Kernel's neutral
            # ``thinking_enabled`` option.  Translate that host hint at the
            # adapter boundary while preserving an explicit Kernel option.
            if "thinking_enabled" not in options:
                thinking_hint = thinking_enabled_hint(raw_meta)
                if thinking_hint is not None:
                    options["thinking_enabled"] = thinking_hint
            workflow_options = dict(options.get("workflow_options", {}) or {})
            completion_gate = _completion_gate_payload(raw_meta)
            if completion_gate is not None:
                options.setdefault("workflow_id", "completion_gate")
                workflow_options["completion_gate"] = completion_gate
            for key in _NATIVE_PLAN_METADATA_KEYS:
                if key in raw_meta:
                    canonical = {
                        "forcePlanStart": "force_plan_start",
                        "requirePlanApproval": "require_plan_approval",
                        "pauseAfterPlanWrite": "pause_after_plan_write",
                        "planApproval": "plan_approval",
                        "planStartText": "plan_start_text",
                    }.get(key, key)
                    workflow_options[canonical] = raw_meta[key]
            if plan_requested:
                plan_text = (
                    prompt_content
                    if isinstance(prompt_content, str)
                    else " ".join(
                        str(block.get("text", ""))
                        for block in prompt_content
                        if isinstance(block, Mapping)
                    )
                )
                auto_approve = any(
                    bool(raw_meta.get(key))
                    for key in (
                        "auto_approve_plan",
                        "autoApprovePlan",
                        "skip_plan_approval",
                        "skipPlanApproval",
                    )
                )
                workflow_options.setdefault("force_plan_start", True)
                workflow_options.setdefault("plan_start_text", plan_text)
                if auto_approve:
                    workflow_options["pause_after_plan_write"] = False
                    workflow_options.setdefault(
                        "plan_approval",
                        {"approved": True, "decision": "approved"},
                    )
                else:
                    workflow_options.setdefault("pause_after_plan_write", True)
            if workflow_options:
                options["workflow_options"] = workflow_options
        selection = self._select_native_workflow(prompt_content, raw_meta)
        if selection is not None and not options.get("workflow_id"):
            workflow_id = str(selection.get("workflow_id", "") or "").strip().lower()
            if workflow_id in self._native_workflow_ids:
                options["workflow_id"] = workflow_id
                selected_options = selection.get("workflow_options", {})
                if isinstance(selected_options, Mapping):
                    workflow_options = dict(options.get("workflow_options", {}) or {})
                    for key, value in selected_options.items():
                        workflow_options.setdefault(str(key), value)
                    if workflow_options:
                        options["workflow_options"] = workflow_options
        payload = {
            "session_id": session_id,
            "turn_id": turn_id,
            "user_input": {"role": "user", "content": prompt_content},
            "attachments": attachments,
            "options": options,
            "metadata": {
                **run_metadata,
                "system_prompt": self._system_prompt,
                "workspace_dir": state["cwd"],
            },
        }
        request = request_from_payload(payload)
        handle = await self._service.start(request)
        self._active[session_id] = handle
        projection = ACPEventProjection(
            acp_session_id=session_id,
            correlation_session_id=correlation_session_id,
            task_id=task_id,
            turn_id=correlation_turn_id,
        )
        content_seen = False
        action_hints = ActionHintStreamNormalizer()
        last_content_event: AgentEvent | None = None
        try:
            async for event in handle.events():
                if event.type == "model.content.delta":
                    content_seen = True
                    last_content_event = event
                    for content in action_hints.push(
                        str(event.payload.get("content", ""))
                    ):
                        await self._render(
                            replace(
                                event,
                                payload={**dict(event.payload), "content": content},
                            )
                        )
                else:
                    await self._render(event)
                usage_payload = projection.observe(event)
                if usage_payload is not None:
                    await self._send_turn_usage(
                        session_id,
                        correlation_turn_id,
                        usage_payload,
                    )
            if last_content_event is not None:
                for content in action_hints.finish():
                    await self._render(
                        replace(
                            last_content_event,
                            payload={
                                **dict(last_content_event.payload),
                                "content": content,
                            },
                        )
                    )
            result = await handle.wait()
            await self._send_turn_usage(
                session_id,
                correlation_turn_id,
                projection.finalize(result.usage),
            )
        finally:
            self._active.pop(session_id, None)
        if self._projection_manager is not None:
            self._projection_manager.schedule(
                PostRunProjectionRequest(
                    session_id=session_id,
                    run_id=str(getattr(handle, "run_id", "") or request.request_id),
                    turn_id=correlation_turn_id,
                    user_input=request.user_input,
                    result=result,
                    metadata=dict(raw_meta),
                ),
                lambda value: self._send_host_projection(
                    session_id,
                    correlation_turn_id,
                    value,
                ),
            )
        if result.status == "cancelled":
            return PromptResponse(stopReason="cancelled")
        if result.status == "completed":
            terminal_metadata_text = _native_terminal_metadata_text(result.metadata)
            if terminal_metadata_text:
                try:
                    from acp import session_notification, text_block, update_agent_message

                    await self._conn.sessionUpdate(
                        session_notification(
                            session_id,
                            update_agent_message(text_block(terminal_metadata_text)),
                        )
                    )
                except Exception:
                    pass
            # Workflow boundaries can produce a terminal user-facing message
            # without asking the model for another content delta (for example
            # a Plan approval pause).  Emit that message at the ACP rendering
            # edge while avoiding duplicate output for streamed model text.
            if result.final_message and not content_seen:
                try:
                    from acp import session_notification, text_block, update_agent_message

                    await self._conn.sessionUpdate(
                        session_notification(
                            session_id,
                            update_agent_message(text_block(result.final_message)),
                        )
                    )
                except Exception:
                    # Rendering is best-effort; durable events remain the
                    # source of truth for a reconnecting host.
                    pass
            stop_reason = {
                "max_steps": "max_turn_requests",
                "max_tokens": "max_tokens",
                "checkpoint_paused": "end_turn",
            }.get(result.stop_reason, "end_turn")
            response_metadata = dict(result.metadata)
            if result.usage is not None:
                response_metadata["usage"] = {
                    "totalTokens": result.usage.total_tokens,
                    "sessionId": correlation_session_id,
                    "session_id": correlation_session_id,
                    "taskId": task_id,
                    "task_id": task_id,
                    "turnId": turn_id,
                    "turn_id": turn_id,
                }
            return PromptResponse(
                stopReason=stop_reason,
                field_meta=response_metadata,
            )
        return PromptResponse(stopReason="refusal")

    async def cancel(self, params: Any) -> None:
        session_id = str(params.sessionId)
        handle = self._active.get(session_id)
        if handle is not None:
            await handle.cancel(reason="ACP cancel notification")

    async def extMethod(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Route native controls and typed host-extension plugins."""

        session_id = str(params.get("sessionId", "") or "")
        if method in {"inject", "cancel_inject"}:
            if not session_id or session_id not in self._sessions:
                return {"error": "session_not_found"}
            handle = self._active.get(session_id)
            if handle is None:
                return {"error": "no_active_turn"}
            injection_id = str(params.get("injectionId", "") or "")
            if not injection_id:
                injection_id = uuid4().hex if method == "inject" else ""
            if not injection_id:
                return {"error": "empty_injection_id"}
            operation_id_value = params.get("operationId")
            if operation_id_value is None or operation_id_value == "":
                operation_id = uuid4().hex
            elif not isinstance(operation_id_value, str) or not operation_id_value.strip():
                return {"error": "invalid_operation_id"}
            else:
                operation_id = operation_id_value.strip()
            if method == "inject":
                text = params.get("text", "")
                if not isinstance(text, str) or not text.strip():
                    return {"error": "empty_text"}
                kind = "run.inject"
                payload = {"text": text, "injection_id": injection_id}
            else:
                kind = "run.cancel_inject"
                payload = {"injection_id": injection_id}
            ack = await handle.send(
                ControlCommand(
                    # ``operationId`` identifies this idempotent state
                    # transition. ``injectionId`` identifies the mutable
                    # context item, so cancel followed by reinject must use
                    # two command identities while targeting one item.
                    command_id=f"acp:{operation_id}",
                    session_id=session_id,
                    run_id=handle.run_id,
                    kind=kind,
                    payload=payload,
                    source="acp.extension",
                )
            )
            if not ack.accepted:
                error = ack.error.to_dict() if ack.error is not None else None
                return {"error": error or ack.status}
            return {
                "ok": True,
                "injectionId": injection_id,
                "operationId": operation_id,
            }
        if method == "goal" and self._goal_store is not None:
            if not session_id or session_id not in self._sessions:
                return {"error": "session_not_found"}
            previous = self._goal_store.get(session_id)
            result = apply_goal_action(self._goal_store, session_id, params)
            if "error" not in result:
                metadata = dict(
                    self._sessions[session_id].get("metadata", {}) or {}
                )
                goal = result.get("goal")
                if goal is None:
                    metadata.pop("goal", None)
                else:
                    metadata["goal"] = goal
                update_session = getattr(
                    self._service,
                    "update_session_metadata",
                    None,
                )
                if callable(update_session):
                    try:
                        await update_session(session_id, metadata)
                    except Exception:
                        self._goal_store.restore(session_id, goal_payload(previous))
                        return {"error": "session_persistence_failed"}
                self._sessions[session_id]["metadata"] = metadata
            return result
        if self._extension_router is not None:
            metadata = self._sessions.get(session_id, {}).get("metadata", {})
            try:
                routed = await self._extension_router.handle(
                    method,
                    params,
                    context=HostExtensionContext(
                        session_id=session_id,
                        session_metadata=(
                            dict(metadata) if isinstance(metadata, Mapping) else {}
                        ),
                        service=self._service,
                        active_handle=self._active.get(session_id),
                        connection=self._conn,
                    ),
                )
            except Exception as exc:
                return {
                    "error": {
                        "code": "extension_failed",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                }
            if routed is not None:
                return routed
        return {"error": "unsupported_extension"}

    def _select_native_workflow(
        self,
        prompt: str | list[dict[str, Any]],
        metadata: Mapping[str, Any] | None,
    ) -> Mapping[str, Any] | None:
        """Invoke an optional host/plugin-owned prompt-to-workflow selector."""

        selector = self._workflow_selector
        if selector is None:
            return None
        try:
            value = selector(prompt, metadata or {})
        except Exception:
            # Routing extensions fail open to the normal deterministic host
            # predicates; they never make a valid ACP prompt unusable.
            return None
        return value if isinstance(value, Mapping) else None

    def _prompt_metadata(
        self,
        session_id: str,
        prompt_metadata: Any,
    ) -> dict[str, Any]:
        """Merge session defaults with per-turn ACP metadata.

        ACP hosts commonly select a workflow at ``session/new`` and omit the
        same field on every prompt.  Keep that default in the adapter-owned
        session record while allowing a turn to override it explicitly.
        """

        state = self._sessions.get(session_id, {})
        session_metadata = state.get("metadata", {}) if isinstance(state, Mapping) else {}
        merged = dict(session_metadata) if isinstance(session_metadata, Mapping) else {}
        if isinstance(prompt_metadata, Mapping):
            turn_metadata = _host_metadata(prompt_metadata)
            # These fields determine the Session's Tool graph or security
            # boundary.  Accepting them on a later prompt would make Context
            # describe a different policy than the cached Tool instances
            # enforce.  Hosts must create a new Session to change them.
            for key in (
                "permission_mode",
                "permissionMode",
                "filesystem_policy",
                "filesystemPolicy",
                "workspace_layout",
                "workspaceLayout",
                "artifact_mode",
                "artifactMode",
                "env_context",
                "envContext",
            ):
                turn_metadata.pop(key, None)
            merged.update(turn_metadata)
        return sanitize_metadata(merged)


    async def _render(self, event: AgentEvent) -> None:
        from acp import (
            session_notification,
            start_tool_call,
            text_block,
            tool_content,
            update_agent_message,
            update_agent_thought,
            update_tool_call,
        )

        update: Any | None = None
        if event.type == "model.content.delta":
            update = update_agent_message(text_block(str(event.payload.get("content", ""))))
        elif event.type == "model.thinking.delta":
            update = update_agent_thought(text_block(str(event.payload.get("thinking", ""))))
        elif event.type == "tool.call.requested":
            call_id = str(event.payload.get("call_id", ""))
            name = str(event.payload.get("tool_name", "tool"))
            update = start_tool_call(
                call_id,
                f"🔧 {name}",
                kind="execute",
                raw_input=event.payload.get("arguments", {}),
            )
        elif event.type == "tool.call.completed":
            call_id = str(event.payload.get("call_id", ""))
            ok = event.payload.get("status") == "succeeded"
            text = str(event.payload.get("content", "") or "")
            error = event.payload.get("error")
            if not ok and isinstance(error, Mapping):
                text = str(error.get("message", "tool failed"))
            update = update_tool_call(
                call_id,
                status="completed" if ok else "failed",
                content=[tool_content(text_block(text))] if text else None,
                raw_output=(
                    event.payload.get("output")
                    if event.payload.get("output") is not None
                    else dict(event.payload)
                ),
            )
        elif event.type == "tool.progress" and event.payload.get("kind") == "subagent.event":
            data = event.payload.get("data", {})
            data = dict(data) if isinstance(data, Mapping) else {}
            update = update_tool_call(
                str(event.payload.get("call_id", "")),
                raw_output={
                    "type": "sub_agent_progress",
                    "parentToolCallId": str(event.payload.get("call_id", "")),
                    "subAgentId": str(data.get("sub_agent_id", "")),
                    "title": str(data.get("title", "")),
                    "taskPreview": str(data.get("task_preview", "")),
                    "event": (
                        dict(data.get("event", {}))
                        if isinstance(data.get("event"), Mapping)
                        else {}
                    ),
                },
            )
        elif event.type == "artifact.created":
            payload = dict(event.payload)
            uri = str(payload.get("uri", payload.get("abs_path", "")) or "")
            filename = str(payload.get("filename", "") or "")
            if not filename and uri:
                filename = Path(uri.removeprefix("file:///")).name
            update = update_tool_call(
                str(payload.get("call_id", f"artifact-{event.sequence}")),
                status="completed",
                raw_output={
                    "type": "artifact",
                    **payload,
                    "filename": filename,
                },
            )
        elif event.type == "workflow.plan.snapshot":
            payload = dict(event.payload)
            plan = payload.get("plan")
            title = (
                str(plan.get("title") or "执行方案")
                if isinstance(plan, Mapping)
                else "执行方案"
            )
            if title == "正在制定执行方案":
                title = "Preparing execution plan"
            call_id = f"plan-snapshot-{event.sequence}"
            try:
                await self._conn.sessionUpdate(
                    session_notification(
                        event.session_id,
                        start_tool_call(
                            call_id,
                            title,
                            kind="execute",
                            raw_input={"action": payload.get("action")},
                        ),
                    )
                )
            except Exception:
                return
            update = update_tool_call(
                call_id,
                status="completed",
                content=[tool_content(text_block(title))],
                raw_output=payload,
            )
        elif event.type == "context.assembled":
            projections = event.payload.get("host_projections", ())
            if isinstance(projections, (list, tuple)):
                for index, projection in enumerate(projections[:16]):
                    if not isinstance(projection, Mapping):
                        continue
                    projection_id = str(
                        projection.get("projection_id")
                        or f"projection-{index}"
                    )
                    payload = projection.get("payload")
                    if (
                        projection.get("surface") != "raw_output"
                        or not isinstance(payload, Mapping)
                    ):
                        continue
                    try:
                        await self._conn.sessionUpdate(
                            session_notification(
                                event.session_id,
                                update_tool_call(
                                    f"context-projection-{event.sequence}-{projection_id}",
                                    raw_output=dict(payload),
                                ),
                            )
                        )
                    except Exception:
                        continue
            update = update_tool_call(
                f"agent-{event.sequence}",
                raw_output={"type": event.type, **dict(event.payload)},
            )
        elif event.type in {"run.progress", "memory.recalled", "memory.error"}:
            update = update_tool_call(
                f"agent-{event.sequence}",
                raw_output={"type": event.type, **dict(event.payload)},
            )
        if update is not None:
            try:
                await self._conn.sessionUpdate(
                    session_notification(event.session_id, update)
                )
            except Exception:
                # A disconnected renderer must not turn a durable Agent Run
                # into a business failure.  The service remains resumable and
                # the host can reconnect using its event sequence.
                return

    async def _send_turn_usage(
        self,
        session_id: str,
        turn_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Render one replayable event projection at the ACP boundary."""

        from acp import session_notification, update_tool_call

        try:
            await self._conn.sessionUpdate(
                session_notification(
                    session_id,
                    update_tool_call(
                        f"turn-usage-{turn_id}",
                        raw_output=dict(payload),
                    ),
                )
            )
        except Exception:
            # Projection failure cannot change the durable Run outcome. A
            # reconnect can rebuild the same snapshot from AgentEvent facts.
            return

    async def _send_host_projection(
        self,
        session_id: str,
        turn_id: str,
        projection: Any,
    ) -> None:
        """Render one typed post-run projection without knowing its policy."""

        from acp import session_notification, update_tool_call

        projection_id = str(getattr(projection, "projection_id", "") or "")
        payload = getattr(projection, "payload", None)
        surface = str(getattr(projection, "surface", "") or "")
        if not projection_id or surface != "raw_output" or not isinstance(payload, Mapping):
            return
        try:
            await self._conn.sessionUpdate(
                session_notification(
                    session_id,
                    update_tool_call(
                        f"post-run-projection-{turn_id}-{projection_id}",
                        raw_output=dict(payload),
                    ),
                )
            )
        except Exception:
            return


__all__ = ["ACPPermissionGateway", "KernelACPAgent"]
