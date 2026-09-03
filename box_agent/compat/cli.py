"""Lazy compatibility facade for the stable CLI application entry point."""

from __future__ import annotations

from typing import Any

from ._forward import forward_attribute

_TARGET = "box_agent.adapters.cli.app"
__all__ = ["main"]


def __getattr__(name: str) -> Any:
    return forward_attribute(_TARGET, name, globals())
