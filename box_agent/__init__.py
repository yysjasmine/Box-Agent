"""Box Agent public package boundary.

Names are loaded lazily so stable contracts remain importable without loading
host adapters, provider SDKs, or MCP integrations.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "Agent": ("box_agent.agent", "Agent"),
    "AgentLoopKernel": ("box_agent.kernel", "AgentLoopKernel"),
    "AgentRunOptions": ("box_agent.agent", "AgentRunOptions"),
    "AgentEvent": ("box_agent.api", "AgentEvent"),
    "StopReason": ("box_agent.compat.events", "StopReason"),
    "BaseHook": ("box_agent.compat.hooks", "BaseHook"),
    "HookManager": ("box_agent.compat.hooks", "HookManager"),
    "load_hooks": ("box_agent.compat.hooks", "load_hooks"),
    "LLMClient": ("box_agent.llm", "LLMClient"),
    "KernelAgentService": ("box_agent.services.kernel", "KernelAgentService"),
    "build_kernel_service": (
        "box_agent.adapters",
        "build_kernel_service",
    ),
    "PluginKernelComposer": ("box_agent.kernel", "PluginKernelComposer"),
    "FunctionCall": ("box_agent.schema", "FunctionCall"),
    "LLMProvider": ("box_agent.schema", "LLMProvider"),
    "LLMResponse": ("box_agent.schema", "LLMResponse"),
    "Message": ("box_agent.schema", "Message"),
    "ToolCall": ("box_agent.schema", "ToolCall"),
    "WorkflowCheckpointUpdate": (
        "box_agent.workflow_policy",
        "WorkflowCheckpointUpdate",
    ),
    "WorkflowPolicy": ("box_agent.workflow_policy", "WorkflowPolicy"),
}

__version__ = "0.9.6"


def _frozen_runtime_version(default: str) -> str:
    """Use the outer runtime bundle version when running a frozen ACP binary."""
    if not getattr(sys, "frozen", False):
        return default
    version_path = Path(sys.executable).resolve().parent.parent / "VERSION"
    try:
        bundled_version = version_path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    return bundled_version or default


__version__ = _frozen_runtime_version(__version__)


def __getattr__(name: str):
    """Load a public export only when it is actually requested."""

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(importlib.import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy exports in interactive discovery."""

    return sorted(set(globals()) | set(_LAZY_EXPORTS))

__all__ = [
    "Agent",
    "AgentLoopKernel",
    "AgentEvent",
    "AgentRunOptions",
    "BaseHook",
    "build_kernel_service",
    "FunctionCall",
    "HookManager",
    "LLMClient",
    "KernelAgentService",
    "PluginKernelComposer",
    "LLMProvider",
    "LLMResponse",
    "Message",
    "StopReason",
    "ToolCall",
    "WorkflowCheckpointUpdate",
    "WorkflowPolicy",
    "load_hooks",
]
