"""Task-registry Artifact processor for the native Kernel."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from box_agent.api import ArtifactPublishRequest
from .artifacts import artifact_envelope
from box_agent.context import TaskContext
from box_agent.compat.events import ArtifactEvent

from .task_registry import begin_task, finish_task, register_artifact_revision


class TaskRegistryArtifactProcessor:
    """Persist stable artifact/revision lineage before host publication."""

    def begin_run(self, request: Any) -> None:
        workspace, context, artifact_root = _task_scope(request)
        begin_task(workspace, context, artifact_root_dir=artifact_root)

    def end_run(
        self,
        request: Any,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        workspace, context, artifact_root = _task_scope(request)
        stop_reason = str(payload.get("stop_reason", "") or "")
        if event_type == "run.completed" and stop_reason != "checkpoint_paused":
            execution_status = "completed"
            delivery_status = "complete"
        elif event_type == "run.completed":
            execution_status = "paused"
            delivery_status = "incomplete"
        elif event_type == "run.cancelled":
            execution_status = "cancelled"
            delivery_status = "incomplete"
        else:
            execution_status = "error"
            delivery_status = "incomplete"
        finish_task(
            workspace,
            context,
            execution_status=execution_status,
            delivery_status=delivery_status,
            artifact_root_dir=artifact_root,
        )

    def process(self, request: ArtifactPublishRequest) -> Mapping[str, Any]:
        metadata = request.metadata
        workspace = Path(str(metadata.get("workspace_dir", "."))).expanduser().resolve()
        task_id = str(metadata.get("task_id", "") or request.turn_id)
        correlation_session = str(
            metadata.get("correlation_session_id", "") or request.session_id
        )
        correlation_turn = str(
            metadata.get("correlation_turn_id", "") or request.turn_id
        )
        artifact_root = _artifact_root(workspace, metadata)
        raw = dict(request.artifact)
        raw.setdefault("tool_call_id", request.call_id)
        event = ArtifactEvent(
            tool_call_id=str(raw.get("tool_call_id", request.call_id)),
            kind=str(raw.get("kind", "file") or "file"),
            filename=str(raw.get("filename", "") or _filename(raw)),
            rel_path=str(raw.get("rel_path", "") or ""),
            abs_path=str(raw.get("abs_path", "") or _absolute_path(raw)),
            uri=str(raw.get("uri", "") or ""),
            mime=str(raw.get("mime", "application/octet-stream")),
            size=int(raw.get("size", -1) or -1),
            sha256=str(raw.get("sha256", "") or ""),
            produced_at=str(raw.get("produced_at", "") or ""),
            layout_id=str(raw.get("layout_id", "") or ""),
            edit_mode=str(raw.get("edit_mode", "") or ""),
        )
        lineage = register_artifact_revision(
            workspace,
            TaskContext(
                session_id=correlation_session,
                task_id=task_id,
                turn_id=correlation_turn,
            ),
            event,
            artifact_root_dir=artifact_root,
        )
        return {
            **raw,
            **artifact_envelope(
                event,
                str(artifact_root),
                session_id=correlation_session,
                task_id=task_id,
                turn_id=correlation_turn,
                lineage=lineage,
            ),
        }


def _artifact_root(workspace: Path, metadata: Mapping[str, Any]) -> Path:
    layout = metadata.get("workspace_layout", metadata.get("workspaceLayout", {}))
    if isinstance(layout, Mapping):
        value = layout.get("artifact_root_dir", layout.get("artifactRootDir"))
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser().resolve()
    return (workspace / "output").resolve()


def _task_scope(request: Any) -> tuple[Path, TaskContext, Path]:
    metadata = getattr(request, "metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    workspace = Path(str(metadata.get("workspace_dir", "."))).expanduser().resolve()
    session_id = str(
        metadata.get("correlation_session_id", "")
        or getattr(request, "session_id", "")
    )
    turn_id = str(
        metadata.get("correlation_turn_id", "")
        or getattr(request, "turn_id", "")
    )
    task_id = str(metadata.get("task_id", "") or turn_id)
    return (
        workspace,
        TaskContext(session_id=session_id, task_id=task_id, turn_id=turn_id),
        _artifact_root(workspace, metadata),
    )


def _absolute_path(payload: Mapping[str, Any]) -> str:
    uri = str(payload.get("uri", "") or "")
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return ""
    path = unquote(parsed.path)
    if parsed.netloc:
        path = f"//{parsed.netloc}{path}"
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return str(Path(path))


def _filename(payload: Mapping[str, Any]) -> str:
    path = str(payload.get("abs_path", "") or _absolute_path(payload))
    return Path(path).name if path else "artifact"


__all__ = ["TaskRegistryArtifactProcessor"]
