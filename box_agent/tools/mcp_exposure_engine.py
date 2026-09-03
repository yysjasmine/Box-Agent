"""Session-scoped Tool Engine for deferred MCP capability exposure."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from box_agent.api import ErrorCode, ErrorInfo, ToolCallRequest, ToolCallResult
from box_agent.plugins import PluginHost

from .engine import RegistryToolEngine
from .mcp_tool_search import MCPToolExposureManager, ToolExposure
from .registration import register_tool_plugins


class MCPToolExposureEngine:
    """Expose only the MCP tools activated for one Agent session.

    The process-wide MCP catalog owns discovery. This engine owns the
    provider-visible snapshot for one session and rebuilds its registry-backed
    executor only when that snapshot changes. Permission checks, hooks, Tool
    normalization, and execution therefore remain on the canonical Tool
    Engine path instead of creating a protocol-specific execution path.
    """

    def __init__(
        self,
        candidates: Iterable[Any],
        exposure_manager: MCPToolExposureManager,
        *,
        permission_gateway: Any | None = None,
        effect_ledger: Any | None = None,
        hooks: Iterable[Any] = (),
    ) -> None:
        self._candidates = tuple(candidates)
        self._exposure_manager = exposure_manager
        self._permission_gateway = permission_gateway
        self._effect_ledger = effect_ledger
        self._hooks = tuple(hooks)
        self._engine: RegistryToolEngine | None = None
        self._exposure: ToolExposure | None = None
        self._snapshot_key: tuple[tuple[str, int, int | None], ...] = ()
        self._offered_generations: dict[str, int] = {}
        self._prepared_engines: dict[str, RegistryToolEngine] = {}

    def _refresh(self) -> RegistryToolEngine:
        exposure = self._exposure_manager.prepare_tools(list(self._candidates))
        snapshot_key = tuple(
            (
                str(getattr(tool, "name", "") or ""),
                id(tool),
                exposure.mcp_generations.get(
                    str(getattr(tool, "name", "") or "")
                ),
            )
            for tool in exposure.tools
        )
        if self._engine is not None and snapshot_key == self._snapshot_key:
            self._exposure = exposure
            return self._engine

        host = PluginHost()
        register_tool_plugins(host, exposure.tools)
        self._engine = RegistryToolEngine(
            host.registries["tools.executors"],
            descriptor_registry=host.registries["tools.descriptors"],
            permission_gateway=self._permission_gateway,
            effect_ledger=self._effect_ledger,
            hooks=self._hooks,
            defer_effect_prepare=self._effect_ledger is not None,
            defer_effect_completion=self._effect_ledger is not None,
        )
        self._exposure = exposure
        self._snapshot_key = snapshot_key
        return self._engine

    def schemas(self) -> tuple[Mapping[str, Any], ...]:
        engine = self._refresh()
        assert self._exposure is not None
        self._offered_generations = dict(self._exposure.mcp_generations)
        return engine.schemas()

    def canonicalize_call(self, request: ToolCallRequest) -> ToolCallRequest:
        return self._refresh().canonicalize_call(request)

    def supports_workflow_action(self, tool_name: str, capability: str) -> bool:
        return self._refresh().supports_workflow_action(tool_name, capability)

    def unoffered_call_error(self, tool_name: str) -> str:
        return f"Tool '{tool_name}' was not offered in this model step."

    def _generation_error(self, request: ToolCallRequest) -> ToolCallResult | None:
        offered_generation = self._offered_generations.get(request.tool_name)
        target = None
        if self._exposure is not None:
            target = next(
                (
                    tool
                    for tool in self._exposure.tools
                    if str(getattr(tool, "name", "") or "")
                    == request.tool_name
                ),
                None,
            )
        message = self._exposure_manager.validate_call(
            request.tool_name,
            offered_generation,
            target,
        )
        if message is None:
            return None
        return ToolCallResult(
            call_id=request.call_id,
            status="failed",
            error=ErrorInfo(
                code=ErrorCode.TOOL_NOT_FOUND,
                category="tool",
                message=message,
            ),
        )

    async def prepare_call(
        self,
        request: ToolCallRequest,
        *,
        context: Any | None = None,
    ) -> tuple[ToolCallRequest, ToolCallResult | None]:
        engine = self._refresh()
        generation_error = self._generation_error(request)
        if generation_error is not None:
            return request, generation_error
        prepared, result = await engine.prepare_call(request, context=context)
        if result is None:
            self._prepared_engines[prepared.call_id] = engine
        return prepared, result

    async def execute(
        self,
        request: ToolCallRequest,
        *,
        context: Any | None = None,
    ) -> ToolCallResult:
        engine = self._prepared_engines.pop(request.call_id, None)
        if engine is None:
            engine = self._refresh()
            generation_error = self._generation_error(request)
            if generation_error is not None:
                return generation_error
        return await engine.execute(request, context=context)

    async def end_run(
        self,
        *,
        session_id: str,
        run_id: str,
        metadata: Mapping[str, Any],
    ) -> None:
        await self._refresh().end_run(
            session_id=session_id,
            run_id=run_id,
            metadata=metadata,
        )


__all__ = ["MCPToolExposureEngine"]
