from __future__ import annotations

from pathlib import Path

import pytest

from box_agent.config import (
    AgentConfig,
    Config,
    FilesystemPermissions,
    LLMConfig,
    Officev3Config,
    Officev3Paths,
    Officev3Permissions,
    ToolsConfig,
)
from box_agent.context import ContextBuildRequest, SessionEnvironmentContextContributor
from box_agent.permissions import SessionPermissionResolver
from box_agent.tools.session_workspace import SessionWorkspaceToolBuilder


def _config(
    workspace: Path,
    *,
    allow_full_access: bool = True,
    officev3: Officev3Config | None = None,
) -> Config:
    return Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(workspace)),
        tools=ToolsConfig(allow_full_access=allow_full_access),
        officev3=officev3 or Officev3Config(),
    )


def test_explicit_default_permission_mode_replaces_global_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    global_allowed = tmp_path / "global"
    session_allowed = tmp_path / "session"
    officev3 = Officev3Config(
        permissions=Officev3Permissions(
            filesystem=FilesystemPermissions(
                scope="user_home",
                allowed_directories=[str(global_allowed)],
            )
        ),
        paths=Officev3Paths(session_workspace_root=str(tmp_path / "legacy")),
    )
    officev3._present = True

    profile = SessionPermissionResolver(
        _config(workspace, officev3=officev3)
    ).resolve(
        workspace,
        {
            "permission_mode": "default",
            "filesystem_policy": {
                "filesystem_scope": "session_workspace",
                "session_workspace_root": str(workspace),
                "allowed_directories": [str(session_allowed)],
            },
        },
    )

    assert profile.permission_mode == "default"
    assert profile.allow_full_access is False
    assert profile.permission_engine is not None
    assert profile.policy is not None
    assert profile.policy.filesystem_scope == "session_workspace"
    assert profile.policy.session_workspace_root == str(workspace)
    assert profile.policy.allowed_directories == (str(session_allowed),)


@pytest.mark.parametrize("invalid_mode", [[], {}, 1, "unknown"])
def test_invalid_permission_mode_fails_closed(
    tmp_path: Path, invalid_mode: object
) -> None:
    workspace = tmp_path / "workspace"
    profile = SessionPermissionResolver(
        _config(workspace, allow_full_access=True)
    ).resolve(workspace, {"permission_mode": invalid_mode})

    assert profile.permission_mode == "default"
    assert profile.allow_full_access is False
    assert profile.permission_engine is not None
    assert profile.policy is not None
    assert profile.policy.filesystem_scope == "session_workspace"
    assert profile.policy.session_workspace_root == str(workspace.resolve())


@pytest.mark.parametrize(
    ("mode", "bypass"),
    [("unrestricted_filesystem", False), ("full_access", True)],
)
def test_elevated_permission_modes_are_explicit_and_distinct(
    tmp_path: Path, mode: str, bypass: bool
) -> None:
    workspace = tmp_path / "workspace"
    profile = SessionPermissionResolver(
        _config(workspace, allow_full_access=False)
    ).resolve(workspace, {"permission_mode": mode})

    assert profile.allow_full_access is True
    assert profile.bypass_dangerous_command_approval is bypass
    assert profile.permission_engine is None
    assert profile.policy is None


def test_omitted_mode_preserves_configured_office_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    allowed = tmp_path / "allowed"
    officev3 = Officev3Config(
        permissions=Officev3Permissions(
            filesystem=FilesystemPermissions(
                scope="custom", allowed_directories=[str(allowed)]
            )
        ),
        paths=Officev3Paths(session_workspace_root=str(workspace)),
    )
    officev3._present = True

    profile = SessionPermissionResolver(
        _config(workspace, officev3=officev3)
    ).resolve(workspace, {})

    assert profile.permission_mode is None
    assert profile.allow_full_access is False
    assert profile.permission_engine is not None
    assert profile.policy is not None
    assert profile.policy.filesystem_scope == "custom"
    assert profile.policy.allowed_directories == (str(allowed),)


def test_malformed_filesystem_policy_cannot_expand_access(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    profile = SessionPermissionResolver(
        _config(workspace, allow_full_access=True)
    ).resolve(
        workspace,
        {
            "permission_mode": "default",
            "filesystem_policy": {
                "filesystem_scope": ["user_home"],
                "session_workspace_root": 42,
                "allowed_directories": "C:/everywhere",
            },
        },
    )

    assert profile.policy is not None
    assert profile.policy.filesystem_scope == "session_workspace"
    assert profile.policy.session_workspace_root == str(workspace.resolve())
    assert profile.policy.allowed_directories == ()


def test_environment_context_contributor_uses_same_permission_resolution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    contributor = SessionEnvironmentContextContributor(
        SessionPermissionResolver(_config(workspace, allow_full_access=True))
    )
    request = ContextBuildRequest(
        session_id="session-1",
        run_id="run-1",
        items=(),
        token_budget=10_000,
        metadata={
            "workspace_dir": str(workspace),
            "permission_mode": "default",
            "env_context": {"platform": "win32"},
        },
    )

    items = contributor.provide(request)
    content = "\n".join(item.content for item in items)

    assert "## File Access Context" in content
    assert str(workspace.resolve()) in content
    assert "## 当前用户环境" in content
    assert "win32" in content
    assert "## Skill Runtime Context" in content


def test_workspace_tool_builder_applies_resolved_permission_profile(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    config = _config(workspace, allow_full_access=True)
    builder = SessionWorkspaceToolBuilder(
        config=config,
        default_workspace=workspace,
        permission_resolver=SessionPermissionResolver(config),
        llm=None,
        output=lambda _message: None,
    )

    tools = builder(
        type(
            "Request",
            (),
            {
                "session_id": "session-1",
                "metadata": {
                    "workspace_dir": str(workspace),
                    "permission_mode": "default",
                    "artifact_mode": "project",
                },
            },
        )()
    )
    bash = next(tool for tool in tools if getattr(tool, "name", "") == "bash")

    assert bash.allow_full_access is False
    assert bash.non_interactive is True
    assert bash._perm is not None
    assert bash._perm.policy.session_workspace_root == str(workspace.resolve())


def test_workspace_tool_builder_distinguishes_full_access_from_unrestricted(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    config = _config(workspace, allow_full_access=False)
    builder = SessionWorkspaceToolBuilder(
        config=config,
        default_workspace=workspace,
        permission_resolver=SessionPermissionResolver(config),
        llm=None,
        output=lambda _message: None,
    )

    def bash_for(mode: str):
        tools = builder(
            type(
                "Request",
                (),
                {
                    "session_id": f"session-{mode}",
                    "metadata": {
                        "workspace_dir": str(workspace),
                        "permission_mode": mode,
                        "artifact_mode": "project",
                    },
                },
            )()
        )
        return next(tool for tool in tools if getattr(tool, "name", "") == "bash")

    unrestricted = bash_for("unrestricted_filesystem")
    full = bash_for("full_access")
    assert unrestricted.allow_full_access is True
    assert unrestricted.bypass_dangerous_command_approval is False
    assert full.allow_full_access is True
    assert full.bypass_dangerous_command_approval is True
