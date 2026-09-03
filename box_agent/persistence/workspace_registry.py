"""Shared workspace metadata persisted for ACP hosts and the CLI."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


WorkspaceTaskType = Literal["general", "code"]
WORKSPACE_CONFIG_SCHEMA_VERSION = 1
_SUPPORTED_TASK_TYPES = {"general", "code"}
_WRITE_LOCK = threading.RLock()


class WorkspaceRegistryError(ValueError):
    """Raised when the shared workspace configuration is invalid."""


@dataclass(frozen=True)
class WorkspaceProfile:
    path: str
    task_type: WorkspaceTaskType
    updated_at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def default_workspace_registry_path(home_dir: Path | None = None) -> Path:
    # Honour the conventional HOME override on every platform.  ACP/CLI test
    # harnesses and hosted desktop shells use it to isolate profiles; on
    # Windows ``Path.home()`` otherwise ignores HOME and leaks one test's
    # registry into the next process.
    resolved_home = home_dir or (
        Path(os.environ["HOME"]) if os.environ.get("HOME") else Path.home()
    )
    return resolved_home / ".box-agent" / "config" / "workspaces.json"


def normalize_workspace_path(value: str | Path) -> str:
    raw = str(value).strip()
    if not raw:
        raise WorkspaceRegistryError("workspace path is required")
    path = Path(raw).expanduser()
    try:
        normalized = path.resolve(strict=False)
    except (OSError, RuntimeError):
        normalized = path.absolute()
    # Preserve the user's spelling in the durable profile.  Windows path
    # comparisons remain case-insensitive at lookup/update time, but lowering
    # the stored value makes diagnostics and host UI unexpectedly lose the
    # original workspace casing.
    return os.path.normpath(str(normalized))


def _same_workspace_path(left: str, right: str) -> bool:
    """Compare normalized paths using the platform's case semantics."""
    return os.path.normcase(left) == os.path.normcase(right)


def _normalize_task_type(value: Any) -> WorkspaceTaskType:
    if value not in _SUPPORTED_TASK_TYPES:
        raise WorkspaceRegistryError("task_type must be 'general' or 'code'")
    return value


def _profile_from_payload(value: Any) -> WorkspaceProfile | None:
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    task_type = value.get("task_type")
    updated_at = value.get("updated_at")
    if not isinstance(path, str) or not path.strip():
        return None
    if task_type not in _SUPPORTED_TASK_TYPES:
        return None
    return WorkspaceProfile(
        path=normalize_workspace_path(path),
        task_type=task_type,
        updated_at=updated_at if isinstance(updated_at, str) else "",
    )


class WorkspaceRegistry:
    """Read and atomically update ``~/.box-agent/config/workspaces.json``."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_workspace_registry_path()

    def _read_payload(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_version": WORKSPACE_CONFIG_SCHEMA_VERSION,
                "workspaces": [],
            }
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceRegistryError(
                f"invalid workspace config: {self.path}"
            ) from exc
        if not isinstance(payload, dict):
            raise WorkspaceRegistryError("workspace config root must be an object")
        if not isinstance(payload.get("workspaces", []), list):
            raise WorkspaceRegistryError("workspaces must be an array")
        return payload

    def _write_payload(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload["schema_version"] = WORKSPACE_CONFIG_SCHEMA_VERSION
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    def list(self) -> list[WorkspaceProfile]:
        payload = self._read_payload()
        profiles = [
            profile
            for item in payload.get("workspaces", [])
            if (profile := _profile_from_payload(item)) is not None
        ]
        return sorted(profiles, key=lambda item: item.updated_at, reverse=True)

    def get(self, workspace_path: str | Path) -> WorkspaceProfile | None:
        target = normalize_workspace_path(workspace_path)
        return next(
            (profile for profile in self.list() if _same_workspace_path(profile.path, target)),
            None,
        )

    def set(
        self,
        workspace_path: str | Path,
        task_type: WorkspaceTaskType,
    ) -> WorkspaceProfile:
        normalized_path = normalize_workspace_path(workspace_path)
        normalized_type = _normalize_task_type(task_type)
        with _WRITE_LOCK:
            payload = self._read_payload()
            raw_workspaces = payload.get("workspaces", [])
            existing_index = next(
                (
                    index
                    for index, item in enumerate(raw_workspaces)
                    if isinstance(item, dict)
                    and isinstance(item.get("path"), str)
                    and _same_workspace_path(
                        normalize_workspace_path(item["path"]), normalized_path
                    )
                ),
                None,
            )
            if existing_index is not None:
                existing = _profile_from_payload(raw_workspaces[existing_index])
                if existing is not None and existing.task_type == normalized_type:
                    return existing

            profile = WorkspaceProfile(
                path=normalized_path,
                task_type=normalized_type,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            if existing_index is None:
                raw_workspaces.append(profile.to_dict())
            else:
                raw_item = raw_workspaces[existing_index]
                raw_workspaces[existing_index] = {
                    **(raw_item if isinstance(raw_item, dict) else {}),
                    **profile.to_dict(),
                }
            payload["workspaces"] = raw_workspaces
            self._write_payload(payload)
            return profile


__all__ = [
    "WORKSPACE_CONFIG_SCHEMA_VERSION",
    "WorkspaceProfile",
    "WorkspaceRegistry",
    "WorkspaceRegistryError",
    "WorkspaceTaskType",
    "default_workspace_registry_path",
    "normalize_workspace_path",
]
