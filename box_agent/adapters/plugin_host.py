"""Compose concrete Box-Agent capabilities into the typed PluginHost."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from box_agent.api import PermissionDecision, PermissionRequest
from box_agent.context import InMemoryContextEngine
from box_agent.plugins import PluginHost
from box_agent.plugins.hooks import LifecycleHookAdapter
from box_agent.tools.registration import register_tool_plugins

from .capabilities import LLMClientPort, MemoryManagerEngine


class PermissionNegotiatorGateway:
    """Adapt existing ``negotiate(dict)`` or new ``decide(request)`` objects."""

    def __init__(self, negotiator: Any) -> None:
        self._negotiator = negotiator
        self._retry_counts: dict[tuple[str, str, str], int] = {}

    async def decide(self, request: PermissionRequest) -> PermissionDecision:
        decide = getattr(self._negotiator, "decide", None)
        if callable(decide):
            value = decide(request)
        else:
            negotiate = getattr(self._negotiator, "negotiate", None)
            if not callable(negotiate):
                return PermissionDecision(granted=False, reason="no permission decision method")
            # The established negotiator contract is a flat payload. The new
            # protocol keeps tool-specific fields in ``metadata``; reconstruct
            # the old wire shape at this adapter boundary instead of forcing
            # every permission provider to understand both representations.
            legacy_payload = {
                **dict(request.metadata),
                "scope": request.scope,
                "reason": request.reason,
                "requested_scope": request.requested_scope,
            }
            if request.resource and not legacy_payload.get("path"):
                legacy_payload["path"] = request.resource
            value = negotiate(legacy_payload)
        if hasattr(value, "__await__"):
            value = await value
        if isinstance(value, PermissionDecision):
            granted = value.granted
            reason = value.reason
            supplied_metadata = dict(value.metadata)
        elif isinstance(value, Mapping):
            granted = bool(value.get("granted", value.get("approved", False)))
            reason = str(value.get("reason", ""))
            supplied_metadata = dict(value)
        else:
            granted = bool(value)
            reason = "permission decision"
            supplied_metadata = {}
        identity = (
            str(request.metadata.get("session_id", "")),
            str(request.metadata.get("run_id", "")),
            str(request.metadata.get("call_id", "")),
        )
        retry_count = self._retry_counts.get(identity, 0)
        if granted:
            retry_count += 1
            self._retry_counts[identity] = retry_count
        metadata = {
            **dict(request.metadata),
            **supplied_metadata,
            "type": "policy_decision",
            "tool_name": str(request.metadata.get("tool_name", "")),
            "decision": "approved" if granted else "denied",
            "retry_count": retry_count,
            "scope": request.scope,
            "requested_scope": request.requested_scope,
        }
        if request.resource:
            metadata.setdefault("path", request.resource)
            metadata.setdefault("resource", request.resource)
        return PermissionDecision(
            granted=granted,
            reason=reason,
            metadata=metadata,
        )


def build_plugin_host(
    *,
    llm: Any,
    tools: Iterable[Any] = (),
    memory_manager: Any | None = None,
    permission_negotiator: Any | None = None,
    context_engine: Any | None = None,
    hooks: Iterable[Any] = (),
    workflow_policy: Any | None = None,
    prefer_generate: bool = False,
) -> PluginHost:
    """Register concrete runtime objects in the canonical typed registries."""

    host = PluginHost()
    def register_capability(registry_name: str, key: str, value: Any) -> None:
        host.registries[registry_name].register(key, value, source="box_agent.host")

    llm_port = LLMClientPort(llm, prefer_generate=prefer_generate)
    register_capability("llm.providers", "default", llm_port)
    context_port = context_engine if context_engine is not None else InMemoryContextEngine()
    register_capability("context.providers", "default", context_port)
    register_tool_plugins(host, tools)
    if memory_manager is not None:
        memory_port = MemoryManagerEngine(memory_manager)
        register_capability("memory.providers", "default", memory_port)
    if permission_negotiator is not None:
        permission_port = PermissionNegotiatorGateway(permission_negotiator)
        register_capability("permission.policies", "default", permission_port)
    hook_values = tuple(hooks)
    for index, hook in enumerate(hook_values):
        host.registries["hooks"].register(
            f"hook-{index}", LifecycleHookAdapter(hook), source="box_agent.host"
        )
    if workflow_policy is not None:
        host.registries["workflows"].register(
            "default", workflow_policy, source="box_agent.host"
        )
    return host


def build_kernel_service(
    *,
    llm: Any,
    tools: Iterable[Any] = (),
    memory_manager: Any | None = None,
    permission_negotiator: Any | None = None,
    context_engine: Any | None = None,
    hooks: Iterable[Any] = (),
    workflow_policy: Any | None = None,
    event_log: Any | None = None,
    effect_ledger: Any | None = None,
    session_store: Any | None = None,
    lease_store: Any | None = None,
    owner_id: str | None = None,
    lease_ttl_seconds: float = 60.0,
    control_handler: Any | None = None,
) -> KernelAgentService:
    """Build a ``KernelAgentService`` from concrete application components."""

    from box_agent.services.kernel import KernelAgentService

    hook_values = tuple(hooks)
    host = build_plugin_host(
        llm=llm,
        tools=tools,
        memory_manager=memory_manager,
        permission_negotiator=permission_negotiator,
        context_engine=context_engine,
        hooks=hook_values,
        workflow_policy=workflow_policy,
    )
    return KernelAgentService.from_plugin_host(
        host,
        event_log=event_log,
        effect_ledger=effect_ledger,
        session_store=session_store,
        lease_store=lease_store,
        owner_id=owner_id,
        lease_ttl_seconds=lease_ttl_seconds,
        control_handler=control_handler,
        hook_keys=tuple(f"hook-{i}" for i in range(len(hook_values))),
    )


__all__ = [
    "PermissionNegotiatorGateway",
    "build_kernel_service",
    "build_plugin_host",
]
