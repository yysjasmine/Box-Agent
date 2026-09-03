"""Event-driven observability plugins for the Agent Runtime."""

from __future__ import annotations

import importlib


def __getattr__(name: str):
    modules = {
        "AgentLoggerHook": "box_agent.observability.agent_logger_hook",
        "SessionTraceHook": "box_agent.observability.session_trace_hook",
    }
    if name not in modules:
        raise AttributeError(name)
    value = getattr(
        importlib.import_module(modules[name]),
        name,
    )
    globals()[name] = value
    return value


__all__ = ["AgentLoggerHook", "SessionTraceHook"]
