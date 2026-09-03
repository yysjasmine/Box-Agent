"""Contract tests for the pluggable Memory Engine."""

from __future__ import annotations

import asyncio
import threading

import pytest

from box_agent.adapters.capabilities import MemoryManagerEngine
from box_agent.memory_engine import (
    InMemoryMemoryEngine,
    MemoryCapabilityAdapter,
    MemoryConflictError,
    MemoryEntry,
    MemoryQuery,
)


@pytest.mark.asyncio
async def test_memory_engine_recalls_relevant_entries_in_score_order() -> None:
    engine = InMemoryMemoryEngine()
    await engine.write(MemoryEntry("m-1", "python coding conventions", kind="preference"))
    await engine.write(MemoryEntry("m-2", "travel packing list", kind="note"))
    await engine.write(MemoryEntry("m-3", "python testing conventions", kind="fact"))

    result = await engine.recall(MemoryQuery("python conventions", limit=2))

    assert [entry.entry_id for entry in result.entries] == ["m-1", "m-3"]
    assert result.entries[0].score >= result.entries[1].score


@pytest.mark.asyncio
async def test_memory_engine_rejects_conflicting_entry_reuse() -> None:
    engine = InMemoryMemoryEngine()
    await engine.write(MemoryEntry("m-1", "original"))

    with pytest.raises(MemoryConflictError):
        await engine.write(MemoryEntry("m-1", "changed"))


def test_memory_entry_rejects_empty_identity() -> None:
    with pytest.raises(ValueError):
        MemoryEntry("", "content")


@pytest.mark.asyncio
async def test_memory_capability_adapter_supports_recall_only_plugin() -> None:
    class RecallOnly:
        async def recall(self, query):
            return type("Recall", (), {"entries": (), "query": query})()

    adapter = MemoryCapabilityAdapter(RecallOnly())
    result = await adapter.recall(MemoryQuery("hello"))
    await adapter.write(MemoryEntry("m-1", "write is optional"))
    await adapter.flush()

    assert result.entries == ()


@pytest.mark.asyncio
async def test_concrete_memory_adapter_runs_sync_storage_off_event_loop() -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingManager:
        def search(self, text, *, limit):
            assert text == "hello"
            assert limit == 3
            entered.set()
            assert release.wait(timeout=2.0)
            return [{"id": "m-1", "content": "remembered"}]

    engine = MemoryManagerEngine(BlockingManager())
    recall_task = asyncio.create_task(engine.recall(MemoryQuery("hello", limit=3)))
    assert await asyncio.to_thread(entered.wait, 2.0)

    heartbeat = asyncio.create_task(asyncio.sleep(0.01))
    await asyncio.wait_for(heartbeat, timeout=0.2)
    assert not recall_task.done()

    release.set()
    result = await asyncio.wait_for(recall_task, timeout=2.0)
    assert [entry.entry_id for entry in result.entries] == ["m-1"]
