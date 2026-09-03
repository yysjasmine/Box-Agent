"""Pluggable context assembly and compaction capabilities.

Contract DTOs are imported eagerly; reference implementations stay lazy so
``box_agent.api`` and ``box_agent.kernel`` do not load a concrete context
engine merely to expose the SPI.
"""

from __future__ import annotations

import importlib

from .api import (
    ContextBuildRequest,
    ContextBuildResult,
    ContextCompactor,
    ContextManifest,
    ContextEngine,
    ContextEventSink,
    ContextItem,
    ContextProvider,
    HostProjection,
)
from .model_history import (
    MODEL_HISTORY_PLACEHOLDER_PREFIXES,
    is_model_history_placeholder,
    is_model_instruction_source_path,
)
from .task import TaskContext, normalize_task_id

_LAZY_EXPORTS = {
    "ActionHintContextContributor": (
        "box_agent.context.action_hints",
        "ActionHintContextContributor",
    ),
    "ContextProviderAdapter": ("box_agent.context.in_memory", "ContextProviderAdapter"),
    "InMemoryContextEngine": ("box_agent.context.in_memory", "InMemoryContextEngine"),
    "CompositeContextEngine": ("box_agent.context.composite", "CompositeContextEngine"),
    "SkillCatalogContextContributor": (
        "box_agent.context.skills",
        "SkillCatalogContextContributor",
    ),
    "ExpertContextContributor": (
        "box_agent.context.experts",
        "ExpertContextContributor",
    ),
    "SessionModeContextContributor": (
        "box_agent.context.session_mode",
        "SessionModeContextContributor",
    ),
    "SessionEnvironmentContextContributor": (
        "box_agent.context.session_environment",
        "SessionEnvironmentContextContributor",
    ),
    "build_project_startup_context_prompt": (
        "box_agent.context.project",
        "build_project_startup_context_prompt",
    ),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))

__all__ = [
    "ActionHintContextContributor",
    "ContextBuildRequest",
    "ContextBuildResult",
    "ContextCompactor",
    "ContextManifest",
    "ContextEngine",
    "ContextEventSink",
    "ContextItem",
    "ContextProvider",
    "HostProjection",
    "ContextProviderAdapter",
    "CompositeContextEngine",
    "InMemoryContextEngine",
    "SkillCatalogContextContributor",
    "ExpertContextContributor",
    "SessionModeContextContributor",
    "SessionEnvironmentContextContributor",
    "build_project_startup_context_prompt",
    "MODEL_HISTORY_PLACEHOLDER_PREFIXES",
    "TaskContext",
    "is_model_history_placeholder",
    "is_model_instruction_source_path",
    "normalize_task_id",
]
