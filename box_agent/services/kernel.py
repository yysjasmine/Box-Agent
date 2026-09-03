"""Service host for the plugin-composed Agent Loop Kernel.

``KernelAgentService`` is the runtime boundary used by ACP, CLI, SDK, and
third-party hosts.  It owns sessions, run identity, ordered event delivery,
control commands, and durable replay; the actual orchestration remains in an
``AgentLoop`` implementation such as :class:`box_agent.kernel.AgentLoopKernel`.

Compatibility facades are deliberately not imported here. This keeps the
stable SPI path independently testable while adapters preserve their wire
protocols.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..api import (
    Artifact,
    AttachmentRef,
    AgentEvent,
    AgentRunHandle,
    CommandAck,
    ControlCommand,
    ControlHandler,
    ErrorCode,
    ErrorInfo,
    Message,
    RunRequest,
    RunOptions,
    RunResult,
    RunStatus,
    SessionInfo,
    SessionOpenRequest,
    Usage,
)
from ..persistence import (
    EffectRecord,
    EffectStatus,
    OutboxRecord,
    PersistenceConflictError,
    RunCheckpoint,
)


_TERMINAL_EVENTS = {"run.completed", "run.cancelled", "run.failed"}
_CHECKPOINT_EVENTS = {
    "run.started",
    "memory.recalled",
    "memory.write.requested",
    "memory.written",
    "memory.flushed",
    "context.assembled",
    "context.compacted",
    "model.requested",
    "model.response.completed",
    "model.usage",
    "tool.call.requested",
    "permission.requested",
    "permission.resolved",
    "tool.call.completed",
    "artifact.created",
    "workflow.checkpoint",
    "workflow.plan.snapshot",
    "workflow.continuation.requested",
    "workflow.control.applied",
    "step.completed",
    *_TERMINAL_EVENTS,
}


def _workspace_identity(metadata: Mapping[str, Any]) -> str | None:
    value = metadata.get("workspace_dir")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PersistenceConflictError("session workspace_dir must be a non-empty string")
    resolved = Path(value).expanduser().resolve(strict=False)
    return os.path.normcase(str(resolved))


def _validate_session_workspace(
    session: SessionInfo,
    requested_metadata: Mapping[str, Any],
) -> None:
    """Keep a durable Session bound to its original filesystem authority."""
    existing = _workspace_identity(session.metadata)
    requested = _workspace_identity(requested_metadata)
    if existing is not None and requested is not None and existing != requested:
        raise PersistenceConflictError(
            f"session {session.session_id!r} is bound to a different workspace"
        )


def _error(
    code: ErrorCode | str,
    message: str,
    *,
    category: str = "runtime",
    details: Mapping[str, Any] | None = None,
    retryable: bool = False,
) -> ErrorInfo:
    return ErrorInfo(
        code=code,
        category=category,
        message=message,
        retryable=retryable,
        details=dict(details or {}),
    )


def _request_from_dict(data: Mapping[str, Any]) -> RunRequest:
    """Decode the durable, data-only request without loading legacy code."""

    user = data.get("user_input") or {}
    raw_content = user.get("content", "")
    content = (
        tuple(dict(block) for block in raw_content)
        if isinstance(raw_content, list)
        and all(isinstance(block, Mapping) for block in raw_content)
        else str(raw_content)
    )
    options_data = data.get("options") or {}
    raw_attachments = data.get("attachments") or ()
    option_names = {
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
    }
    return RunRequest(
        request_id=str(data["request_id"]),
        session_id=str(data["session_id"]),
        turn_id=str(data["turn_id"]),
        user_input=Message(
            role=str(user.get("role", "user")),
            content=content,
            name=user.get("name"),
            metadata=user.get("metadata", {}),
        ),
        attachments=tuple(
            AttachmentRef(
                attachment_id=str(item.get("attachment_id", "")),
                kind=str(item.get("kind", "")),
                uri=str(item.get("uri", "")),
                mime_type=str(
                    item.get("mime_type", "application/octet-stream")
                ),
                name=str(item.get("name", "")),
                metadata=item.get("metadata", {}),
            )
            for item in raw_attachments
            if isinstance(item, Mapping)
        ),
        options=RunOptions(
            **{name: options_data[name] for name in option_names if name in options_data}
        ),
        metadata=data.get("metadata", {}),
    )


def _usage_from_events(events: list[AgentEvent]) -> Usage | None:
    values = [
        event.payload.get("usage")
        for event in events
        if event.type == "model.response.completed"
        and isinstance(event.payload.get("usage"), Mapping)
    ]
    if not values:
        values = [
            event.payload
            for event in events
            if event.type == "model.usage"
        ]
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    found = False
    for value in values:
        if not isinstance(value, Mapping):
            continue
        found = True
        input_tokens += int(value.get("input_tokens", 0))
        output_tokens += int(value.get("output_tokens", 0))
        total_tokens += int(value.get("total_tokens", 0))
    if not found:
        return None
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _result_from_event(events: list[AgentEvent], event: AgentEvent) -> RunResult:
    status = {
        "run.completed": "completed",
        "run.cancelled": "cancelled",
        "run.failed": "failed",
    }[event.type]
    payload = event.payload
    error = None
    error_data = payload.get("error")
    if status == "failed":
        if isinstance(error_data, Mapping):
            error = ErrorInfo(
                code=error_data.get("code", ErrorCode.INTERNAL_ERROR),
                category=str(error_data.get("category", "loop")),
                message=str(error_data.get("message", "run failed")),
                retryable=bool(error_data.get("retryable", False)),
                details=error_data.get("details", {}),
            )
        else:
            error = _error(
                ErrorCode.INTERNAL_ERROR,
                str(payload.get("message", "run failed")),
                category="loop",
            )
    return RunResult(
        status=status,
        stop_reason=str(payload.get("stop_reason", status)),
        final_message=str(payload.get("final_content", payload.get("content", ""))),
        usage=_usage_from_events(events),
        artifacts=_artifacts_from_events(events),
        error=error,
        metadata=(
            dict(payload.get("metadata", {}))
            if isinstance(payload.get("metadata"), Mapping)
            else {}
        ),
    )


def _digest(command: ControlCommand) -> str:
    value = {
        "kind": command.kind,
        "payload": dict(command.payload),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _event_hash(event: AgentEvent) -> str:
    """Return a stable digest for the event represented by a checkpoint."""

    return hashlib.sha256(
        json.dumps(
            event.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _ack_from_dict(command_id: str, data: Mapping[str, Any]) -> CommandAck:
    error_data = data.get("error")
    error = None
    if isinstance(error_data, Mapping):
        error = ErrorInfo(
            code=error_data.get("code", ErrorCode.INTERNAL_ERROR),
            category=str(error_data.get("category", "control")),
            message=str(error_data.get("message", "control rejected")),
            retryable=bool(error_data.get("retryable", False)),
            details=dict(error_data.get("details", {})),
        )
    return CommandAck(
        command_id=command_id,
        accepted=bool(data.get("accepted", False)),
        status=str(data.get("status", "rejected")),
        error=error,
    )


def _request_fingerprint(request: RunRequest | Mapping[str, Any]) -> str:
    value = request.to_dict() if isinstance(request, RunRequest) else dict(request)
    metadata = dict(value.get("metadata", {}))
    metadata = {
        key: item
        for key, item in metadata.items()
        if key not in {"run_id", "plugin_lock"} and not str(key).startswith("_")
    }
    value["metadata"] = metadata
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _artifacts_from_events(events: list[AgentEvent]) -> tuple[Artifact, ...]:
    artifacts: list[Artifact] = []
    for event in events:
        if event.type != "artifact.created":
            continue
        payload = event.payload
        uri = str(payload.get("uri", payload.get("abs_path", "")) or "")
        if not uri:
            continue
        artifacts.append(
            Artifact(
                artifact_id=str(payload.get("artifact_id", payload.get("filename", uri))),
                kind=str(payload.get("kind", "file") or "file"),
                uri=uri,
                mime=str(payload.get("mime", "application/octet-stream")),
                size=int(payload.get("size", -1) or -1),
                sha256=str(payload.get("sha256", "") or ""),
            )
        )
    return tuple(artifacts)


def _call_factory(factory: Callable[..., Any], request: RunRequest, bundle: Any) -> Any:
    """Call a kernel factory while supporting zero/one/two argument plugins."""

    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory(request, bundle)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    if has_varargs or len(positional) >= 2:
        return factory(request, bundle)
    if len(positional) == 1:
        return factory(request)
    return factory()


async def _invoke_control_handler(
    handler: ControlHandler, command: ControlCommand
) -> CommandAck:
    """Normalize a third-party control extension into the stable ACK contract."""

    try:
        target = getattr(handler, "handle", handler)
        if not callable(target):
            raise TypeError("control handler must be callable or expose handle(command)")
        value = target(command)
        if inspect.isawaitable(value):
            value = await value
    except Exception as exc:
        return CommandAck(
            command_id=command.command_id,
            accepted=False,
            status="rejected",
            error=_error(
                ErrorCode.INTERNAL_ERROR,
                f"control handler failed: {type(exc).__name__}: {exc}",
                category="control",
                details={"exception_type": type(exc).__name__},
            ),
        )

    if isinstance(value, CommandAck):
        if value.command_id == command.command_id:
            return value
        return CommandAck(
            command_id=command.command_id,
            accepted=False,
            status="rejected",
            error=_error(
                ErrorCode.INTERNAL_ERROR,
                "control handler returned an ACK for a different command_id",
                category="control",
            ),
        )
    if isinstance(value, Mapping):
        error_data = value.get("error")
        error = None
        if isinstance(error_data, Mapping):
            error = ErrorInfo(
                code=error_data.get("code", ErrorCode.INTERNAL_ERROR),
                category=str(error_data.get("category", "control")),
                message=str(error_data.get("message", "control rejected")),
                retryable=bool(error_data.get("retryable", False)),
                details=dict(error_data.get("details", {})),
            )
        accepted = bool(value.get("accepted", False))
        return CommandAck(
            command_id=command.command_id,
            accepted=accepted,
            status=str(value.get("status", "accepted" if accepted else "rejected")),
            error=error,
        )
    if value is None or value is True:
        return CommandAck(command_id=command.command_id, accepted=True, status="accepted")
    if value is False:
        return CommandAck(
            command_id=command.command_id,
            accepted=False,
            status="rejected",
            error=_error(ErrorCode.UNSUPPORTED_COMMAND, "control handler declined command"),
        )
    return CommandAck(
        command_id=command.command_id,
        accepted=False,
        status="rejected",
        error=_error(
            ErrorCode.INTERNAL_ERROR,
            "control handler must return CommandAck, mapping, bool, or None",
            category="control",
        ),
    )


def _control_handler_from_registry(host: Any) -> ControlHandler | None:
    """Build a command router from the optional ``control.routes`` registry."""

    registry = getattr(host, "registries", {}).get("control.routes")
    if registry is None:
        return None

    async def dispatch(command: ControlCommand) -> Any:
        route = registry.resolve(command.kind, scope="run")
        if route is None:
            return False
        target = getattr(route, "handle", route)
        if not callable(target):
            return False
        value = target(command)
        return await value if inspect.isawaitable(value) else value

    return dispatch


def _validate_recovery_bundle(run_id: str, bundle: Any) -> None:
    """Reject corrupt or cross-run facts before a kernel is resumed."""

    events = tuple(getattr(bundle, "events", ()) or ())
    expected_sequence = 1
    session_id: str | None = None
    for event in events:
        if not isinstance(event, AgentEvent):
            raise PersistenceConflictError("recovery event is not an AgentEvent")
        if event.run_id != run_id:
            raise PersistenceConflictError(
                f"recovery event {event.event_id!r} belongs to another run"
            )
        if session_id is None:
            session_id = event.session_id
        elif event.session_id != session_id:
            raise PersistenceConflictError("recovery events belong to multiple sessions")
        if event.sequence != expected_sequence:
            raise PersistenceConflictError(
                f"run {run_id!r} has a sequence gap at {expected_sequence}"
            )
        expected_sequence += 1
    checkpoint = getattr(bundle, "checkpoint", None)
    if checkpoint is not None:
        if checkpoint.run_id != run_id:
            raise PersistenceConflictError("recovery checkpoint belongs to another run")
        if session_id is not None and checkpoint.session_id != session_id:
            raise PersistenceConflictError("recovery checkpoint belongs to another session")
        if checkpoint.schema_version != "1":
            raise PersistenceConflictError(
                f"unsupported recovery checkpoint schema {checkpoint.schema_version!r}"
            )
        if not isinstance(checkpoint.state, Mapping):
            raise PersistenceConflictError("recovery checkpoint state must be a mapping")
        if not isinstance(checkpoint.plugin_lock, Mapping):
            raise PersistenceConflictError("recovery checkpoint plugin_lock must be a mapping")
        for plugin_id, version in checkpoint.plugin_lock.items():
            if not isinstance(plugin_id, str) or not plugin_id.strip() or not isinstance(version, str):
                raise PersistenceConflictError("recovery checkpoint plugin_lock is malformed")
        if not isinstance(checkpoint.plugin_snapshot, Mapping):
            raise PersistenceConflictError("recovery plugin_snapshot must be a mapping")
        for plugin_id, record in checkpoint.plugin_snapshot.items():
            if not isinstance(plugin_id, str) or not plugin_id.strip() or not isinstance(record, Mapping):
                raise PersistenceConflictError("recovery plugin_snapshot is malformed")
            version = record.get("version")
            schema = record.get("schema_version")
            if not isinstance(version, str) or not version.strip() or not isinstance(schema, str) or not schema.strip():
                raise PersistenceConflictError(
                    f"recovery plugin snapshot '{plugin_id}' is missing version/schema"
                )
            locked_version = checkpoint.plugin_lock.get(plugin_id)
            if locked_version is not None and locked_version != version:
                raise PersistenceConflictError(
                    f"recovery plugin snapshot '{plugin_id}' does not match plugin_lock"
                )
            if "state" not in record:
                raise PersistenceConflictError(
                    f"recovery plugin snapshot '{plugin_id}' is missing state"
                )
        context_manifest = checkpoint.state.get("context_manifest")
        context_digest = checkpoint.state.get("context_digest")
        if context_manifest is not None:
            if not isinstance(context_manifest, Mapping):
                raise PersistenceConflictError("recovery context_manifest must be a mapping")
            manifest_hash = context_manifest.get("content_hash")
            if not isinstance(manifest_hash, str) or not manifest_hash.strip():
                raise PersistenceConflictError("recovery context_manifest has no content_hash")
            if context_digest is not None and context_digest != manifest_hash:
                raise PersistenceConflictError("recovery context digest does not match manifest")
        elif context_digest is not None and (
            not isinstance(context_digest, str) or not context_digest.strip()
        ):
            raise PersistenceConflictError("recovery context_digest is malformed")
        if checkpoint.sequence > len(events):
            raise PersistenceConflictError("checkpoint is ahead of the durable event log")
        # The checkpoint is a claim about a specific event boundary.  Verify
        # that claim before rebuilding a kernel; otherwise a torn/corrupted
        # store could resume from a state that no longer matches its event
        # stream.  Sequence zero is reserved for an empty initial snapshot.
        if checkpoint.sequence > 0:
            checkpoint_event = events[checkpoint.sequence - 1]
            if _event_hash(checkpoint_event) != checkpoint.event_hash:
                raise PersistenceConflictError(
                    "recovery checkpoint event hash does not match the durable event"
                )

    for effect in tuple(getattr(bundle, "effects", ()) or ()):
        if getattr(effect, "run_id", run_id) != run_id:
            raise PersistenceConflictError("recovery effect belongs to another run")
        status = getattr(getattr(effect, "status", None), "value", getattr(effect, "status", None))
        if status not in {"prepared", "running", "succeeded", "failed", "unknown"}:
            raise PersistenceConflictError(f"recovery effect has unknown status {status!r}")


@dataclass(slots=True)
class _KernelRunState:
    run_id: str
    request: RunRequest
    kernel: Any
    cancel_event: asyncio.Event
    condition: asyncio.Condition
    events: list[AgentEvent]
    command_acks: dict[str, tuple[str, CommandAck]]
    controls: asyncio.Queue[ControlCommand]
    plugin_lock: dict[str, str]
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    control_store: Any | None = None
    lease: Any | None = None
    lease_task: asyncio.Task[None] | None = None
    lease_lost: bool = False
    control_handler: ControlHandler | None = None
    task: asyncio.Task[None] | None = None
    result: RunResult | None = None
    state: str = "running"
    terminal: bool = False
    checkpoint_error: BaseException | None = None


class KernelAgentService:
    """Host one or more runs on the new :class:`AgentLoop` SPI."""

    @classmethod
    def from_plugin_host(
        cls,
        host: Any,
        *,
        event_log: Any | None = None,
        effect_ledger: Any | None = None,
        session_store: Any | None = None,
        lease_store: Any | None = None,
        owner_id: str | None = None,
        lease_ttl_seconds: float = 60.0,
        control_handler: ControlHandler | None = None,
        plugin_lock: Mapping[str, str] | None = None,
        plugin_lock_provider: Callable[[], Mapping[str, str]] | None = None,
        plugin_snapshot_provider: Callable[[], Any] | None = None,
        plugin_restore_provider: Callable[[Mapping[str, Mapping[str, Any]]], Any]
        | None = None,
        **composition_options: Any,
    ) -> "KernelAgentService":
        """Create a service whose kernel is resolved from ``PluginHost``.

        This is the supported third-party entry point: activate plugins,
        register their capabilities, then hand the host to this factory.
        Adapters do not need to know how individual components are wired.
        """

        from ..kernel import PluginKernelComposer

        composer = PluginKernelComposer(
            host,
            effect_ledger=effect_ledger,
            **composition_options,
        )
        if plugin_lock is None and plugin_lock_provider is None:
            snapshot = getattr(host, "lock_snapshot", None)
            plugin_lock_provider = snapshot if callable(snapshot) else None
        control_handler = control_handler or _control_handler_from_registry(host)
        if session_store is None:
            sessions_registry = getattr(host, "registries", {}).get("sessions")
            if sessions_registry is not None:
                session_store = sessions_registry.resolve("default", scope="run")
        lifecycle_registry = getattr(host, "registries", {}).get("session.lifecycle")
        session_lifecycle_handlers = (
            tuple(lifecycle_registry.resolve_all(scope="session"))
            if lifecycle_registry is not None
            else ()
        )
        return cls(
            kernel_factory=composer.build,
            event_log=event_log,
            session_store=session_store,
            lease_store=lease_store,
            owner_id=owner_id,
            lease_ttl_seconds=lease_ttl_seconds,
            control_handler=control_handler,
            plugin_lock=plugin_lock,
            plugin_lock_provider=plugin_lock_provider,
            plugin_snapshot_provider=host.snapshot,
            plugin_restore_provider=host.restore_snapshot,
            session_lifecycle_handlers=session_lifecycle_handlers,
        )

    def __init__(
        self,
        kernel: Any | None = None,
        *,
        kernel_factory: Callable[..., Any] | None = None,
        event_log: Any | None = None,
        session_store: Any | None = None,
        lease_store: Any | None = None,
        owner_id: str | None = None,
        lease_ttl_seconds: float = 60.0,
        control_handler: ControlHandler | None = None,
        plugin_lock: Mapping[str, str] | None = None,
        plugin_lock_provider: Callable[[], Mapping[str, str]] | None = None,
        plugin_snapshot_provider: Callable[[], Any] | None = None,
        plugin_restore_provider: Callable[[Mapping[str, Mapping[str, Any]]], Any]
        | None = None,
        session_lifecycle_handlers: tuple[Any, ...] = (),
    ) -> None:
        if kernel is None and kernel_factory is None:
            raise ValueError("kernel or kernel_factory is required")
        self._kernel = kernel
        self._kernel_factory = kernel_factory
        self._event_log = event_log
        self._session_store = session_store
        self._lease_store = lease_store
        self._owner_id = owner_id or uuid4().hex
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        self._lease_ttl_seconds = lease_ttl_seconds
        # A loop may expose its own control SPI (the compatibility adapter is
        # one example).  Keep an explicitly supplied service handler as the
        # highest-priority override, otherwise discover that optional hook
        # without coupling the service to a concrete loop implementation.
        self._control_handler = control_handler or getattr(kernel, "handle_control", None)
        self._plugin_lock = dict(plugin_lock or {})
        self._plugin_lock_provider = plugin_lock_provider
        self._plugin_snapshot_provider = plugin_snapshot_provider
        self._plugin_restore_provider = plugin_restore_provider
        self._session_lifecycle_handlers = tuple(session_lifecycle_handlers)
        self._sessions: dict[str, SessionInfo] = {}
        self._runs: dict[str, _KernelRunState] = {}
        self._request_runs: dict[tuple[str, str], str] = {}

    async def open_session(self, request: SessionOpenRequest) -> SessionInfo:
        session_id = request.session_id or uuid4().hex
        existing = self._sessions.get(session_id)
        if existing is not None:
            _validate_session_workspace(existing, request.metadata)
            return existing
        if self._session_store is not None:
            get_session = getattr(self._session_store, "get", None)
            if callable(get_session):
                existing = await get_session(session_id)
                if existing is not None:
                    _validate_session_workspace(existing, request.metadata)
                    self._sessions[session_id] = existing
                    return existing
        session = SessionInfo(
            session_id=session_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata=request.metadata,
        )
        self._sessions[session_id] = session
        if self._session_store is not None:
            put_session = getattr(self._session_store, "put", None)
            if callable(put_session):
                await put_session(session)
        return session

    def active_run_ids(self) -> tuple[str, ...]:
        """Return nonterminal Runs that still depend on their locked graph."""

        return tuple(
            run_id
            for run_id, state in self._runs.items()
            if not state.terminal
        )

    def _current_plugin_lock(self) -> dict[str, str]:
        value = (
            self._plugin_lock_provider()
            if self._plugin_lock_provider is not None
            else self._plugin_lock
        )
        if not isinstance(value, Mapping):
            raise TypeError("plugin_lock_provider must return a mapping")
        lock = {str(key): str(version) for key, version in value.items()}
        if any(not key.strip() or not version.strip() for key, version in lock.items()):
            raise ValueError("plugin lock keys and versions must be non-empty strings")
        return lock

    async def load_session(self, request: SessionOpenRequest) -> SessionInfo:
        """Load an existing durable Session without creating a new one."""

        session_id = request.session_id
        if not session_id:
            raise ValueError("load_session requires an explicit session_id")
        existing = self._sessions.get(session_id)
        if existing is not None:
            _validate_session_workspace(existing, request.metadata)
            return existing
        if self._session_store is not None:
            get_session = getattr(self._session_store, "get", None)
            if callable(get_session):
                existing = await get_session(session_id)
                if existing is not None:
                    _validate_session_workspace(existing, request.metadata)
                    self._sessions[session_id] = existing
                    return existing
        raise ValueError(f"unknown durable session: {session_id}")

    async def update_session_metadata(
        self,
        session_id: str,
        metadata: Mapping[str, Any],
    ) -> SessionInfo:
        """Persist host controls that occur outside a running Agent Loop."""

        existing = self._sessions.get(session_id)
        if existing is None and self._session_store is not None:
            get_session = getattr(self._session_store, "get", None)
            if callable(get_session):
                existing = await get_session(session_id)
        if existing is None:
            raise ValueError(f"unknown durable session: {session_id}")
        _validate_session_workspace(existing, metadata)
        updated = SessionInfo(
            session_id=existing.session_id,
            created_at=existing.created_at,
            metadata=metadata,
        )
        if self._session_store is not None:
            update_session = getattr(self._session_store, "update", None)
            if not callable(update_session):
                raise TypeError("configured SessionStore does not support metadata updates")
            await update_session(updated)
        self._sessions[session_id] = updated
        return updated

    async def start(self, request: RunRequest) -> AgentRunHandle:
        if request.session_id not in self._sessions:
            if self._session_store is not None:
                get_session = getattr(self._session_store, "get", None)
                if callable(get_session):
                    restored = await get_session(request.session_id)
                    if restored is not None:
                        self._sessions[request.session_id] = restored
            if request.session_id not in self._sessions:
                raise ValueError(f"unknown session: {request.session_id}")
        _validate_session_workspace(
            self._sessions[request.session_id],
            request.metadata,
        )
        key = (request.session_id, request.request_id)
        existing_run_id = self._request_runs.get(key)
        if existing_run_id is not None:
            existing_request = self._runs[existing_run_id].request
            if _request_fingerprint(existing_request) != _request_fingerprint(request):
                raise PersistenceConflictError(
                    f"request_id {request.request_id!r} was reused with different data"
                )
            return _KernelAgentRunHandle(self._runs[existing_run_id])
        if self._event_log is not None:
            find_run = getattr(self._event_log, "find_run", None)
            if callable(find_run):
                existing_run_id = await find_run(*key)
                if existing_run_id is not None:
                    loader = getattr(self._event_log, "get_run_request", None)
                    if callable(loader):
                        stored = await loader(existing_run_id)
                        if stored is not None and _request_fingerprint(stored) != _request_fingerprint(request):
                            raise PersistenceConflictError(
                                f"request_id {request.request_id!r} was reused with different data"
                            )
                    return await self.resume(existing_run_id)

        # Rebuild conversation context for a fresh turn from durable Session
        # facts.  The history is private runtime metadata and is deliberately
        # stripped before persisting the request itself.
        session_events = await self._load_session_events(request.session_id)
        runtime_metadata = dict(request.metadata)
        run_plugin_lock = self._current_plugin_lock()
        if run_plugin_lock:
            runtime_metadata["plugin_lock"] = dict(run_plugin_lock)
        if session_events:
            runtime_metadata["_session_events"] = {
                "events": [event.to_dict() for event in session_events]
            }

        run_id = uuid4().hex
        run_request = replace(
            request,
            metadata={**runtime_metadata, "run_id": run_id},
        )
        await self._register_durable_run(run_id, run_request)
        state = _KernelRunState(
            run_id=run_id,
            request=run_request,
            kernel=self._new_kernel(run_request, None),
            cancel_event=asyncio.Event(),
            condition=asyncio.Condition(),
            events=[],
            command_acks={},
            controls=asyncio.Queue(),
            plugin_lock=run_plugin_lock,
            control_store=self._event_log,
            control_handler=self._control_handler,
        )
        self._remember_state(state)
        try:
            await self._acquire_lease(state)
        except Exception:
            self._forget_state(state)
            raise
        state.task = asyncio.create_task(self._consume(state))
        return _KernelAgentRunHandle(state)

    async def resume(self, run_id: str) -> AgentRunHandle:
        state = self._runs.get(run_id)
        if state is not None:
            return _KernelAgentRunHandle(state)
        if self._event_log is None:
            raise ValueError(f"unknown run: {run_id}")

        request_data = await self._load_durable_request(run_id)
        if request_data is None:
            raise ValueError(f"run {run_id!r} has no durable request")
        bundle = await self._event_log.load_recovery_bundle(run_id)
        _validate_recovery_bundle(run_id, bundle)
        request = _request_from_dict(request_data)
        current_plugin_lock = self._current_plugin_lock()
        stored_plugin_lock = request.metadata.get("plugin_lock", {})
        if (
            isinstance(stored_plugin_lock, Mapping)
            and dict(stored_plugin_lock) != current_plugin_lock
            # Requests written before plugin-lock fencing have no lock at all;
            # they remain resumable through the compatibility path. Once a
            # non-empty lock was persisted, a different active graph is
            # rejected.
            and bool(stored_plugin_lock)
        ):
            raise PersistenceConflictError(
                f"run {run_id!r} was created with a different plugin lock"
            )
        checkpoint_plugin_lock = getattr(getattr(bundle, "checkpoint", None), "plugin_lock", {})
        if (
            isinstance(checkpoint_plugin_lock, Mapping)
            and dict(checkpoint_plugin_lock) != current_plugin_lock
            and bool(checkpoint_plugin_lock)
        ):
            raise PersistenceConflictError(
                f"run {run_id!r} checkpoint was created with a different plugin lock"
            )
        await self._restore_plugin_snapshot(bundle)
        request = replace(
            request,
            metadata={
                **dict(request.metadata),
                "run_id": run_id,
                # Kept in-memory only; the original durable request remains
                # a serializable, host-independent record.
                "_recovery_bundle": bundle,
                "_session_events": {
                    "events": [
                        event.to_dict()
                        for event in await self._load_session_events(request.session_id)
                        if event.run_id != run_id
                    ]
                },
            },
        )
        await self.open_session(SessionOpenRequest(session_id=request.session_id))
        state = _KernelRunState(
            run_id=run_id,
            request=request,
            kernel=self._new_kernel(request, bundle),
            cancel_event=asyncio.Event(),
            condition=asyncio.Condition(),
            events=list(bundle.events),
            command_acks={},
            controls=asyncio.Queue(),
            plugin_lock=(
                dict(stored_plugin_lock)
                if isinstance(stored_plugin_lock, Mapping)
                else {}
            ),
            control_store=self._event_log,
            control_handler=self._control_handler,
        )
        self._remember_state(state)
        terminal_event = next(
            (event for event in reversed(state.events) if event.type in _TERMINAL_EVENTS),
            None,
        )
        if terminal_event is not None:
            state.result = _result_from_event(state.events, terminal_event)
            state.state = state.result.status
            state.terminal = True
        else:
            try:
                await self._acquire_lease(state)
            except Exception:
                self._forget_state(state)
                raise
            state.task = asyncio.create_task(self._consume(state))
        return _KernelAgentRunHandle(state)

    async def _restore_plugin_snapshot(self, bundle: Any) -> None:
        snapshot = getattr(getattr(bundle, "checkpoint", None), "plugin_snapshot", {})
        if not snapshot or self._plugin_restore_provider is None:
            return
        value = self._plugin_restore_provider(snapshot)
        if inspect.isawaitable(value):
            await value

    async def attach(self, run_id: str) -> AgentRunHandle:
        """Return a read-only observer for a durable Run.

        Attaching never creates a Kernel task and never acquires a lease.  An
        in-process observer shares the live state condition; an observer in a
        different process polls the durable event log until a terminal fact is
        committed.
        """

        state = self._runs.get(run_id)
        if state is not None:
            return _KernelAgentRunHandle(state, read_only=True)
        if self._event_log is None:
            raise ValueError(f"unknown run: {run_id}")
        bundle = await self._event_log.load_recovery_bundle(run_id)
        _validate_recovery_bundle(run_id, bundle)
        if not bundle.events and bundle.checkpoint is None:
            # ``register_run`` is committed before the worker task starts.
            # Treat that durable request as a known-but-not-yet-observable Run
            # so an observer can attach during the tiny startup window instead
            # of racing the first ``run.started`` event.
            loader = getattr(self._event_log, "get_run_request", None)
            request_data = await loader(run_id) if callable(loader) else None
            if not isinstance(request_data, Mapping):
                raise ValueError(f"unknown run: {run_id!r}")
            session_id = str(request_data.get("session_id", ""))
            if not session_id:
                raise PersistenceConflictError(
                    f"run {run_id!r} durable request has no session_id"
                )
        else:
            session_id = (
                bundle.checkpoint.session_id
                if bundle.checkpoint is not None
                else bundle.events[0].session_id
            )
        terminal_event = next(
            (event for event in reversed(bundle.events) if event.type in _TERMINAL_EVENTS),
            None,
        )
        result = _result_from_event(bundle.events, terminal_event) if terminal_event else None
        return _AttachedKernelRunHandle(
            self,
            run_id=run_id,
            session_id=session_id,
            events=tuple(bundle.events),
            result=result,
        )

    async def get_status(self, session_id: str, run_id: str | None = None) -> RunStatus:
        if run_id is None:
            candidates = [state for state in self._runs.values() if state.request.session_id == session_id]
            if not candidates:
                raise ValueError(f"no run for session: {session_id}")
            state = max(candidates, key=lambda item: len(item.events))
            return self._status(state)

        state = self._runs.get(run_id)
        if state is not None:
            if state.request.session_id != session_id:
                raise ValueError(f"unknown run: {run_id}")
            return self._status(state)
        if self._event_log is None:
            raise ValueError(f"unknown run: {run_id}")
        bundle = await self._event_log.load_recovery_bundle(run_id)
        _validate_recovery_bundle(run_id, bundle)
        if not bundle.events and bundle.checkpoint is None:
            loader = getattr(self._event_log, "get_run_request", None)
            request_data = await loader(run_id) if callable(loader) else None
            if not isinstance(request_data, Mapping):
                raise ValueError(f"unknown run: {run_id}")
            durable_session_id = str(request_data.get("session_id", ""))
            if not durable_session_id:
                raise PersistenceConflictError(
                    f"run {run_id!r} durable request has no session_id"
                )
            if durable_session_id != session_id:
                raise ValueError(f"unknown run: {run_id}")
            return RunStatus(
                run_id=run_id,
                session_id=session_id,
                state="running",
                sequence=0,
                terminal=False,
            )
        durable_session_id = (
            bundle.checkpoint.session_id
            if bundle.checkpoint is not None
            else bundle.events[0].session_id
        )
        if durable_session_id != session_id:
            raise ValueError(f"unknown run: {run_id}")
        terminal_event = next(
            (event for event in reversed(bundle.events) if event.type in _TERMINAL_EVENTS),
            None,
        )
        state_name = (
            terminal_event.type.removeprefix("run.")
            if terminal_event is not None
            else "running"
        )
        return RunStatus(
            run_id=run_id,
            session_id=session_id,
            state=state_name,
            sequence=len(bundle.events),
            terminal=terminal_event is not None,
        )

    async def close_session(self, session_id: str) -> None:
        states = [state for state in self._runs.values() if state.request.session_id == session_id]
        for state in states:
            if not state.terminal:
                state.cancel_event.set()
                await state.controls.put(
                    ControlCommand(
                        command_id=uuid4().hex,
                        session_id=session_id,
                        run_id=state.run_id,
                        kind="run.cancel",
                        payload={"reason": "session closed"},
                        source="service",
                    )
                )
        for state in states:
            if state.task is not None:
                await state.task
        self._sessions.pop(session_id, None)
        cleanup_errors: list[BaseException] = []
        for handler in self._session_lifecycle_handlers:
            callback = getattr(handler, "on_session_close", None)
            if not callable(callback):
                cleanup_errors.append(
                    TypeError(
                        "session lifecycle plugin must provide on_session_close(session_id)"
                    )
                )
                continue
            try:
                value = callback(session_id)
                if inspect.isawaitable(value):
                    await value
            except BaseException as exc:
                cleanup_errors.append(exc)
        if self._session_store is not None:
            close_session = getattr(self._session_store, "close", None)
            if callable(close_session):
                await close_session(session_id)
        if cleanup_errors:
            raise ExceptionGroup(
                f"session {session_id!r} cleanup failed",
                cleanup_errors,
            )

    def _new_kernel(self, request: RunRequest, bundle: Any | None) -> Any:
        if self._kernel_factory is not None:
            return _call_factory(self._kernel_factory, request, bundle)
        return self._kernel

    def _remember_state(self, state: _KernelRunState) -> None:
        self._runs[state.run_id] = state
        self._request_runs[(state.request.session_id, state.request.request_id)] = state.run_id

    def _forget_state(self, state: _KernelRunState) -> None:
        self._runs.pop(state.run_id, None)
        self._request_runs.pop((state.request.session_id, state.request.request_id), None)

    async def _register_durable_run(self, run_id: str, request: RunRequest) -> None:
        if self._event_log is None:
            return
        register_run = getattr(self._event_log, "register_run", None)
        if callable(register_run):
            durable_request = request.to_dict()
            metadata = dict(durable_request.get("metadata", {}))
            # Private runtime hydration (recovery/session event snapshots) is
            # process-local and can contain the entire transcript. Never put
            # it in the durable request record or expose it to another host.
            durable_request["metadata"] = {
                key: value for key, value in metadata.items() if not str(key).startswith("_")
            }
            await register_run(run_id, durable_request)

    async def _load_durable_request(self, run_id: str) -> Mapping[str, Any] | None:
        loader = getattr(self._event_log, "get_run_request", None)
        if not callable(loader):
            return None
        return await loader(run_id)

    async def _load_session_events(self, session_id: str) -> tuple[AgentEvent, ...]:
        """Load prior session facts without making persistence mandatory."""

        if self._event_log is not None:
            loader = getattr(self._event_log, "events_for_session", None)
            if callable(loader):
                value = await loader(session_id)
                return tuple(value or ())
        events: list[AgentEvent] = []
        for state in self._runs.values():
            if state.request.session_id == session_id:
                events.extend(state.events)
        return tuple(events)

    async def _consume(self, state: _KernelRunState) -> None:
        if state.lease is not None and self._lease_store is not None:
            renew = getattr(self._lease_store, "renew", None)
            if callable(renew):
                state.lease_task = asyncio.create_task(
                    self._renew_lease(state),
                    name=f"agent-lease-renew:{state.run_id}",
                )
        try:
            result = await state.kernel.run(
                state.request,
                emit=lambda event: self._append_event(state, event),
                cancel_event=state.cancel_event,
                controls=self._controls(state),
            )
            if not isinstance(result, RunResult):
                raise TypeError("Agent Loop must return api.RunResult")
            if state.lease_lost and result.status != "failed" and not state.terminal:
                lease_error = _error(
                    ErrorCode.LEASE_LOST,
                    "run lease was lost while the Agent Loop was executing",
                    category="lease",
                    retryable=True,
                )
                result = RunResult(
                    status="failed",
                    stop_reason="lease_lost",
                    final_message=result.final_message,
                    usage=result.usage,
                    artifacts=result.artifacts,
                    error=lease_error,
                    metadata=result.metadata,
                )
            state.result = result
            state.state = result.status
            if not state.terminal:
                await self._append_event(
                    state,
                    self._terminal_event_for_result(state, result),
                )
            state.terminal = True
        except asyncio.CancelledError:
            if not state.terminal:
                state.result = RunResult(status="cancelled", stop_reason="cancelled")
                await self._append_event(
                    state,
                    self._terminal_event_for_result(state, state.result),
                )
                state.state = "cancelled"
                state.terminal = True
        except Exception as exc:
            # A compliant loop emits its terminal event before returning.  If
            # a provider raises after that fact, preserve the durable terminal
            # result instead of replacing it with a second failure.
            if state.terminal and state.checkpoint_error is None:
                return
            error = _error(
                ErrorCode.INTERNAL_ERROR,
                f"agent loop failed: {type(exc).__name__}: {exc}",
                category="loop",
                details={"exception_type": type(exc).__name__},
            )
            state.result = RunResult(status="failed", stop_reason="error", error=error)
            if not state.terminal:
                await self._append_event(
                    state,
                    AgentEvent(
                        event_id=uuid4().hex,
                        sequence=len(state.events) + 1,
                        session_id=state.request.session_id,
                        run_id=state.run_id,
                        turn_id=state.request.turn_id,
                        type="run.failed",
                        payload={"stop_reason": "error", "error": error.to_dict()},
                    ),
                )
            state.state = "failed"
            state.terminal = True
        finally:
            if state.lease_task is not None:
                state.lease_task.cancel()
                await asyncio.gather(state.lease_task, return_exceptions=True)
                state.lease_task = None
            await self._release_lease(state)
            async with state.condition:
                state.condition.notify_all()

    async def _acquire_lease(self, state: _KernelRunState) -> None:
        if self._lease_store is None:
            return
        acquire = getattr(self._lease_store, "acquire", None)
        if not callable(acquire):
            raise TypeError("lease_store must provide acquire(run_id, owner_id, ttl_seconds)")
        state.lease = await acquire(
            state.run_id,
            owner_id=self._owner_id,
            ttl_seconds=self._lease_ttl_seconds,
        )

    async def _release_lease(self, state: _KernelRunState) -> None:
        if state.lease is None or self._lease_store is None:
            return
        release = getattr(self._lease_store, "release", None)
        if callable(release):
            await release(state.lease)
        state.lease = None

    @staticmethod
    def _effect_records_for_event(
        state: _KernelRunState,
        event: AgentEvent,
    ) -> tuple[EffectRecord, ...]:
        """Translate a Kernel's staged effect data into durable records."""

        provider = getattr(state.kernel, "pending_effects_for_event", None)
        if not callable(provider):
            return ()
        value = provider(event)
        if inspect.isawaitable(value):
            # The Kernel hook is intentionally synchronous: it snapshots
            # in-memory transition data before opening the SQLite transaction.
            return ()
        if not isinstance(value, (list, tuple)):
            return ()
        records: list[EffectRecord] = []
        event_call_id = str(event.payload.get("call_id", ""))
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) != 7:
                continue
            _call_id, effect_id, status, result, run_id, idempotency_key, request_digest = item
            if event_call_id and str(_call_id) != event_call_id:
                raise PersistenceConflictError(
                    "staged effect transition call_id does not match the event"
                )
            if not all(
                isinstance(field, str) and field.strip()
                for field in (effect_id, run_id, idempotency_key, request_digest)
            ):
                raise PersistenceConflictError(
                    "staged effect transition is missing durable identity"
                )
            if result is not None and not isinstance(result, Mapping):
                raise PersistenceConflictError(
                    "staged effect transition result must be a mapping"
                )
            try:
                effect_status = EffectStatus(status)
            except (TypeError, ValueError) as exc:
                raise PersistenceConflictError(
                    f"staged effect transition has unknown status {status!r}"
                ) from exc
            records.append(
                EffectRecord(
                    effect_id=effect_id,
                    run_id=run_id,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    status=effect_status,
                    result=dict(result) if isinstance(result, Mapping) else None,
                )
            )
        return tuple(records)

    async def _controls(self, state: _KernelRunState) -> AsyncIterator[ControlCommand]:
        while not state.terminal:
            yield await state.controls.get()

    async def _append_event(self, state: _KernelRunState, event: AgentEvent) -> None:
        normalized = self._normalize_event(state, event)
        effect_records = self._effect_records_for_event(state, normalized)
        try:
            checkpoint = await self._checkpoint_for_event(state, normalized)
        except BaseException as exc:
            state.checkpoint_error = exc
            raise
        if self._event_log is not None:
            if checkpoint is not None:
                commit = getattr(self._event_log, "commit_checkpoint", None)
                if not callable(commit):
                    raise TypeError(
                        "event_log must provide commit_checkpoint for boundary events"
                    )
                try:
                    outbox_record = OutboxRecord(
                        f"{state.run_id}:{normalized.sequence}", normalized
                    )
                    try:
                        signature = inspect.signature(commit)
                        supports_outbox = "outbox" in signature.parameters or any(
                            parameter.kind == inspect.Parameter.VAR_KEYWORD
                            for parameter in signature.parameters.values()
                        )
                    except (TypeError, ValueError):
                        supports_outbox = True
                    supports_effects = "effects" in signature.parameters or any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in signature.parameters.values()
                    )
                    if supports_outbox:
                        kwargs = {
                            "events": (normalized,),
                            "outbox": (outbox_record,),
                        }
                        if supports_effects:
                            kwargs["effects"] = effect_records
                        value = commit(checkpoint, **kwargs)
                    else:
                        # Legacy stores predate the outbox column.  Keep their
                        # checkpoint behavior source-compatible while making
                        # the capability loss explicit for operators.
                        kwargs = {"events": (normalized,)}
                        if supports_effects:
                            kwargs["effects"] = effect_records
                        value = commit(checkpoint, **kwargs)
                    if inspect.isawaitable(value):
                        await value
                    if supports_effects:
                        acknowledge = getattr(
                            state.kernel, "acknowledge_effect_boundary", None
                        )
                        if callable(acknowledge):
                            acknowledge(normalized)
                    else:
                        finalize = getattr(
                            state.kernel, "finalize_effect_boundary", None
                        )
                        if callable(finalize):
                            value = finalize(normalized)
                            if inspect.isawaitable(value):
                                await value
                except BaseException as exc:
                    state.checkpoint_error = exc
                    raise
            else:
                # Every emitted fact gets an outbox envelope.  Stores that
                # implement ``append_with_outbox`` can publish it after the
                # same transaction commits; older stores retain the append
                # fallback for source compatibility during migration.
                append_with_outbox = getattr(self._event_log, "append_with_outbox", None)
                if callable(append_with_outbox):
                    value = append_with_outbox(
                        normalized,
                        outbox=OutboxRecord(
                            f"{state.run_id}:{normalized.sequence}", normalized
                        ),
                    )
                    if inspect.isawaitable(value):
                        await value
                    finalize = getattr(
                        state.kernel, "finalize_effect_boundary", None
                    )
                    if effect_records and callable(finalize):
                        value = finalize(normalized)
                        if inspect.isawaitable(value):
                            await value
                else:
                    append_event = getattr(self._event_log, "append_event", None)
                    if not callable(append_event):
                        append_event = getattr(self._event_log, "append", None)
                    if not callable(append_event):
                        raise TypeError("event_log must provide append_event(event)")
                    value = append_event(normalized)
                    if inspect.isawaitable(value):
                        await value
                    finalize = getattr(
                        state.kernel, "finalize_effect_boundary", None
                    )
                    if effect_records and callable(finalize):
                        value = finalize(normalized)
                        if inspect.isawaitable(value):
                            await value
        elif effect_records:
            # An in-memory service has no durable transaction boundary. Keep
            # standalone usage correct by completing the staged effect after
            # the event has been accepted by the in-process state.
            finalize = getattr(state.kernel, "finalize_effect_boundary", None)
            if callable(finalize):
                value = finalize(normalized)
                if inspect.isawaitable(value):
                    await value
        async with state.condition:
            state.events.append(normalized)
            if normalized.type in _TERMINAL_EVENTS:
                state.state = normalized.type.removeprefix("run.")
                state.terminal = True
                if state.result is None:
                    state.result = _result_from_event(state.events, normalized)
            state.condition.notify_all()

    async def _checkpoint_for_event(
        self,
        state: _KernelRunState,
        event: AgentEvent,
    ) -> RunCheckpoint | None:
        """Build the snapshot that must commit with a boundary event."""

        if (
            self._event_log is None
            or event.type not in _CHECKPOINT_EVENTS
            or state.checkpoint_error is not None
        ):
            return None
        future_events = [*state.events, event]
        terminal = event.type in _TERMINAL_EVENTS or state.terminal
        future_result = state.result
        if future_result is None and event.type in _TERMINAL_EVENTS:
            future_result = _result_from_event(future_events, event)
        state_payload: dict[str, Any] = {
            "state": (
                event.type.removeprefix("run.")
                if event.type in _TERMINAL_EVENTS
                else state.state
            ),
            "terminal": terminal,
            "last_event_type": event.type,
            "last_sequence": event.sequence,
            "request_id": state.request.request_id,
            "turn_id": state.request.turn_id,
        }
        if future_result is not None:
            state_payload["stop_reason"] = future_result.stop_reason
            if future_result.final_message:
                state_payload["final_content"] = future_result.final_message
            if future_result.metadata:
                state_payload["metadata"] = dict(future_result.metadata)
        manifest = event.payload.get("manifest")
        if not isinstance(manifest, Mapping):
            # Carry the last assembled context fence forward so a later model
            # or terminal checkpoint can still restore provider-owned sources.
            for previous in reversed(future_events):
                candidate = previous.payload.get("manifest")
                if isinstance(candidate, Mapping):
                    manifest = candidate
                    break
        if isinstance(manifest, Mapping):
            state_payload["context_manifest"] = dict(manifest)
            if manifest.get("content_hash"):
                state_payload["context_digest"] = str(manifest["content_hash"])
        # Workflow state is part of the checkpoint boundary, not merely a
        # terminal summary.  A crash after ``tool.call.completed`` must retain
        # the Goal/Plan (or third-party policy) mutation that happened before
        # the event was emitted.  The optional Kernel hook keeps this generic;
        # compatibility loops and custom kernels can still expose state in
        # the event payload itself.
        workflow_state: Mapping[str, Any] | None = None
        snapshot = getattr(state.kernel, "workflow_checkpoint_payload", None)
        if callable(snapshot):
            try:
                value = snapshot()
                if inspect.isawaitable(value):
                    value = await value
                if isinstance(value, Mapping):
                    workflow_state = dict(value)
            except Exception:
                # A faulty optional serializer must not suppress the event
                # itself or make an otherwise recoverable run fail.
                workflow_state = None
        if workflow_state is None:
            candidate = event.payload.get("workflow_state")
            if isinstance(candidate, Mapping):
                workflow_state = dict(candidate)
        if workflow_state:
            state_payload["workflow_state"] = workflow_state
        plugin_snapshot: Mapping[str, Mapping[str, Any]] = {}
        if self._plugin_snapshot_provider is not None:
            value = self._plugin_snapshot_provider()
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, Mapping):
                plugin_snapshot = {
                    str(key): dict(item)
                    for key, item in value.items()
                    if isinstance(item, Mapping)
                }
        return RunCheckpoint(
            checkpoint_id=f"{state.run_id}:{event.sequence}",
            session_id=state.request.session_id,
            run_id=state.run_id,
            sequence=event.sequence,
            state=state_payload,
            event_hash=_event_hash(event),
            plugin_lock=state.plugin_lock,
            plugin_snapshot=plugin_snapshot,
        )

    async def _renew_lease(self, state: _KernelRunState) -> None:
        """Keep a long-running worker fenced until its Run reaches a terminal fact."""

        renew = getattr(self._lease_store, "renew", None)
        if not callable(renew) or state.lease is None:
            return
        # Renew well before expiry.  A fixed 100ms floor is fine for ordinary
        # leases but would make short test/embedded leases expire before the
        # first heartbeat; cap the interval as well as applying a tiny floor.
        interval = max(0.01, min(0.1, self._lease_ttl_seconds / 3))
        try:
            while not state.terminal and state.lease is not None:
                await asyncio.sleep(interval)
                if state.terminal or state.lease is None:
                    return
                state.lease = await renew(
                    state.lease,
                    ttl_seconds=self._lease_ttl_seconds,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A worker that loses its fence must stop before it can issue more
            # effects.  The outer consumer converts the result to a stable
            # LEASE_LOST failure after the loop observes cancellation.
            state.lease_lost = True
            state.cancel_event.set()

    @staticmethod
    def _normalize_event(state: _KernelRunState, event: AgentEvent) -> AgentEvent:
        event_id = event.event_id
        if any(existing.event_id == event_id for existing in state.events):
            event_id = uuid4().hex
        return AgentEvent(
            event_id=event_id,
            sequence=len(state.events) + 1,
            session_id=state.request.session_id,
            run_id=state.run_id,
            turn_id=event.turn_id or state.request.turn_id,
            type=event.type,
            payload=event.payload,
            audience=event.audience,
            occurred_at=event.occurred_at,
            correlation_id=event.correlation_id,
            protocol_version=event.protocol_version,
        )

    @staticmethod
    def _terminal_event_for_result(state: _KernelRunState, result: RunResult) -> AgentEvent:
        event_type = {
            "completed": "run.completed",
            "cancelled": "run.cancelled",
            "failed": "run.failed",
        }.get(result.status, "run.failed")
        payload: dict[str, Any] = {"stop_reason": result.stop_reason}
        if result.final_message:
            payload["final_content"] = result.final_message
        if result.usage is not None:
            payload["usage"] = result.usage.to_dict()
        if result.error is not None:
            payload["error"] = result.error.to_dict()
        if result.metadata:
            payload["metadata"] = dict(result.metadata)
        return AgentEvent(
            event_id=uuid4().hex,
            sequence=len(state.events) + 1,
            session_id=state.request.session_id,
            run_id=state.run_id,
            turn_id=state.request.turn_id,
            type=event_type,
            payload=payload,
        )

    @staticmethod
    def _status(state: _KernelRunState) -> RunStatus:
        return RunStatus(
            run_id=state.run_id,
            session_id=state.request.session_id,
            state=state.state,
            sequence=len(state.events),
            terminal=state.terminal,
        )


class _KernelAgentRunHandle:
    def __init__(self, state: _KernelRunState, *, read_only: bool = False) -> None:
        self._state = state
        self.run_id = state.run_id
        self._read_only = read_only

    async def events(self, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        index = after_sequence
        while True:
            async with self._state.condition:
                while len(self._state.events) <= index and not self._state.terminal:
                    await self._state.condition.wait()
                batch = list(self._state.events[index:])
                index = len(self._state.events)
                terminal = self._state.terminal
            for event in batch:
                yield event
            if terminal and not batch:
                return

    async def send(self, command: ControlCommand) -> CommandAck:
        if self._read_only:
            return CommandAck(
                command_id=command.command_id,
                accepted=False,
                status="rejected",
                error=_error(
                    ErrorCode.UNSUPPORTED_COMMAND,
                    "attached run handles are read-only",
                ),
            )
        async with self._state.control_lock:
            if (
                command.session_id != self._state.request.session_id
                or command.run_id != self.run_id
            ):
                return self._remember(
                    command,
                    CommandAck(
                        command_id=command.command_id,
                        accepted=False,
                        status="rejected",
                        error=_error(
                            ErrorCode.INVALID_REQUEST,
                            "command does not target this run",
                        ),
                    ),
                )

            command = command.canonical()
            digest = _digest(command)
            previous = self._state.command_acks.get(command.command_id)
            if previous is None:
                previous = await self._load_durable_ack(command.command_id)
                if previous is not None:
                    previous_digest, previous_ack = previous
                    self._state.command_acks[command.command_id] = previous
            if previous is not None:
                previous_digest, previous_ack = previous
                if previous_digest == digest:
                    return previous_ack
                return CommandAck(
                    command_id=command.command_id,
                    accepted=False,
                    status="rejected",
                    error=_error(
                        ErrorCode.COMMAND_ID_REUSED,
                        "command_id was already used with a different payload",
                    ),
                )

            if command.kind == "run.cancel":
                if self._state.terminal:
                    ack = CommandAck(
                        command_id=command.command_id,
                        accepted=False,
                        status="rejected",
                        error=_error(
                            ErrorCode.RUN_NOT_ACTIVE,
                            "run is no longer active",
                        ),
                    )
                else:
                    self._state.cancel_event.set()
                    await self._state.controls.put(command)
                    ack = CommandAck(
                        command_id=command.command_id,
                        accepted=True,
                        status="accepted",
                    )
            elif command.kind in {"run.inject", "run.cancel_inject"}:
                injection_id = command.payload.get("injection_id")
                text = command.payload.get("text")
                valid = isinstance(injection_id, str) and bool(injection_id.strip())
                if command.kind == "run.inject":
                    valid = valid and isinstance(text, str) and bool(text.strip())
                if self._state.terminal:
                    ack = CommandAck(
                        command_id=command.command_id,
                        accepted=False,
                        status="rejected",
                        error=_error(
                            ErrorCode.RUN_NOT_ACTIVE,
                            "run is no longer active",
                        ),
                    )
                elif not valid:
                    ack = CommandAck(
                        command_id=command.command_id,
                        accepted=False,
                        status="rejected",
                        error=_error(
                            ErrorCode.INVALID_REQUEST,
                            f"{command.kind} requires a non-empty injection_id"
                            + (
                                " and text"
                                if command.kind == "run.inject"
                                else ""
                            ),
                        ),
                    )
                else:
                    await self._state.controls.put(command)
                    ack = CommandAck(
                        command_id=command.command_id,
                        accepted=True,
                        status="accepted",
                    )
            elif self._state.terminal:
                ack = CommandAck(
                    command_id=command.command_id,
                    accepted=False,
                    status="rejected",
                    error=_error(
                        ErrorCode.RUN_NOT_ACTIVE,
                        "run is no longer active",
                    ),
                )
            elif self._state.control_handler is not None:
                ack = await _invoke_control_handler(
                    self._state.control_handler,
                    command,
                )
                if ack.accepted and not self._state.terminal:
                    # The handler is the extension seam, while the Loop
                    # remains the owner of execution state. Forward accepted
                    # commands so a plugin-aware Loop can consume them at a
                    # deterministic point.
                    await self._state.controls.put(command)
            else:
                ack = CommandAck(
                    command_id=command.command_id,
                    accepted=False,
                    status="rejected",
                    error=_error(
                        ErrorCode.UNSUPPORTED_COMMAND,
                        f"Agent Loop does not support command '{command.kind}'",
                    ),
                )
            await self._persist_durable_ack(command.command_id, digest, ack)
            return self._remember(command, ack, digest=digest)

    async def _load_durable_ack(
        self,
        command_id: str,
    ) -> tuple[str, CommandAck] | None:
        store = self._state.control_store
        getter = getattr(store, "get_control_ack", None)
        if not callable(getter):
            return None
        value = await getter(self.run_id, command_id)
        if not isinstance(value, Mapping):
            return None
        digest = value.get("digest")
        ack_data = value.get("ack")
        if not isinstance(digest, str) or not isinstance(ack_data, Mapping):
            return None
        return digest, _ack_from_dict(command_id, ack_data)

    async def _persist_durable_ack(
        self,
        command_id: str,
        digest: str,
        ack: CommandAck,
    ) -> None:
        store = self._state.control_store
        writer = getattr(store, "put_control_ack", None)
        if callable(writer):
            await writer(self.run_id, command_id, digest, ack.to_dict())

    def _remember(
        self,
        command: ControlCommand,
        ack: CommandAck,
        *,
        digest: str | None = None,
    ) -> CommandAck:
        self._state.command_acks[command.command_id] = (digest or _digest(command), ack)
        return ack

    async def cancel(self, reason: str | None = None) -> CommandAck:
        if self._read_only:
            return CommandAck(
                command_id="",
                accepted=False,
                status="rejected",
                error=_error(
                    ErrorCode.UNSUPPORTED_COMMAND,
                    "attached run handles are read-only",
                ),
            )
        if self._state.terminal:
            return CommandAck(
                command_id="",
                accepted=False,
                status="rejected",
                error=_error(ErrorCode.RUN_NOT_ACTIVE, "run is no longer active"),
            )
        self._state.cancel_event.set()
        await self._state.controls.put(
            ControlCommand(
                command_id=uuid4().hex,
                session_id=self._state.request.session_id,
                run_id=self.run_id,
                kind="run.cancel",
                payload={"reason": reason} if reason else {},
                source="handle",
            )
        )
        return CommandAck(command_id="", accepted=True, status="accepted")

    async def status(self) -> RunStatus:
        return KernelAgentService._status(self._state)

    async def wait(self) -> RunResult:
        if self._state.task is not None:
            await self._state.task
        if self._state.result is None:
            raise RuntimeError("run completed without a result")
        return self._state.result


class _AttachedKernelRunHandle:
    """Read-only handle that can follow a Run owned by another process."""

    def __init__(
        self,
        service: KernelAgentService,
        *,
        run_id: str,
        session_id: str,
        events: tuple[AgentEvent, ...],
        result: RunResult | None,
    ) -> None:
        self._service = service
        self.run_id = run_id
        self._session_id = session_id
        self._events = events
        self._result = result

    async def _snapshot(self) -> tuple[tuple[AgentEvent, ...], bool, RunResult | None]:
        live = self._service._runs.get(self.run_id)
        if live is not None:
            return tuple(live.events), live.terminal, live.result
        event_log = self._service._event_log
        if event_log is None:
            return self._events, self._result is not None, self._result
        bundle = await event_log.load_recovery_bundle(self.run_id)
        _validate_recovery_bundle(self.run_id, bundle)
        events = tuple(bundle.events)
        terminal_event = next(
            (event for event in reversed(events) if event.type in _TERMINAL_EVENTS),
            None,
        )
        result = _result_from_event(events, terminal_event) if terminal_event else None
        self._events = events
        self._result = result
        return events, terminal_event is not None, result

    async def events(self, after_sequence: int = 0) -> AsyncIterator[AgentEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        cursor = after_sequence
        while True:
            events, terminal, _ = await self._snapshot()
            batch = [event for event in events if event.sequence > cursor]
            for event in batch:
                cursor = event.sequence
                yield event
            if terminal and not batch:
                return
            await asyncio.sleep(0.02)

    async def send(self, command: ControlCommand) -> CommandAck:
        return CommandAck(
            command_id=command.command_id,
            accepted=False,
            status="rejected",
            error=_error(
                ErrorCode.UNSUPPORTED_COMMAND,
                "attached run handles are read-only",
            ),
        )

    async def cancel(self, reason: str | None = None) -> CommandAck:
        return CommandAck(
            command_id="",
            accepted=False,
            status="rejected",
            error=_error(
                ErrorCode.UNSUPPORTED_COMMAND,
                "attached run handles are read-only",
            ),
        )

    async def status(self) -> RunStatus:
        events, terminal, _ = await self._snapshot()
        state = "running"
        if terminal:
            state = next(
                event.type.removeprefix("run.")
                for event in reversed(events)
                if event.type in _TERMINAL_EVENTS
            )
        return RunStatus(
            run_id=self.run_id,
            session_id=self._session_id,
            state=state,
            sequence=len(events),
            terminal=terminal,
        )

    async def wait(self) -> RunResult:
        async for _ in self.events():
            pass
        _, _, result = await self._snapshot()
        if result is None:
            raise RuntimeError("run completed without a result")
        return result


__all__ = ["KernelAgentService"]
