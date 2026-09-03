"""Lightweight ACP entrypoint backed exclusively by the Agent Kernel."""

from __future__ import annotations

import asyncio
import importlib
import sys
from typing import Any


_LAZY_EXPORTS = {
    "ACPPermissionGateway": (
        "box_agent.adapters.acp_kernel",
        "ACPPermissionGateway",
    ),
    "KernelACPAgent": ("box_agent.adapters.acp_kernel", "KernelACPAgent"),
}


async def run_acp_server(
    config: Any | None = None,
) -> None:
    """Open ACP immediately and assemble the Kernel runtime in the background."""
    from .bootstrap import run_acp_bootstrap

    await run_acp_bootstrap(config)


def main() -> None:
    """Console-script entrypoint."""

    if sys.argv[1:]:
        raise SystemExit(f"unrecognized arguments: {' '.join(sys.argv[1:])}")
    asyncio.run(run_acp_server())


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(importlib.import_module(module_name), attribute_name)
    globals()[name] = value
    return value


__all__ = [
    "ACPPermissionGateway",
    "KernelACPAgent",
    "main",
    "run_acp_server",
]
