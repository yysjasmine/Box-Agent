"""Runtime-owned workflow selection persisted across ACP session boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


OWNER_SCHEMA_VERSION = 1
OWNER_FILENAME = "workflow-owner.json"
REGISTERED_WORKFLOW_KINDS = frozenset({"controlled_presentation", "external_skill"})
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class WorkflowOwner:
    session_id: str
    workflow_kind: str
    workflow_options: dict[str, str | int | bool]
    created_at: str


def default_workflow_owner_root() -> Path:
    configured = os.environ.get("BOX_AGENT_WORKFLOW_OWNER_DIR", "").strip()
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".box-agent" / "sessions"
    )


def _safe_session_segment(session_id: str) -> str:
    normalized = session_id.strip()
    safe = _SAFE_SEGMENT_RE.sub("_", normalized).strip("._-")
    if not safe:
        raise ValueError("A stable session id is required for workflow ownership.")
    if safe != normalized or len(safe) > 120:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
        safe = f"{safe[:100]}-{digest}"
    return safe


def _owner_path(session_id: str, root_dir: str | Path | None) -> Path:
    root = Path(root_dir) if root_dir is not None else default_workflow_owner_root()
    return root.expanduser().resolve() / _safe_session_segment(session_id) / OWNER_FILENAME


def _normalize_options(options: Mapping[str, Any] | None) -> dict[str, str | int | bool]:
    normalized: dict[str, str | int | bool] = {}
    for key, value in (options or {}).items():
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(value, bool | int | str):
            normalized[key.strip()[:128]] = value if not isinstance(value, str) else value[:8_000]
    return normalized


def _payload_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def save_workflow_owner(
    *,
    session_id: str,
    workflow_kind: str | None,
    workflow_options: Mapping[str, Any] | None,
    root_dir: str | Path | None = None,
) -> WorkflowOwner | None:
    """Atomically persist one registered workflow owner for a stable session."""
    if not session_id.strip() or workflow_kind not in REGISTERED_WORKFLOW_KINDS:
        return None
    path = _owner_path(session_id, root_dir)
    created_at = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "schema_version": OWNER_SCHEMA_VERSION,
        "session_id": session_id.strip(),
        "workflow_kind": workflow_kind,
        "workflow_options": _normalize_options(workflow_options),
        "created_at": created_at,
    }
    payload["payload_sha256"] = _payload_digest(payload)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return WorkflowOwner(
        session_id=payload["session_id"],
        workflow_kind=workflow_kind,
        workflow_options=dict(payload["workflow_options"]),
        created_at=created_at,
    )


def load_workflow_owner(
    *,
    session_id: str,
    root_dir: str | Path | None = None,
) -> WorkflowOwner | None:
    """Load a validated runtime-owned workflow selection."""
    if not session_id.strip():
        return None
    path = _owner_path(session_id, root_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    digest = payload.pop("payload_sha256", None)
    if not isinstance(digest, str) or digest != _payload_digest(payload):
        return None
    workflow_kind = payload.get("workflow_kind")
    if (
        payload.get("schema_version") != OWNER_SCHEMA_VERSION
        or payload.get("session_id") != session_id.strip()
        or workflow_kind not in REGISTERED_WORKFLOW_KINDS
        or not isinstance(payload.get("created_at"), str)
        or not isinstance(payload.get("workflow_options"), dict)
    ):
        return None
    options = _normalize_options(payload["workflow_options"])
    return WorkflowOwner(
        session_id=session_id.strip(),
        workflow_kind=workflow_kind,
        workflow_options=options,
        created_at=payload["created_at"],
    )


def clear_workflow_owner(
    *,
    session_id: str,
    root_dir: str | Path | None = None,
) -> bool:
    if not session_id.strip():
        return False
    path = _owner_path(session_id, root_dir)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


__all__ = [
    "WorkflowOwner",
    "clear_workflow_owner",
    "default_workflow_owner_root",
    "load_workflow_owner",
    "save_workflow_owner",
]
