"""Compatibility imports over the plugin-composed Agent runtime."""

from box_agent.adapters import build_kernel_service
from box_agent.compat.runtime import invoke_tool_with_permissions, run_agent_loop
from box_agent.kernel import AgentLoopKernel, PluginKernelComposer
from box_agent.workflows.guards import CompletionGate
from box_agent.services import KernelAgentService

__all__ = [
    "CompletionGate",
    "AgentLoopKernel",
    "KernelAgentService",
    "PluginKernelComposer",
    "build_kernel_service",
    "invoke_tool_with_permissions",
    "run_agent_loop",
]
