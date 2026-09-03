"""CLI protocol adapters.

The CLI adapter owns terminal rendering and negotiation only. Runtime policy
and the Agent Loop remain in ``box_agent.services`` and ``box_agent.kernel``.
"""

from .memory_proposal import CLIMemoryProposalNegotiator
from .kernel_runtime import KernelCLIConversation
from .permission_broker import CLIPermissionNegotiator

__all__ = [
    "CLIMemoryProposalNegotiator",
    "CLIPermissionNegotiator",
    "KernelCLIConversation",
]
