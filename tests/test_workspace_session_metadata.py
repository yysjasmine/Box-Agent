"""Workspace profiles contribute Session defaults through the plugin SPI."""

from __future__ import annotations

from box_agent.api import SessionOpenRequest
from box_agent.persistence.workspace_registry import WorkspaceRegistry
from box_agent.services.workspace_profiles import WorkspaceProfileSessionMetadata


async def test_saved_code_workspace_contributes_code_project_defaults(tmp_path):
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    workspace = tmp_path / "project"
    workspace.mkdir()
    registry.set(workspace, "code")
    contributor = WorkspaceProfileSessionMetadata(lambda: registry)

    result = await contributor.contribute(
        SessionOpenRequest(metadata={"workspace_dir": str(workspace)})
    )

    assert result == {"session_mode": "code_agent", "artifact_mode": "project"}


async def test_general_or_unknown_workspace_contributes_no_defaults(tmp_path):
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    workspace = tmp_path / "project"
    workspace.mkdir()
    registry.set(workspace, "general")
    contributor = WorkspaceProfileSessionMetadata(lambda: registry)

    assert await contributor.contribute(
        SessionOpenRequest(metadata={"workspace_dir": str(workspace)})
    ) == {}
    assert await contributor.contribute(
        SessionOpenRequest(metadata={"workspace_dir": str(tmp_path / "missing")})
    ) == {}
