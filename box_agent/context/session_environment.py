"""Host environment and effective permission context contributor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from box_agent.permissions.session import (
    SessionPermissionResolver,
    build_file_access_prompt,
)
from box_agent.tools.runtime import build_skill_runtime_context, build_skill_runtime_prompt

from .api import ContextBuildRequest, ContextItem
from .environment import EnvContext, build_env_context_prompt


class SessionEnvironmentContextContributor:
    """Expose enforced filesystem and host runtime facts to the model."""

    def __init__(self, permission_resolver: SessionPermissionResolver) -> None:
        self._permission_resolver = permission_resolver

    def provide(self, request: ContextBuildRequest) -> tuple[ContextItem, ...]:
        metadata: Mapping[str, Any] = request.metadata
        workspace_value = metadata.get("workspace_dir")
        if not isinstance(workspace_value, str) or not workspace_value.strip():
            return ()

        profile = self._permission_resolver.resolve(
            Path(workspace_value), metadata
        )
        env_context = EnvContext.from_meta(
            metadata.get("env_context", metadata.get("envContext"))
        )
        runtime_context = build_skill_runtime_context(
            sandbox_mode=True,
            env_context=env_context,
        )
        values = (
            ("file-access", build_file_access_prompt(profile), 940),
            ("host-environment", build_env_context_prompt(env_context), 820),
            ("skill-runtime", build_skill_runtime_prompt(runtime_context), 810),
        )
        scope = request.session_id or request.run_id or "run"
        return tuple(
            ContextItem(
                item_id=f"{scope}:context:{suffix}",
                kind="system",
                content=content,
                priority=priority,
                pinned=True,
                metadata={"role": "system", "contributor": "session_environment"},
            )
            for suffix, content, priority in values
            if content.strip()
        )


__all__ = ["SessionEnvironmentContextContributor"]
