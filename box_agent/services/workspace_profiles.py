"""Workspace-profile defaults exposed through the Session metadata SPI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from box_agent.persistence.workspace_registry import WorkspaceRegistry


class WorkspaceProfileSessionMetadata:
    """Translate a saved workspace task type into runtime-neutral defaults."""

    def __init__(
        self,
        registry_factory: Callable[[], WorkspaceRegistry] = WorkspaceRegistry,
    ) -> None:
        self._registry_factory = registry_factory

    async def contribute(self, request: Any) -> Mapping[str, Any]:
        metadata = getattr(request, "metadata", {})
        workspace = (
            metadata.get("workspace_dir")
            if isinstance(metadata, Mapping)
            else None
        )
        if not isinstance(workspace, str) or not workspace.strip():
            return {}
        profile = await asyncio.to_thread(self._registry_factory().get, workspace)
        if profile is None or profile.task_type != "code":
            return {}
        return {"session_mode": "code_agent", "artifact_mode": "project"}


__all__ = ["WorkspaceProfileSessionMetadata"]
