"""Built-in host extensions implemented outside the ACP transport."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from box_agent.api import SessionOpenRequest
from box_agent.tools.registration import MCPToolRegistryController
from box_agent.persistence.workspace_registry import (
    WorkspaceRegistry,
    WorkspaceRegistryError,
)


class SkillListExtension:
    def __init__(self, skill_loader: Any | None) -> None:
        self._skill_loader = skill_loader

    async def handle(self, params: Mapping[str, Any], context: Any) -> dict[str, Any]:
        del params, context
        if self._skill_loader is None:
            return {"skills": []}
        list_metadata = getattr(self._skill_loader, "list_skills_metadata", None)
        return {"skills": list_metadata() if callable(list_metadata) else []}


class WorkspaceListExtension:
    async def handle(self, params: Mapping[str, Any], context: Any) -> dict[str, Any]:
        del params, context
        try:
            registry = WorkspaceRegistry()
            return {
                "workspaces": [profile.to_dict() for profile in registry.list()],
                "configPath": str(registry.path),
            }
        except WorkspaceRegistryError as exc:
            return {"error": str(exc)}


class WorkspaceGetExtension:
    async def handle(self, params: Mapping[str, Any], context: Any) -> dict[str, Any]:
        del context
        path = params.get("path", "")
        if not isinstance(path, str) or not path.strip():
            return {"error": "path is required"}
        try:
            registry = WorkspaceRegistry()
            profile = registry.get(path)
            return {
                "workspace": profile.to_dict() if profile is not None else None,
                "configPath": str(registry.path),
            }
        except WorkspaceRegistryError as exc:
            return {"error": str(exc)}


class WorkspaceSetExtension:
    async def handle(self, params: Mapping[str, Any], context: Any) -> dict[str, Any]:
        del context
        path = params.get("path", "")
        task_type = params.get("taskType", params.get("task_type"))
        if not isinstance(path, str) or not path.strip():
            return {"error": "path is required"}
        try:
            registry = WorkspaceRegistry()
            profile = registry.set(path, task_type)
            return {
                "workspace": profile.to_dict(),
                "configPath": str(registry.path),
            }
        except WorkspaceRegistryError as exc:
            return {"error": str(exc)}


class MCPStatusExtension:
    async def handle(self, params: Mapping[str, Any], context: Any) -> dict[str, Any]:
        del params, context
        from box_agent.tools.mcp_loader import (
            get_mcp_config_path,
            get_mcp_status,
            is_mcp_loading,
        )

        return {
            "servers": get_mcp_status(),
            "loading": is_mcp_loading(),
            "configPath": get_mcp_config_path(),
        }


class UtilityPromptExtension:
    def __init__(self, service: Any) -> None:
        self._service = service

    async def handle(self, params: Mapping[str, Any], context: Any) -> dict[str, Any]:
        del context
        return await self._service.prompt(params)


class PresentationPreflightExtension:
    def __init__(self, service: Any) -> None:
        self._service = service

    async def handle(self, params: Mapping[str, Any], context: Any) -> dict[str, Any]:
        del context
        return await self._service.preflight(dict(params))


async def _session_is_known(params: Mapping[str, Any], context: Any) -> bool:
    session_id = str(params.get("sessionId", "") or "").strip()
    if not session_id:
        return True
    service = getattr(context, "service", None)
    loader = getattr(service, "load_session", None)
    if not callable(loader):
        return True
    try:
        await loader(SessionOpenRequest(session_id=session_id))
    except ValueError:
        return False
    return True


class MemoryProposalListExtension:
    def __init__(self, service: Any) -> None:
        self._service = service

    async def handle(self, params: Mapping[str, Any], context: Any) -> dict[str, Any]:
        if not await _session_is_known(params, context):
            return {"error": "session_not_found"}
        return await self._service.list(params)


class MemoryProposalApplyExtension:
    def __init__(self, service: Any) -> None:
        self._service = service

    async def handle(self, params: Mapping[str, Any], context: Any) -> dict[str, Any]:
        if not await _session_is_known(params, context):
            return {"error": "session_not_found"}
        return await self._service.apply(params)


def _active_run_ids(context: Any) -> tuple[str, ...]:
    service = getattr(context, "service", None)
    getter = getattr(service, "active_run_ids", None)
    if not callable(getter):
        return ()
    return tuple(str(run_id) for run_id in getter())


def _active_run_error(context: Any) -> dict[str, Any] | None:
    active = _active_run_ids(context)
    if not active:
        return None
    return {
        "success": False,
        "error": "agent runs are active; retry after they reach a terminal state",
        "activeRunIds": list(active),
    }


class MCPReconnectExtension:
    def __init__(self, controller: MCPToolRegistryController) -> None:
        self._controller = controller

    async def handle(self, params: Mapping[str, Any], context: Any) -> dict[str, Any]:
        name = str(params.get("name", "") or "").strip()
        if not name:
            return {"success": False, "error": "name is required"}
        if (error := _active_run_error(context)) is not None:
            return error
        from box_agent.tools.mcp_loader import (
            disconnect_mcp_server,
            get_mcp_tools_for_server,
            reconnect_mcp_server,
        )

        result = dict(await reconnect_mcp_server(name))
        tools = get_mcp_tools_for_server(name) if result.get("success") else ()
        try:
            await self._controller.replace_server(name, tools)
        except Exception as exc:
            # The connection and registry are one capability boundary. If the
            # new Tool graph cannot be published, close it and expose neither
            # the stale nor the conflicting generation.
            await disconnect_mcp_server(name)
            await self._controller.remove_server(name)
            return {
                "success": False,
                "error": f"tool registration failed: {type(exc).__name__}: {exc}",
            }
        return result


class MCPDisconnectExtension:
    def __init__(self, controller: MCPToolRegistryController) -> None:
        self._controller = controller

    async def handle(self, params: Mapping[str, Any], context: Any) -> dict[str, Any]:
        name = str(params.get("name", "") or "").strip()
        if not name:
            return {"success": False, "error": "name is required"}
        if (error := _active_run_error(context)) is not None:
            return error
        from box_agent.tools.mcp_loader import disconnect_mcp_server

        result = dict(await disconnect_mcp_server(name))
        await self._controller.remove_server(name)
        return result


__all__ = [
    "MemoryProposalApplyExtension",
    "MemoryProposalListExtension",
    "MCPDisconnectExtension",
    "MCPReconnectExtension",
    "MCPStatusExtension",
    "PresentationPreflightExtension",
    "SkillListExtension",
    "UtilityPromptExtension",
    "WorkspaceGetExtension",
    "WorkspaceListExtension",
    "WorkspaceSetExtension",
]
