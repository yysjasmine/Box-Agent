"""Memory tools — read, write, and search long-term memory.

Provides persistent cross-session memory that survives beyond individual
sessions.  Core memory (MEMORY.md) is always injected; topic-sharded context
memory is searchable on demand.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .base import Tool, ToolResult


class MemoryWriteTool(Tool):
    """Tool for writing entries to long-term memory."""

    def __init__(self, memory_manager, llm=None):
        from box_agent.memory_engine import MemoryManager

        self._mgr: MemoryManager = memory_manager
        self._llm = llm

    @property
    def name(self) -> str:
        return "memory_write"

    @property
    def description(self) -> str:
        return (
            "Write to long-term memory that persists across sessions. "
            "Use category='core' ONLY when the user explicitly states personal info "
            "or preferences (e.g. 'my name is...', 'I prefer...'). Never write "
            "summaries or inferences to core. "
            "Use category='context' for project context, task patterns, and notes. "
            "Context writes are model-merged with existing context when possible."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The content to write. Use markdown bullet points, "
                        "e.g. '- User prefers Chinese responses\\n- Project uses React'"
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": ["core", "context"],
                    "description": (
                        "'core' for user identity/preferences (always recalled), "
                        "'context' for project info/task patterns (searchable). Default: 'core'."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["append", "overwrite"],
                    "description": "Write mode: 'append' adds to existing (default), 'overwrite' replaces the category.",
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Optional topic label that buckets context entries into "
                        "per-topic files (e.g. 'preferences', 'project-x'). "
                        "Only used when category='context'. Defaults to 'general'."
                    ),
                },
            },
            "required": ["content"],
        }

    async def execute(self, content: str, category: str = "core", mode: str = "append", topic: str = "general") -> ToolResult:
        try:
            if category == "context":
                if mode == "overwrite":
                    await asyncio.to_thread(
                        self._mgr.write_context,
                        content,
                        topic=topic,
                    )
                    strategy = "overwrite"
                elif self._llm is not None:
                    strategy = await self._mgr.update_context_with_llm(content, self._llm, topic=topic)
                else:
                    await asyncio.to_thread(
                        self._mgr.append_context,
                        content,
                        topic=topic,
                    )
                    strategy = "append_dedup"
                current = await asyncio.to_thread(self._mgr.read_context)
                label = "context"
            else:
                if mode == "overwrite":
                    await asyncio.to_thread(self._mgr.write_core, content)
                else:
                    await asyncio.to_thread(self._mgr.append_core, content)
                current = await asyncio.to_thread(self._mgr.read_core)
                label = "core"
                strategy = mode

            return ToolResult(
                success=True,
                content=f"Memory updated ({label}, {strategy}). Current {label} memory:\n{current}",
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Failed to write memory: {e}")


class MemoryReadTool(Tool):
    """Tool for reading all long-term memory."""

    def __init__(self, memory_manager):
        from box_agent.memory_engine import MemoryManager

        self._mgr: MemoryManager = memory_manager

    @property
    def name(self) -> str:
        return "memory_read"

    @property
    def description(self) -> str:
        return (
            "Read all long-term memory. Returns core memory (always recalled) "
            "and context memory (searchable). Use this to review what has been saved."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self) -> ToolResult:
        try:
            core, context = await asyncio.gather(
                asyncio.to_thread(self._mgr.read_core),
                asyncio.to_thread(self._mgr.read_context),
            )
            if not core and not context:
                return ToolResult(success=True, content="No long-term memory saved yet.")

            parts: list[str] = []
            if core:
                parts.append(f"[Core Memory (MEMORY.md)]\n{core}")
            if context:
                parts.append(f"[Context Memory]\n{context}")
            return ToolResult(success=True, content="\n\n".join(parts))
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Failed to read memory: {e}")


class MemorySearchTool(Tool):
    """Tool for searching context memory by keyword."""

    def __init__(self, memory_manager):
        from box_agent.memory_engine import MemoryManager

        self._mgr: MemoryManager = memory_manager

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search topic-sharded context/experience memory by keyword. Use this "
            "when the current request may depend on saved user preferences, prior "
            "decisions, repo/workflow conventions, previous pitfalls, specific "
            "paths or errors, or recurring task experience. Skip it for clearly "
            "one-off simple questions. Prefer 1-3 short durable search keys "
            "instead of the full user sentence: project/product names, repo/module "
            "paths, exact errors, workflows, artifact types, or prior decisions. "
            "For compound Chinese requests, split stable noun phrases into "
            "separate searches. Core memory is always available — use this for "
            "everything else."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Search key or short phrase (case-insensitive). Avoid full "
                        "sentences with action words; use stable nouns such as "
                        "project names, paths, errors, workflows, or artifact types."
                    ),
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Optional context topic to search, e.g. 'preferences', "
                        "'project', 'feedback', or 'general'. If omitted, the "
                        "memory index routes the query to relevant topics first."
                    ),
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, topic: str | None = None) -> ToolResult:
        try:
            results = await asyncio.to_thread(
                self._mgr.search,
                query,
                topic=topic,
            )
            if not results:
                topic_suffix = f" in topic '{topic}'" if topic else ""
                return ToolResult(
                    success=True,
                    content=f"No matching memories found for '{query}'{topic_suffix}.",
                    raw_output={
                        "type": "memory_search",
                        "query": query,
                        **({"topic": topic} if topic else {}),
                        "matched_memories": [],
                    },
                )
            topic_suffix = f" in topic '{topic}'" if topic else ""
            return ToolResult(
                success=True,
                content=f"Found {len(results)} match(es) for '{query}'{topic_suffix}:\n" + "\n".join(results),
                raw_output={
                    "type": "memory_search",
                    "query": query,
                    **({"topic": topic} if topic else {}),
                    "matched_memories": [
                        {
                            "id": f"context:{index}",
                            "source": "context",
                            "category": "context",
                            "text": line,
                        }
                        for index, line in enumerate(results, start=1)
                    ],
                },
            )
        except Exception as e:
            return ToolResult(success=False, content="", error=f"Failed to search memory: {e}")
