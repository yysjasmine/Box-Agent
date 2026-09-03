"""Trusted, durable checkpoints for recoverable workflow pauses.

The registry in this module is deliberately built in. Third-party Skills use
the registered generic external adapter; only trusted runtime routing may
select another built-in workflow kind. Skills cannot provide executable
checkpoint code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .artifacts import artifact_scan_root

_log = logging.getLogger(__name__)

CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_DIRECTORY = Path(".box-agent") / "checkpoints"
_SAFE_KIND_RE = re.compile(r"[^a-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class WorkflowPauseCheckpoint:
    """Validated metadata emitted when a workflow pauses for context safety."""

    checkpoint_id: str
    workflow_kind: str
    adapter_id: str
    schema_version: int
    workspace_identity: str
    path: str
    stage: str | None
    artifact_count: int
    artifact_set_sha256: str
    workflow_options: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkpointId": self.checkpoint_id,
            "workflowKind": self.workflow_kind,
            "adapterId": self.adapter_id,
            "schemaVersion": self.schema_version,
            "workspaceIdentity": self.workspace_identity,
            "path": self.path,
            "stage": self.stage,
            "artifactCount": self.artifact_count,
            "artifactSetSha256": self.artifact_set_sha256,
            "workflowOptions": dict(self.workflow_options),
        }


class WorkflowCheckpointAdapter(Protocol):
    """Trusted adapter contract. Implementations live in Box-Agent core."""

    adapter_id: str
    workflow_kind: str

    def build_state(
        self,
        policy: Any,
        *,
        workspace_dir: Path,
        artifact_root_dir: Path | None,
    ) -> dict[str, Any]: ...


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_identity(workspace_dir: Path) -> str:
    return hashlib.sha256(os.fsencode(str(workspace_dir))).hexdigest()


def _payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _checkpoint_path(workspace_dir: Path, workflow_kind: str) -> Path:
    safe_kind = _SAFE_KIND_RE.sub("-", workflow_kind.casefold()).strip("-._")
    if not safe_kind:
        raise ValueError("Workflow kind cannot be represented as a checkpoint filename.")
    return workspace_dir / CHECKPOINT_DIRECTORY / f"{safe_kind}.json"


def _artifact_manifest(root: Path | None) -> tuple[list[dict[str, Any]], str]:
    if root is None or not root.is_dir():
        return [], hashlib.sha256(b"").hexdigest()
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            relative_path = path.relative_to(root).as_posix()
            stat = path.stat()
            files.append(
                {
                    "path": relative_path,
                    "size": stat.st_size,
                    "sha256": _sha256_file(path),
                }
            )
        except OSError as exc:
            raise OSError(f"Unable to hash workflow artifact {path}: {exc}") from exc
    aggregate = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return files, aggregate


class _ControlledPresentationCheckpointAdapter:
    adapter_id = "box-agent.controlled-presentation.v1"
    workflow_kind = "controlled_presentation"

    def build_state(
        self,
        policy: Any,
        *,
        workspace_dir: Path,
        artifact_root_dir: Path | None,
    ) -> dict[str, Any]:
        checkpoint_text = policy.build_checkpoint()
        if not isinstance(checkpoint_text, str) or not checkpoint_text.strip():
            raise ValueError("The presentation workflow has no recoverable filesystem state.")
        update = policy.update_checkpoint(checkpoint_text)
        artifact_root = artifact_scan_root(workspace_dir, artifact_root_dir)
        artifacts, artifact_set_sha256 = _artifact_manifest(artifact_root)
        return {
            "stage": getattr(policy, "stage", None),
            "checkpoint_text_sha256": hashlib.sha256(
                update.text.encode("utf-8")
            ).hexdigest(),
            "artifact_root": str(artifact_root) if artifact_root is not None else None,
            "artifacts": artifacts,
            "artifact_set_sha256": artifact_set_sha256,
        }


class _ExternalSkillCheckpointAdapter:
    adapter_id = "box-agent.external-skill.v1"
    workflow_kind = "external_skill"

    def build_state(
        self,
        policy: Any,
        *,
        workspace_dir: Path,
        artifact_root_dir: Path | None,
    ) -> dict[str, Any]:
        checkpoint_text = policy.build_checkpoint()
        if not isinstance(checkpoint_text, str) or not checkpoint_text.strip():
            raise ValueError("The external Skill has no recoverable checkpoint state.")
        update = policy.update_checkpoint(checkpoint_text)
        artifact_root = artifact_scan_root(workspace_dir, artifact_root_dir)
        artifacts, artifact_set_sha256 = _artifact_manifest(artifact_root)
        raw_options = policy.checkpoint_options()
        workflow_options = {
            str(key): str(value)
            for key, value in raw_options.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        return {
            "stage": getattr(policy, "stage", None),
            "checkpoint_text_sha256": hashlib.sha256(
                update.text.encode("utf-8")
            ).hexdigest(),
            "artifact_root": str(artifact_root) if artifact_root is not None else None,
            "artifacts": artifacts,
            "artifact_set_sha256": artifact_set_sha256,
            "workflow_options": workflow_options,
        }


_ADAPTERS: dict[str, WorkflowCheckpointAdapter] = {
    _ControlledPresentationCheckpointAdapter.workflow_kind: (
        _ControlledPresentationCheckpointAdapter()
    ),
    _ExternalSkillCheckpointAdapter.workflow_kind: _ExternalSkillCheckpointAdapter(),
}


def save_workflow_checkpoint(
    policy: Any,
    *,
    workspace_dir: str | Path | None,
    artifact_root_dir: str | Path | None,
) -> WorkflowPauseCheckpoint | None:
    """Atomically persist a checkpoint for a trusted, registered workflow."""
    workflow_kind = getattr(policy, "kind", None)
    adapter = _ADAPTERS.get(workflow_kind)
    if adapter is None or workspace_dir is None:
        return None
    workspace = Path(workspace_dir).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Workflow workspace does not exist: {workspace}")
    artifact_root = (
        Path(artifact_root_dir).expanduser().resolve()
        if artifact_root_dir is not None
        else None
    )
    state = adapter.build_state(
        policy,
        workspace_dir=workspace,
        artifact_root_dir=artifact_root,
    )
    checkpoint_id = uuid4().hex
    path = _checkpoint_path(workspace, workflow_kind)
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "checkpoint_id": checkpoint_id,
        "workflow_kind": workflow_kind,
        "adapter_id": adapter.adapter_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "workspace_path": str(workspace),
        "workspace_identity": _workspace_identity(workspace),
        "state": state,
    }
    payload["payload_sha256"] = _payload_sha256(payload)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Windows and some filesystems do not permit fsync on directories.
            pass
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return WorkflowPauseCheckpoint(
        checkpoint_id=checkpoint_id,
        workflow_kind=workflow_kind,
        adapter_id=adapter.adapter_id,
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        workspace_identity=payload["workspace_identity"],
        path=str(path),
        stage=state.get("stage") if isinstance(state.get("stage"), str) else None,
        artifact_count=len(state.get("artifacts", [])),
        artifact_set_sha256=str(state.get("artifact_set_sha256") or ""),
        workflow_options=dict(state.get("workflow_options") or {}),
    )


def load_workflow_checkpoint(
    *,
    workspace_dir: str | Path | None,
    workflow_kind: str | None,
) -> WorkflowPauseCheckpoint | None:
    """Load and validate durable metadata for a new process/session boundary."""
    adapter = _ADAPTERS.get(workflow_kind or "")
    if adapter is None or workspace_dir is None:
        return None
    workspace = Path(workspace_dir).expanduser().resolve()
    path = _checkpoint_path(workspace, adapter.workflow_kind)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("checkpoint root must be an object")
        stored_payload_hash = payload.get("payload_sha256")
        hash_input = dict(payload)
        hash_input.pop("payload_sha256", None)
        if (
            not isinstance(stored_payload_hash, str)
            or stored_payload_hash != _payload_sha256(hash_input)
        ):
            raise ValueError("checkpoint payload hash mismatch")
        if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported checkpoint schema version")
        if payload.get("workflow_kind") != adapter.workflow_kind:
            raise ValueError("workflow kind mismatch")
        if payload.get("adapter_id") != adapter.adapter_id:
            raise ValueError("checkpoint adapter mismatch")
        if payload.get("workspace_path") != str(workspace):
            raise ValueError("workspace path mismatch")
        identity = _workspace_identity(workspace)
        if payload.get("workspace_identity") != identity:
            raise ValueError("workspace identity mismatch")
        state = payload.get("state")
        if not isinstance(state, dict):
            raise ValueError("checkpoint state must be an object")
        checkpoint_id = payload.get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise ValueError("checkpoint id is missing")
        artifacts = state.get("artifacts")
        if not isinstance(artifacts, list):
            raise ValueError("artifact manifest is missing")
        artifact_set_sha256 = state.get("artifact_set_sha256")
        if not isinstance(artifact_set_sha256, str) or not artifact_set_sha256:
            raise ValueError("artifact set hash is missing")
        if artifact_set_sha256 != hashlib.sha256(
            json.dumps(artifacts, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest():
            raise ValueError("artifact manifest hash mismatch")
        raw_workflow_options = state.get("workflow_options", {})
        if not isinstance(raw_workflow_options, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in raw_workflow_options.items()
        ):
            raise ValueError("workflow options must contain string data only")
        return WorkflowPauseCheckpoint(
            checkpoint_id=checkpoint_id,
            workflow_kind=adapter.workflow_kind,
            adapter_id=adapter.adapter_id,
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            workspace_identity=identity,
            path=str(path),
            stage=state.get("stage") if isinstance(state.get("stage"), str) else None,
            artifact_count=len(artifacts),
            artifact_set_sha256=artifact_set_sha256,
            workflow_options=dict(raw_workflow_options),
        )
    except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
        _log.warning(
            "workflow_checkpoint/load_rejected path=%s error=%s",
            path,
            exc,
        )
        return None


def clear_workflow_checkpoint(
    *,
    workspace_dir: str | Path | None,
    workflow_kind: str | None,
) -> bool:
    """Remove a completed workflow's durable pause marker."""
    if workflow_kind not in _ADAPTERS or workspace_dir is None:
        return False
    workspace = Path(workspace_dir).expanduser().resolve()
    path = _checkpoint_path(workspace, workflow_kind)
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        _log.warning(
            "workflow_checkpoint/clear_failed path=%s error=%s",
            path,
            exc,
        )
        return False


def checkpoint_resume_instruction(checkpoint: WorkflowPauseCheckpoint) -> str:
    """Build a trusted, bounded instruction for a newly created session."""
    stage = checkpoint.stage or "filesystem_recheck"
    return (
        "[BOX_AGENT_WORKFLOW_RESUME]\n"
        f"checkpoint_id={checkpoint.checkpoint_id}\n"
        f"workflow={checkpoint.workflow_kind}\n"
        f"stage={stage}\n"
        "A previous process paused after saving a durable checkpoint. Re-derive the "
        "current stage from canonical workspace artifacts, preserve completed work, "
        "and continue from the next required action."
    )


__all__ = [
    "CHECKPOINT_DIRECTORY",
    "CHECKPOINT_SCHEMA_VERSION",
    "WorkflowCheckpointAdapter",
    "WorkflowPauseCheckpoint",
    "checkpoint_resume_instruction",
    "clear_workflow_checkpoint",
    "load_workflow_checkpoint",
    "save_workflow_checkpoint",
]
