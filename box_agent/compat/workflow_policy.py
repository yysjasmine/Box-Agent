"""Lazy compatibility facade for the legacy workflow-policy module."""

from __future__ import annotations

from typing import Any

from ._forward import forward_attribute

_TARGET = "box_agent.workflows.contract"
__all__ = ["WorkflowPolicy", "WorkflowAction", "WorkflowCheckpointUpdate"]


def __getattr__(name: str) -> Any:
    return forward_attribute(_TARGET, name, globals())
