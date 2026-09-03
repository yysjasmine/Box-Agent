"""Session-scoped workspace Tool plugin composition."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from box_agent.context.environment import EnvContext
from box_agent.permissions import SessionPermissionResolver

from .runtime import build_skill_runtime_context
from .setup import add_workspace_tools


class SessionWorkspaceToolBuilder:
    """Build the enforced Tool set from one durable Session contract."""

    def __init__(
        self,
        *,
        config: Any,
        default_workspace: Path,
        permission_resolver: SessionPermissionResolver,
        llm: Any,
        output: Callable[[str], None],
        skill_loader: Any | None = None,
        capability_state_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._config = config
        self._default_workspace = default_workspace.resolve()
        self._permission_resolver = permission_resolver
        self._llm = llm
        self._output = output
        self._skill_loader = skill_loader
        self._capability_state_provider = capability_state_provider

    def __call__(self, request: Any) -> tuple[Any, ...]:
        raw_metadata = getattr(request, "metadata", {})
        metadata: Mapping[str, Any] = (
            raw_metadata if isinstance(raw_metadata, Mapping) else {}
        )
        workspace_value = metadata.get("workspace_dir", self._default_workspace)
        workspace = Path(str(workspace_value)).expanduser().resolve()
        permission = self._permission_resolver.resolve(workspace, metadata)
        env_context = EnvContext.from_meta(
            metadata.get("env_context", metadata.get("envContext"))
        )
        runtime_context = build_skill_runtime_context(
            sandbox_mode=True,
            env_context=env_context,
        )

        layout = metadata.get("workspace_layout", metadata.get("workspaceLayout", {}))
        if not isinstance(layout, Mapping):
            layout = {}
        artifact_root = layout.get("artifact_root_dir", layout.get("artifactRootDir"))
        artifact_mode = str(
            metadata.get("artifact_mode", metadata.get("artifactMode", "output"))
        ).strip().lower()
        use_output_dir = artifact_mode != "project"
        session_id = str(getattr(request, "session_id", "") or "")

        tools: list[Any] = []
        add_workspace_tools(
            tools,
            self._config,
            workspace,
            sandbox_mode=True,
            allow_full_access=permission.allow_full_access,
            non_interactive=True,
            output=self._output,
            llm=self._llm,
            permission_engine=permission.permission_engine,
            skill_runtime_context=runtime_context,
            skill_loader=self._skill_loader,
            capability_state_provider=self._capability_state_provider,
            use_output_dir=use_output_dir,
            artifact_root_dir=artifact_root,
            create_artifact_root=use_output_dir,
            skill_scratch_root_dir=(
                workspace / ".box-agent" / "scratch" / session_id
                if not use_output_dir and session_id
                else None
            ),
            env_context=env_context,
            process_owner_id=session_id or None,
            bypass_dangerous_command_approval=(
                permission.bypass_dangerous_command_approval
            ),
        )
        return tuple(
            tool
            for tool in tools
            if str(getattr(tool, "name", ""))
            not in {"goal_read", "goal_write", "plan_read", "plan_write"}
        )


__all__ = ["SessionWorkspaceToolBuilder"]
