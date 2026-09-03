"""Run-bound Tool Engine factories for session-isolated host workspaces."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from box_agent.plugins import PluginHost

from .engine import RegistryToolEngine
from .registration import register_tool_plugins


@dataclass(frozen=True, slots=True)
class _SessionToolSet:
    workspace_dir: str
    tools: tuple[Any, ...]


class SessionScopedToolEngineFactory:
    """Build one Tool Engine per Run over session-owned Tool instances.

    Workspace-dependent Tool objects are created once per Session and never
    shared with another Session. Global plugins (including hot-reloaded MCP
    tools) are resolved again for every Run. The session-to-workspace binding
    is immutable, which prevents a resumed session from silently changing its
    filesystem security boundary.
    """

    def __init__(
        self,
        *,
        base_tools_provider: Callable[[], Iterable[Any]],
        workspace_tools_builder: Callable[[Any], Iterable[Any]],
        session_tool_contributors_provider: Callable[[], Iterable[Any]] | None = None,
        permission_gateway: Any | None = None,
        effect_ledger: Any | None = None,
        hooks: Iterable[Any] = (),
        hooks_provider: Callable[[], Iterable[Any]] | None = None,
        defer_effect_prepare: bool = False,
        defer_effect_completion: bool = False,
    ) -> None:
        if not callable(base_tools_provider):
            raise ValueError("base_tools_provider must be callable")
        if not callable(workspace_tools_builder):
            raise ValueError("workspace_tools_builder must be callable")
        self._base_tools_provider = base_tools_provider
        self._workspace_tools_builder = workspace_tools_builder
        self._session_tool_contributors_provider = session_tool_contributors_provider
        self._permission_gateway = permission_gateway
        self._effect_ledger = effect_ledger
        self._hooks = tuple(hooks)
        self._hooks_provider = hooks_provider
        self._defer_effect_prepare = bool(defer_effect_prepare)
        self._defer_effect_completion = bool(defer_effect_completion)
        self._sessions: dict[str, _SessionToolSet] = {}

    def for_run(self, request: Any, bundle: Any | None = None) -> RegistryToolEngine:
        del bundle
        session_id = str(getattr(request, "session_id", "") or "")
        if not session_id:
            raise ValueError("session_id is required for session-scoped tools")
        metadata = getattr(request, "metadata", {})
        workspace_dir = str(
            metadata.get("workspace_dir", "")
            if hasattr(metadata, "get")
            else ""
        )
        if not workspace_dir:
            raise ValueError("workspace_dir is required for session-scoped tools")

        session_tools = self._sessions.get(session_id)
        if session_tools is None:
            contributed_tools: list[Any] = []
            contributors = (
                tuple(self._session_tool_contributors_provider())
                if self._session_tool_contributors_provider is not None
                else ()
            )
            for contributor in contributors:
                provide_tools = getattr(contributor, "provide_tools", None)
                if not callable(provide_tools):
                    raise TypeError(
                        "session Tool contributor must provide provide_tools(request)"
                    )
                values = provide_tools(request)
                if not isinstance(values, (list, tuple)):
                    raise TypeError(
                        "session Tool contributor must return a finite Tool sequence"
                    )
                contributed_tools.extend(values)
            session_tools = _SessionToolSet(
                workspace_dir=workspace_dir,
                tools=(
                    *tuple(self._workspace_tools_builder(request)),
                    *tuple(contributed_tools),
                ),
            )
            self._sessions[session_id] = session_tools
        elif session_tools.workspace_dir != workspace_dir:
            raise ValueError(
                f"session {session_id!r} workspace is immutable: "
                f"{session_tools.workspace_dir!r} != {workspace_dir!r}"
            )

        tools = _overlay_tools(
            tuple(self._base_tools_provider()),
            session_tools.tools,
        )
        host = PluginHost()
        register_tool_plugins(host, tools)
        return RegistryToolEngine(
            host.registries["tools.executors"],
            descriptor_registry=host.registries["tools.descriptors"],
            permission_gateway=self._permission_gateway,
            effect_ledger=self._effect_ledger,
            hooks=(
                tuple(self._hooks_provider())
                if self._hooks_provider is not None
                else self._hooks
            ),
            defer_effect_prepare=self._defer_effect_prepare,
            defer_effect_completion=self._defer_effect_completion,
        )

    def release_session(self, session_id: str) -> None:
        """Drop cached Tool instances after the owning Session is closed."""

        self._sessions.pop(str(session_id or ""), None)

    def on_session_close(self, session_id: str) -> None:
        """Implement the host-neutral ``SessionLifecycle`` plugin contract."""

        self.release_session(session_id)


def _overlay_tools(
    base_tools: tuple[Any, ...],
    session_tools: tuple[Any, ...],
) -> tuple[Any, ...]:
    """Let session-owned canonical Tool names replace global previews."""

    session_names = {
        str(getattr(tool, "name", "") or "").strip()
        for tool in session_tools
    }
    values: list[Any] = []
    seen_objects: set[int] = set()
    for tool in base_tools:
        if id(tool) in seen_objects:
            continue
        name = str(getattr(tool, "name", "") or "").strip()
        if name in session_names:
            continue
        seen_objects.add(id(tool))
        values.append(tool)
    for tool in session_tools:
        if id(tool) in seen_objects:
            continue
        seen_objects.add(id(tool))
        values.append(tool)
    return tuple(values)


__all__ = ["SessionScopedToolEngineFactory"]
