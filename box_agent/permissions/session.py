"""Deterministic session permission policy composition.

Host adapters pass untrusted session metadata into this resolver.  It produces
one immutable profile consumed by both Context contributors and Tool factories,
so model guidance and enforced filesystem boundaries cannot drift apart.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from box_agent.config import Config
from box_agent.tools.permissions import CapabilityPolicy, GrantStore, PermissionEngine

log = logging.getLogger(__name__)

_ELEVATED_MODES = frozenset({"unrestricted_filesystem", "full_access"})
_VALID_MODES = frozenset({"default", *_ELEVATED_MODES})


@dataclass(frozen=True, slots=True)
class SessionPermissionProfile:
    """Resolved security boundary for one durable Session."""

    permission_mode: str | None
    workspace_dir: Path
    allow_full_access: bool
    bypass_dangerous_command_approval: bool
    policy: CapabilityPolicy | None
    permission_engine: PermissionEngine | None


class SessionPermissionResolver:
    """Resolve config plus host metadata into a fail-closed session profile."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def resolve(
        self,
        workspace_dir: str | Path,
        metadata: Mapping[str, Any] | None,
    ) -> SessionPermissionProfile:
        workspace = Path(workspace_dir).expanduser().resolve()
        meta = metadata if isinstance(metadata, Mapping) else {}
        raw_mode = meta.get("permission_mode", meta.get("permissionMode"))
        permission_mode = self._normalize_mode(raw_mode)

        if permission_mode in _ELEVATED_MODES:
            return SessionPermissionProfile(
                permission_mode=permission_mode,
                workspace_dir=workspace,
                allow_full_access=True,
                bypass_dangerous_command_approval=permission_mode == "full_access",
                policy=None,
                permission_engine=None,
            )

        has_office_policy = bool(getattr(self._config.officev3, "_present", False))
        use_capability_policy = permission_mode == "default" or has_office_policy
        if not use_capability_policy:
            return SessionPermissionProfile(
                permission_mode=None,
                workspace_dir=workspace,
                allow_full_access=bool(self._config.tools.allow_full_access),
                bypass_dangerous_command_approval=False,
                policy=None,
                permission_engine=None,
            )

        policy = (
            CapabilityPolicy.from_config(self._config)
            if has_office_policy
            else CapabilityPolicy()
        )
        if permission_mode == "default":
            policy = policy.with_filesystem_overrides(
                session_workspace_root=str(workspace),
                allowed_directories=(),
                filesystem_scope="session_workspace",
                replace_allowed_directories=True,
            )
        policy = self._apply_host_filesystem_context(
            policy,
            meta,
            replace_allowed_directories=permission_mode == "default",
        )
        engine = PermissionEngine(policy, workspace, grant_store=GrantStore())
        return SessionPermissionProfile(
            permission_mode=permission_mode,
            workspace_dir=workspace,
            # The capability engine is authoritative.  Keep legacy path guards
            # restrictive as a defence in depth instead of configuring a
            # contradictory second source of truth.
            allow_full_access=False,
            bypass_dangerous_command_approval=False,
            policy=policy,
            permission_engine=engine,
        )

    @staticmethod
    def _normalize_mode(raw_mode: Any) -> str | None:
        if raw_mode is None:
            return None
        if isinstance(raw_mode, str) and raw_mode in _VALID_MODES:
            return raw_mode
        log.warning("invalid permission_mode=%r; using fail-closed default", raw_mode)
        return "default"

    @staticmethod
    def _apply_host_filesystem_context(
        policy: CapabilityPolicy,
        metadata: Mapping[str, Any],
        *,
        replace_allowed_directories: bool,
    ) -> CapabilityPolicy:
        raw = metadata.get("filesystem_policy", metadata.get("filesystemPolicy"))
        if not isinstance(raw, Mapping):
            return policy

        workspace_root = raw.get(
            "session_workspace_root", raw.get("sessionWorkspaceRoot")
        )
        if not isinstance(workspace_root, str) or not workspace_root.strip():
            workspace_root = None

        raw_directories = raw.get(
            "allowed_directories", raw.get("allowedDirectories")
        )
        directories = (
            tuple(
                value.strip()
                for value in raw_directories
                if isinstance(value, str) and value.strip()
            )
            if isinstance(raw_directories, (list, tuple))
            else None
        )

        scope = raw.get("filesystem_scope", raw.get("filesystemScope"))
        if not isinstance(scope, str) or not scope.strip():
            scope = None

        return policy.with_filesystem_overrides(
            session_workspace_root=workspace_root,
            allowed_directories=directories,
            filesystem_scope=scope,
            replace_allowed_directories=replace_allowed_directories,
        )


def build_file_access_prompt(profile: SessionPermissionProfile) -> str:
    """Render the effective Tool security boundary for model context."""

    workspace = profile.workspace_dir
    policy = profile.policy
    if policy is None:
        mode = profile.permission_mode or "configured legacy policy"
        return (
            "## File Access Context\n"
            f"- Current workspace: `{workspace}`\n"
            f"- Active permission mode: `{mode}`.\n"
            "- File tools and bash enforce the active runtime policy; try a "
            "specific path instead of assuming access or denial."
        )

    roots = [workspace]
    if policy.session_workspace_root:
        roots.append(Path(policy.session_workspace_root).expanduser())
    roots.extend(Path(value).expanduser() for value in policy.allowed_directories)
    unique_roots = tuple(dict.fromkeys(str(root) for root in roots))

    if policy.filesystem_scope == "user_home":
        scope = "paths under the user home directory are pre-authorized"
    elif policy.filesystem_scope in {"session_workspace", "custom"}:
        scope = "the listed workspace roots are pre-authorized"
    else:
        scope = "the unknown scope fails closed"
    root_lines = "\n".join(f"- `{root}`" for root in unique_roots)
    return (
        "## File Access Context\n"
        f"- Active filesystem scope: `{policy.filesystem_scope}`; {scope}.\n"
        "- Pre-authorized roots:\n"
        f"{root_lines}\n"
        "- These roots are not every path that may be requested. When needed, "
        "try one specific narrow path and let the runtime request permission.\n"
        "- A denial applies only to that path; do not generalize it to other paths."
    )


__all__ = [
    "SessionPermissionProfile",
    "SessionPermissionResolver",
    "build_file_access_prompt",
]
