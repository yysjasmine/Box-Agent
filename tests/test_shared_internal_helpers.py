"""Regression coverage for mechanisms shared across plugin implementations."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from box_agent.llm.async_utils import await_if_needed
from box_agent.workflows.state import workflow_state_from_recovery


@pytest.mark.asyncio
async def test_await_if_needed_preserves_direct_and_awaitable_values() -> None:
    async def deferred() -> str:
        return "awaited"

    assert await await_if_needed("direct") == "direct"
    assert await await_if_needed(deferred()) == "awaited"


def test_workflow_state_from_recovery_reads_latest_matching_checkpoint() -> None:
    events = (
        {"payload": {"workflow_state": {"demo": {"step": 1}}}},
        SimpleNamespace(
            payload={"workflow_state": {"other": {"step": 2}}}
        ),
        {"payload": {"workflow_state": {"demo": {"step": 3}}}},
    )

    assert workflow_state_from_recovery({"events": events}, "demo") == {
        "step": 3
    }
    assert workflow_state_from_recovery(SimpleNamespace(events=events), "other") == {
        "step": 2
    }
    assert workflow_state_from_recovery(None, "demo") is None
