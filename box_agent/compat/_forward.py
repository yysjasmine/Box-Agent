"""Small helper for lazy compatibility facades."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any


def forward_attribute(
    module_name: str,
    name: str,
    cache: dict[str, Any],
) -> Any:
    """Resolve one legacy attribute without importing the target eagerly."""

    module: ModuleType = import_module(module_name)
    value = getattr(module, name)
    cache[name] = value
    return value
