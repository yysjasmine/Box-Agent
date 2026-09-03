"""Tool capability registration and hot-reload lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable
from typing import Any

from box_agent.plugins import PluginConflictError, PluginHost

from .base import tool_call_name_variants


MCP_SOURCE_PREFIX = "mcp.server:"
_TOOL_REGISTRY_NAMES = ("tools.executors",)


def tool_registration_source(tool: Any, *, default: str) -> str:
    """Return the lifecycle owner for one concrete tool instance."""

    server_name = str(getattr(tool, "server_name", "") or "").strip()
    return f"{MCP_SOURCE_PREFIX}{server_name}" if server_name else default


def tool_bindings(tools: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Normalize Tool names and aliases into source-owned generations."""

    generations: dict[str, dict[str, Any]] = {}
    seen_objects: set[int] = set()
    claimed: dict[str, Any] = {}
    for tool in tools:
        object_id = id(tool)
        if object_id in seen_objects:
            continue
        seen_objects.add(object_id)
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            continue
        aliases = tuple(getattr(tool, "aliases", ()) or ())
        normalized_aliases = tuple(str(alias).strip() for alias in aliases)
        if any(not alias for alias in normalized_aliases):
            raise ValueError(f"tool '{name}' declares an empty alias")
        if name in normalized_aliases:
            raise ValueError(f"tool '{name}' repeats its canonical name as an alias")
        if len(set(normalized_aliases)) != len(normalized_aliases):
            raise ValueError(f"tool '{name}' declares a duplicate alias")
        source = tool_registration_source(tool, default="box_agent.tools")
        generation = generations.setdefault(source, {})
        declared_names = (name, *normalized_aliases)
        for declared_name in declared_names:
            for key in tool_call_name_variants(str(declared_name)):
                owner = claimed.get(key)
                if owner is not None and owner is not tool:
                    raise PluginConflictError(
                        f"tool key '{key}' is claimed by distinct tool objects"
                    )
                claimed[key] = tool
                generation[key] = tool
    return generations


def register_tool_plugins(host: PluginHost, tools: Iterable[Any]) -> None:
    """Register the initial complete Tool graph in its canonical registry."""

    generations = tool_bindings(tools)
    registries = tuple(host.registries[name] for name in _TOOL_REGISTRY_NAMES)
    for registry in registries:
        for source, values in generations.items():
            registry.validate_source_replacement(
                tuple(values), source=source, scope="global"
            )
    for registry in registries:
        for source, values in generations.items():
            registry.replace_source(
                values,
                source=source,
                version=_binding_version(values),
            )


def _binding_version(values: dict[str, Any]) -> str:
    """Return a stable replay fence for a Tool generation's public contract."""

    tools = {id(tool): tool for tool in values.values()}.values()
    contracts = []
    for tool in tools:
        contracts.append(
            {
                "name": str(getattr(tool, "name", "") or ""),
                "aliases": sorted(
                    str(alias) for alias in tuple(getattr(tool, "aliases", ()) or ())
                ),
                "description": str(getattr(tool, "description", "") or ""),
                "parameters": getattr(tool, "parameters", {}) or {},
            }
        )
    encoded = json.dumps(
        sorted(contracts, key=lambda item: item["name"]),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class MCPToolRegistryController:
    """Atomically publish one MCP server generation to the Agent Kernel."""

    def __init__(self, host: PluginHost) -> None:
        self._registries = tuple(
            host.registries[name] for name in _TOOL_REGISTRY_NAMES
        )
        self._lock = asyncio.Lock()

    async def replace_server(self, server_name: str, tools: Iterable[Any]) -> None:
        name = str(server_name or "").strip()
        if not name:
            raise ValueError("MCP server name is required")
        source = f"{MCP_SOURCE_PREFIX}{name}"
        generations = tool_bindings(tuple(tools))
        values = generations.pop(source, {})
        if generations:
            raise ValueError("MCP reload returned tools owned by another server")
        async with self._lock:
            for registry in self._registries:
                registry.validate_source_replacement(
                    tuple(values), source=source, scope="global"
                )
            # No await occurs between validation and both map replacements, so
            # another coroutine cannot interleave a conflicting generation.
            for registry in self._registries:
                registry.replace_source(
                    values,
                    source=source,
                    version=_binding_version(values),
                )

    async def remove_server(self, server_name: str) -> None:
        await self.replace_server(server_name, ())


__all__ = [
    "MCPToolRegistryController",
    "MCP_SOURCE_PREFIX",
    "register_tool_plugins",
    "tool_bindings",
    "tool_registration_source",
]
