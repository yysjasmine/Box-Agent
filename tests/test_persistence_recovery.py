"""Durable persistence contracts for resumable Agent runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from box_agent.api import AgentEvent
from box_agent.persistence import (
    EffectStatus,
    PersistenceConflictError,
    RunCheckpoint,
    SQLiteEffectLedger,
    SQLiteEventLog,
    SQLiteLeaseStore,
    SQLiteSessionStore,
    LeaseConflictError,
)


def _event(sequence: int, *, event_id: str | None = None) -> AgentEvent:
    return AgentEvent(
        event_id=event_id or f"event-{sequence}",
        sequence=sequence,
        session_id="session-1",
        run_id="run-1",
        type="run.started" if sequence == 1 else "model.content.delta",
        payload={"sequence": sequence},
    )


@pytest.mark.asyncio
async def test_checkpoint_commit_replays_events_from_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "runs.sqlite3"
    event_log = SQLiteEventLog(db_path)
    checkpoint = RunCheckpoint(
        checkpoint_id="checkpoint-1",
        session_id="session-1",
        run_id="run-1",
        sequence=1,
        state={"phase": "after_model"},
        event_hash="hash-1",
    )

    await event_log.commit_checkpoint(checkpoint, events=[_event(1)])
    bundle = await event_log.load_recovery_bundle("run-1")

    assert bundle.checkpoint == checkpoint
    assert bundle.events == (_event(1),)

    await event_log.append(_event(2))
    replay = await event_log.events_after("run-1", sequence=1)
    assert replay == (_event(2),)


@pytest.mark.asyncio
async def test_duplicate_event_is_idempotent_but_conflicting_event_fails(
    tmp_path: Path,
) -> None:
    event_log = SQLiteEventLog(tmp_path / "runs.sqlite3")
    first = _event(1)

    await event_log.append(first)
    await event_log.append(first)

    with pytest.raises(PersistenceConflictError):
        await event_log.append(
            AgentEvent(
                event_id=first.event_id,
                sequence=1,
                session_id="session-1",
                run_id="run-1",
                type="different.event",
                payload={},
            )
        )


@pytest.mark.asyncio
async def test_effect_ledger_requires_reconciliation_before_reuse(tmp_path: Path) -> None:
    ledger = SQLiteEffectLedger(tmp_path / "runs.sqlite3")

    prepared = await ledger.prepare(
        effect_id="effect-1",
        run_id="run-1",
        idempotency_key="idem-1",
        request_digest="digest-1",
    )
    repeated = await ledger.prepare(
        effect_id="effect-1",
        run_id="run-1",
        idempotency_key="idem-1",
        request_digest="digest-1",
    )
    assert prepared == repeated
    assert prepared.status == EffectStatus.PREPARED

    with pytest.raises(PersistenceConflictError):
        await ledger.prepare(
            effect_id="effect-1",
            run_id="run-1",
            idempotency_key="idem-1",
            request_digest="different",
        )

    completed = await ledger.complete(
        effect_id="effect-1",
        status=EffectStatus.SUCCEEDED,
        result={"value": 42},
    )
    assert completed.status == EffectStatus.SUCCEEDED
    assert await ledger.reconcile("effect-1") == completed


@pytest.mark.asyncio
async def test_lease_fencing_prevents_two_workers_from_running_one_run(
    tmp_path: Path,
) -> None:
    leases = SQLiteLeaseStore(tmp_path / "runs.sqlite3")

    first = await leases.acquire("run-1", owner_id="worker-a", ttl_seconds=60)
    with pytest.raises(LeaseConflictError):
        await leases.acquire("run-1", owner_id="worker-b", ttl_seconds=60)

    await leases.release(first)
    second = await leases.acquire("run-1", owner_id="worker-b", ttl_seconds=60)

    assert second.epoch > first.epoch
    assert second.owner_id == "worker-b"


@pytest.mark.asyncio
async def test_session_store_restores_metadata_after_restart(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    from box_agent.api import SessionInfo

    session = SessionInfo("session-1", "2026-01-01T00:00:00+00:00", {"mode": "code"})
    await store.put(session)

    restored = await SQLiteSessionStore(tmp_path / "sessions.sqlite3").get("session-1")

    assert restored == session


@pytest.mark.asyncio
async def test_session_store_updates_metadata_without_changing_identity(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions-update.sqlite3")
    from box_agent.api import SessionInfo

    original = SessionInfo("session-1", "2026-01-01T00:00:00+00:00", {"mode": "code"})
    await store.put(original)
    updated = SessionInfo(
        original.session_id,
        original.created_at,
        {"mode": "code", "goal": {"objective": "Ship", "status": "active"}},
    )

    await store.update(updated)

    assert await SQLiteSessionStore(tmp_path / "sessions-update.sqlite3").get(
        "session-1"
    ) == updated


def test_session_store_shutdown_closes_database_resource(tmp_path: Path) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions-shutdown.sqlite3")

    store.shutdown()

    with pytest.raises(Exception, match="closed database"):
        store.connection.execute("SELECT 1")
