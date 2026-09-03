"""Build an Agent Loop from typed plugin registries.

The composer is intentionally a small dependency-injection layer.  Plugins
register capability instances; the composer resolves those instances for the
run scope and creates an :class:`AgentLoopKernel`.  ACP, CLI, SDK, and tests
therefore share exactly the same wiring rules.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import inspect
from typing import Any

from box_agent.plugins import PluginHost
from box_agent.tools.engine import RegistryToolEngine

from .loop import AgentLoopKernel


class KernelCompositionError(RuntimeError):
    """Raised when the plugin graph cannot provide a required capability."""


class PluginKernelComposer:
    """Resolve typed plugin registrations into an Agent Loop Kernel.

    Registry keys are explicit so a host can run multiple model/context
    profiles side by side.  Resolution always uses ``run`` scope, which
    naturally falls back to session and global registrations.
    """

    def __init__(
        self,
        host: PluginHost,
        *,
        llm_key: str = "default",
        context_key: str | None = "default",
        memory_key: str | None = "default",
        tool_engine_key: str | None = "default",
        permission_key: str | None = "default",
        effect_ledger_key: str | None = "default",
        effect_ledger: Any | None = None,
        hook_keys: Iterable[str] | None = None,
        workflow_key: str | None = "default",
        artifact_processor_key: str | None = "default",
    ) -> None:
        self._host = host
        self._effect_ledger_override = effect_ledger
        self._keys = {
            "llm": llm_key,
            "context": context_key,
            "memory": memory_key,
            "tool_engine": tool_engine_key,
            "permission": permission_key,
            "effects": effect_ledger_key,
            "workflow": workflow_key,
            "artifact_processor": artifact_processor_key,
        }
        # ``None`` means "all registered hooks".  An explicit empty tuple is
        # still a useful escape hatch for hosts that want a completely clean
        # kernel (for example deterministic replay fixtures).  This keeps
        # plugin registration composable: a third-party Hook becomes active
        # without every adapter having to maintain a second list of keys.
        self._hook_keys = None if hook_keys is None else tuple(hook_keys)

    def build(self, request: Any | None = None, bundle: Any | None = None) -> AgentLoopKernel:
        """Create one kernel from the current plugin registrations."""

        llm = _bind_capability(
            self._resolve("llm", required=True, request=request),
            request,
            bundle,
        )
        context = _bind_capability(
            self._resolve("context", request=request),
            request,
            bundle,
        )
        context = self._compose_context(request, primary=context)
        memory = _bind_capability(
            self._resolve("memory", request=request),
            request,
            bundle,
        )
        if memory is None or not _is_memory_engine(memory):
            split_memory = self._compose_memory(request, primary=memory)
            if split_memory is not None:
                memory = split_memory
        permission = _bind_capability(
            self._resolve("permission", request=request),
            request,
            bundle,
        )
        effect_ledger = (
            self._effect_ledger_override
            if self._effect_ledger_override is not None
            else self._resolve("effects", request=request)
        )
        if self._hook_keys is None:
            hook_registry = self._host.registries.get("hooks")
            hook_keys = (
                tuple(
                    dict.fromkeys(
                        registration.key
                        for registration in hook_registry.registrations()
                    )
                )
                if hook_registry is not None
                else ()
            )
        else:
            hook_keys = self._hook_keys
        hooks = tuple(
            _bind_capability(value, request, bundle)
            for key in hook_keys
            if (value := self._resolve_key("hooks", key)) is not None
        )
        tool_engine = self._resolve("tool_engine", request=request)
        tool_engine = _bind_capability(tool_engine, request, bundle)
        if tool_engine is None:
            tools_registry = self._host.registries.get("tools.executors")
            if tools_registry is None or not tools_registry.registrations():
                tools_registry = self._host.registries.get("tools")
            if tools_registry is not None:
                descriptor_registry = self._host.registries.get("tools.descriptors")
                tool_engine = RegistryToolEngine(
                    tools_registry,
                    descriptor_registry=descriptor_registry,
                    permission_gateway=permission,
                    effect_ledger=effect_ledger,
                    hooks=hooks,
                    defer_effect_prepare=effect_ledger is not None,
                    defer_effect_completion=effect_ledger is not None,
                )
        requested_workflow = self._request_key("workflow", request)
        default_workflow = self._resolve("workflow")
        selected_workflow = (
            self._resolve_key("workflows", requested_workflow)
            if requested_workflow
            else None
        )
        if requested_workflow and selected_workflow is None:
            raise KernelCompositionError(
                f"requested workflow plugin '{requested_workflow}' is not registered"
            )
        workflow = default_workflow
        if selected_workflow is not None and selected_workflow is not default_workflow:
            if default_workflow is None:
                workflow = selected_workflow
            else:
                from .workflow_composite import CompositeWorkflowPolicy

                workflow = CompositeWorkflowPolicy(
                    (default_workflow, selected_workflow)
                )
        workflow = _bind_workflow(workflow, request, bundle)
        artifact_processor = _bind_capability(
            self._resolve("artifact_processor", request=request),
            request,
            bundle,
        )

        return AgentLoopKernel(
            llm=_require_capability(llm, "llm"),
            context_engine=_as_context_engine(context),
            memory_engine=_as_memory_engine(memory),
            tool_engine=tool_engine,
            hooks=hooks,
            workflow_policy=workflow,
            artifact_processor=artifact_processor,
            recovery=bundle,
        )

    def _resolve(
        self,
        kind: str,
        *,
        required: bool = False,
        request: Any | None = None,
    ) -> Any | None:
        key = self._keys[kind]
        if request is not None:
            key = self._request_key(kind, request) or key
        if key is None:
            return None
        value = self._resolve_key(kind, key)
        # ``tools.executors`` contains individual Tool objects. They are not
        # a complete Tool Engine even when a caller registers one under a
        # convenient key such as ``default``. Treat that as unresolved so the
        # registry-backed engine below is composed instead of invoking
        # ``Tool.execute`` with a ``ToolCallRequest``.
        if kind == "tool_engine" and _looks_like_tool(value):
            value = None
        if required and value is None:
            raise KernelCompositionError(
                f"required plugin capability '{kind}:{key}' is not registered"
            )
        return value

    def _resolve_key(self, kind: str, key: str) -> Any | None:
        registry_kinds = {
            "context": ("context.providers", "context"),
            "tool_engine": ("tools.engines", "tools", "tools.executors"),
            "permission": (
                "permission.policies",
                "permission.brokers",
                "permissions",
            ),
            "memory": (
                "memory.providers",
                "memory",
            ),
            "llm": ("llm.providers", "llm"),
            "workflow": ("workflows",),
            "effects": ("effects",),
            "artifact_processor": ("artifacts.processors",),
        }.get(kind, (kind,))
        for registry_kind in registry_kinds:
            registry = self._host.registries.get(registry_kind)
            if registry is None:
                continue
            value = registry.resolve(key, scope="run")
            if value is not None:
                return value
        return None

    def _compose_memory(
        self,
        request: Any | None,
        *,
        primary: Any | None = None,
    ) -> Any | None:
        """Compose split memory registries when no complete engine is bound."""

        key = self._request_key("memory", request) if request is not None else None
        key = key or self._keys["memory"]
        if key is None:
            return None
        provider = primary or self._resolve_registry_key(
            "memory.providers",
            self._request_component_key(request, ("memory_provider", "memory"))
            or key,
        )
        store = self._resolve_registry_key(
            "memory.stores",
            self._request_component_key(request, ("memory_store", "memory")) or key,
        )
        writer = self._resolve_registry_key(
            "memory.writers",
            self._request_component_key(request, ("memory_writer", "memory")) or key,
        )
        if provider is None and store is None and writer is None:
            return None
        from box_agent.memory_engine import CompositeMemoryEngine

        return CompositeMemoryEngine(provider=provider, store=store, writer=writer)

    def _compose_context(
        self,
        request: Any | None,
        *,
        primary: Any | None = None,
    ) -> Any | None:
        """Compose a provider with an optional canonical compactor."""

        key = self._request_key("context", request) if request is not None else None
        key = key or self._keys["context"]
        contributor_registry = self._host.registries.get("context.contributors")
        contributors = (
            contributor_registry.resolve_all(scope="run")
            if contributor_registry is not None
            else ()
        )
        if key is None and not contributors:
            return primary
        compactor = self._resolve_registry_key(
            "context.compactors",
            self._request_component_key(request, ("context_compactor", "context"))
            or key,
        ) if key is not None else None
        if compactor is None and not contributors:
            return primary
        from box_agent.context import CompositeContextEngine

        return CompositeContextEngine(
            provider=primary,
            contributors=tuple(contributors),
            compactor=compactor,
        )

    def _resolve_registry_key(self, registry_kind: str, key: str) -> Any | None:
        registry = self._host.registries.get(registry_kind)
        return registry.resolve(key, scope="run") if registry is not None else None

    @staticmethod
    def _request_key(kind: str, request: Any) -> str | None:
        """Resolve a run-local registry key without exposing registries to hosts."""

        options = getattr(request, "options", None)
        component_keys = getattr(options, "component_keys", {})
        if isinstance(component_keys, Mapping):
            aliases = {
                "context": ("context", "context_provider"),
                "memory": ("memory", "memory_provider"),
                "tool_engine": ("tool_engine", "tools", "tool"),
                "permission": ("permission", "permissions"),
                "llm": ("llm", "model"),
            }.get(kind, (kind,))
            for alias in aliases:
                value = component_keys.get(alias)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        if kind != "workflow":
            return None
        requested = getattr(options, "workflow_id", None)
        if isinstance(requested, str) and requested.strip():
            return requested.strip()
        metadata = getattr(request, "metadata", {})
        if isinstance(metadata, dict):
            for name in ("workflow_id", "workflowId", "workflow"):
                value = metadata.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _request_component_key(request: Any | None, aliases: tuple[str, ...]) -> str | None:
        if request is None:
            return None
        options = getattr(request, "options", None)
        component_keys = getattr(options, "component_keys", {})
        if not isinstance(component_keys, Mapping):
            return None
        for alias in aliases:
            value = component_keys.get(alias)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None


def _require_capability(value: Any, kind: str) -> Any:
    if value is None:
        raise KernelCompositionError(f"required plugin capability '{kind}' is missing")
    return value


def _looks_like_tool(value: Any | None) -> bool:
    """Return whether a resolved value is an individual Tool plugin."""

    return value is not None and bool(
        getattr(value, "name", None)
        and getattr(value, "parameters", None) is not None
    )


def _is_memory_engine(value: Any | None) -> bool:
    """Whether a value implements the complete MemoryEngine lifecycle."""

    return value is not None and all(
        callable(getattr(value, method, None))
        for method in ("recall", "write", "flush")
    )


def _optional_capability(value: Any, kind: str) -> Any | None:
    if value is None:
        return None
    return value


def _as_context_engine(value: Any | None) -> Any | None:
    if value is None or callable(getattr(value, "assemble", None)):
        return value
    if callable(getattr(value, "provide", None)):
        from box_agent.context import ContextProviderAdapter

        return ContextProviderAdapter(value)
    return _optional_capability(value, "context")


def _as_memory_engine(value: Any | None) -> Any | None:
    if value is None:
        return None
    if callable(getattr(value, "recall", None)) and callable(
        getattr(value, "write", None)
    ) and callable(getattr(value, "flush", None)):
        return value
    if callable(getattr(value, "recall", None)) or callable(
        getattr(value, "query", None)
    ):
        from box_agent.memory_engine import MemoryCapabilityAdapter

        return MemoryCapabilityAdapter(value)
    return _optional_capability(value, "memory")


def _bind_workflow(value: Any | None, request: Any | None, bundle: Any | None) -> Any | None:
    """Create a run-local workflow view when the plugin supports it.

    The method is optional to keep existing workflow implementations source
    compatible.  A workflow that has no per-run state is returned unchanged.
    """

    if value is None:
        return None
    factory = getattr(value, "for_run", None)
    if not callable(factory):
        return value
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory(request, bundle)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    if has_varargs or len(positional) >= 2:
        bound = factory(request, bundle)
    elif len(positional) == 1:
        bound = factory(request)
    else:
        bound = factory()
    return value if bound is None else bound


def _bind_capability(value: Any | None, request: Any | None, bundle: Any | None) -> Any | None:
    """Bind an optional run-scoped capability factory through the same SPI."""

    if value is None:
        return None
    factory = getattr(value, "for_run", None)
    if not callable(factory):
        return value
    try:
        return factory(request, bundle)
    except TypeError as exc:
        # Preserve one-argument third-party factories without turning an
        # implementation TypeError inside a two-argument factory into a retry.
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            raise exc
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        if len(positional) == 1:
            return factory(request)
        raise


__all__ = ["KernelCompositionError", "PluginKernelComposer"]
