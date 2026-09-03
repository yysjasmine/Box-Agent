"""Historical Box-Agent contracts translated onto the single Kernel runtime.

New integrations should use ``box_agent.api``, ``box_agent.kernel``, capability
packages, or host adapters. Stable root imports may alias modules here, but
this package never owns an execution loop.
"""

__all__ = [
    "agent",
    "core",
    "cli",
    "acp",
    "events",
    "hooks",
    "goal",
    "runtime",
    "workflow_policy",
]
