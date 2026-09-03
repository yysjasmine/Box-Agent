"""Pluggable memory contracts, stores, extraction, and maintenance.

Contract types are cheap to import; implementations are lazy so the stable
API/kernel import boundary does not eagerly load concrete backends.
"""

from __future__ import annotations

import importlib

from .api import MemoryConflictError, MemoryEngine, MemoryEntry, MemoryQuery, MemoryRecall

_LAZY_EXPORTS = {
    "InMemoryMemoryEngine": ("box_agent.memory_engine.in_memory", "InMemoryMemoryEngine"),
    "MemoryCapabilityAdapter": ("box_agent.memory_engine.in_memory", "MemoryCapabilityAdapter"),
    "CompositeMemoryEngine": ("box_agent.memory_engine.composite", "CompositeMemoryEngine"),
    "MemoryProposalService": ("box_agent.memory_engine.proposals", "MemoryProposalService"),
    "ConversationMemoryExtractionHook": (
        "box_agent.memory_engine.extraction",
        "ConversationMemoryExtractionHook",
    ),
    "ContextEntry": ("box_agent.memory_engine.store", "ContextEntry"),
    "TopicStore": ("box_agent.memory_engine.store", "TopicStore"),
    "MemoryManager": ("box_agent.memory_engine.store", "MemoryManager"),
    "MemoryExtractor": ("box_agent.memory_engine.store", "MemoryExtractor"),
    "MemoryMaintainer": (
        "box_agent.memory_engine.maintenance",
        "MemoryMaintainer",
    ),
    "tokens": ("box_agent.memory_engine.store", "tokens"),
    "jaccard": ("box_agent.memory_engine.store", "jaccard"),
    "parse_context_file": (
        "box_agent.memory_engine.store",
        "parse_context_file",
    ),
    "write_context_file": (
        "box_agent.memory_engine.store",
        "write_context_file",
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
    "MemoryConflictError",
    "MemoryEngine",
    "MemoryEntry",
    "MemoryQuery",
    "MemoryRecall",
    "InMemoryMemoryEngine",
    "MemoryCapabilityAdapter",
    "CompositeMemoryEngine",
    "MemoryProposalService",
    "ConversationMemoryExtractionHook",
    "ContextEntry",
    "TopicStore",
    "MemoryManager",
    "MemoryExtractor",
    "MemoryMaintainer",
    "tokens",
    "jaccard",
    "parse_context_file",
    "write_context_file",
]
