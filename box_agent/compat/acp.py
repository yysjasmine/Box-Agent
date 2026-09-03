"""Lazy compatibility facade for the stable :mod:`box_agent.acp` package.

The protocol-neutral adapter lives under ``box_agent.adapters.acp``; the ACP
package remains the stdio/protocol entry point exposed to existing callers.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_TARGET = "box_agent.acp"


def __getattr__(name: str) -> Any:
    value = getattr(import_module(_TARGET), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(import_module(_TARGET))))
