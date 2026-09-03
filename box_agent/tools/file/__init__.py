"""Structured file tools.

The package is intentionally lazy.  ``file_tools`` owns shared path and
safety helpers used by the concrete readers; importing the concrete classes
eagerly here would create a package cycle while the registry is bootstrapping.
"""

from typing import Any

__all__ = ["JsonlQueryTool", "ReadTool"]


def __getattr__(name: str) -> Any:
    if name == "ReadTool":
        from .read_tool import ReadTool

        return ReadTool
    if name == "JsonlQueryTool":
        from .jsonl_tool import JsonlQueryTool

        return JsonlQueryTool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
