"""Small async interoperability helpers shared by provider adapters."""

from __future__ import annotations

import inspect
from typing import Any


async def await_if_needed(value: Any) -> Any:
    """Resolve an awaitable SDK value or return a direct SDK value unchanged."""

    if inspect.isawaitable(value):
        return await value
    return value


__all__ = ["await_if_needed"]
