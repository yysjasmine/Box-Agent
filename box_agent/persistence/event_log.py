"""SQLite event log and atomic Run checkpoint operations."""

from __future__ import annotations

import sqlite3
from typing import Iterable

from box_agent.api import AgentEvent

from .api import (
    EffectRecord,
    EffectStatus,
    OutboxRecord,
    PersistenceConflictError,
    RecoveryBundle,
    RunCheckpoint,
)
from .sqlite import SQLiteStore, canonical_json, decode_json


def _event_from_dict(data: dict) -> AgentEvent:
    return AgentEvent(
        event_id=data["event_id"],
        sequence=data["sequence"],
        session_id=data["session_id"],
        run_id=data["run_id"],
        type=data["type"],
        payload=data.get("payload", {}),
        turn_id=data.get("turn_id"),
        audience=tuple(data.get("audience", ())),
        occurred_at=data.get("occurred_at"),
        correlation_id=data.get("correlation_id"),
        protocol_version=data.get("protocol_version", "1"),
    )


def _checkpoint_from_row(row) -> RunCheckpoint:
    keys = row.keys()
    return RunCheckpoint(
        checkpoint_id=row["checkpoint_id"],
        session_id=row["session_id"],
        run_id=row["run_id"],
        sequence=row["sequence"],
        state=decode_json(row["state_json"]),
        event_hash=row["event_hash"],
        schema_version=row["schema_version"],
        plugin_lock=decode_json(row["plugin_lock_json"]),
        plugin_snapshot=(
            decode_json(row["plugin_snapshot_json"])
            if "plugin_snapshot_json" in keys
            else {}
        ),
    )


def _effect_from_row(row) -> EffectRecord:
    result = decode_json(row["result_json"]) if row["result_json"] is not None else None
    return EffectRecord(
        effect_id=row["effect_id"],
        run_id=row["run_id"],
        idempotency_key=row["idempotency_key"],
        request_digest=row["request_digest"],
        status=EffectStatus(row["status"]),
        result=result,
    )


def _outbox_from_row(row) -> OutboxRecord:
    return OutboxRecord(
        outbox_id=row["outbox_id"],
        event=_event_from_dict(decode_json(row["event_json"])),
        status=row["status"],
    )


class SQLiteEventLog(SQLiteStore):
    """Append-only, sequence-ordered event log with idempotent writes."""

    def _append_locked(self, event: AgentEvent) -> AgentEvent:
        event_json = canonical_json(event.to_dict())
        by_id = self.connection.execute(
            "SELECT event_json FROM agent_events WHERE event_id = ?", (event.event_id,)
        ).fetchone()
        if by_id is not None:
            if by_id["event_json"] != event_json:
                raise PersistenceConflictError(
                    f"event_id {event.event_id!r} already contains different data"
                )
            return event

        by_sequence = self.connection.execute(
            "SELECT event_json FROM agent_events WHERE run_id = ? AND sequence = ?",
            (event.run_id, event.sequence),
        ).fetchone()
        if by_sequence is not None:
            if by_sequence["event_json"] != event_json:
                raise PersistenceConflictError(
                    f"run {event.run_id!r} sequence {event.sequence} already contains different data"
                )
            return event

        try:
            self.connection.execute(
                "INSERT INTO agent_events(run_id, sequence, event_id, event_json) VALUES (?, ?, ?, ?)",
                (event.run_id, event.sequence, event.event_id, event_json),
            )
        except sqlite3.IntegrityError:
            # Another process may have won the append between the reads above.
            # Re-read both identities and preserve idempotency semantics.
            winner = self.connection.execute(
                "SELECT event_json FROM agent_events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if winner is None:
                winner = self.connection.execute(
                    "SELECT event_json FROM agent_events WHERE run_id = ? AND sequence = ?",
                    (event.run_id, event.sequence),
                ).fetchone()
            if winner is None or winner["event_json"] != event_json:
                raise PersistenceConflictError(
                    f"event identity for {event.event_id!r} conflicts with durable data"
                )
        return event

    async def append(self, event: AgentEvent) -> AgentEvent:
        with self.connection:
            return self._append_locked(event)

    async def append_event(self, event: AgentEvent) -> AgentEvent:
        """Protocol spelling used by remote DurableRunStore implementations."""

        return await self.append(event)

    def _insert_outbox_locked(self, message: OutboxRecord) -> OutboxRecord:
        event_json = canonical_json(message.event.to_dict())
        by_id = self.connection.execute(
            "SELECT outbox_id, event_id, event_json, status FROM agent_outbox "
            "WHERE outbox_id = ?",
            (message.outbox_id,),
        ).fetchone()
        if by_id is not None:
            if (
                by_id["event_id"] != message.event_id
                or by_id["event_json"] != event_json
            ):
                raise PersistenceConflictError(
                    f"outbox_id {message.outbox_id!r} already contains different data"
                )
            if by_id["status"] == "pending" and message.status == "published":
                self.connection.execute(
                    "UPDATE agent_outbox SET status = 'published' WHERE outbox_id = ?",
                    (message.outbox_id,),
                )
                return OutboxRecord(message.outbox_id, message.event, "published")
            return OutboxRecord(message.outbox_id, message.event, by_id["status"])
        existing = self.connection.execute(
            "SELECT outbox_id, event_json, status FROM agent_outbox WHERE event_id = ?",
            (message.event_id,),
        ).fetchone()
        if existing is not None:
            if existing["outbox_id"] != message.outbox_id or existing["event_json"] != event_json:
                raise PersistenceConflictError(
                    f"outbox event {message.event_id!r} already contains different data"
                )
            # Delivery status is monotonic.  A replayed append may carry the
            # original ``pending`` value after a publisher has acknowledged
            # it; never regress that acknowledgement or treat it as a data
            # conflict.
            if existing["status"] == "pending" and message.status == "published":
                self.connection.execute(
                    "UPDATE agent_outbox SET status = 'published' WHERE event_id = ?",
                    (message.event_id,),
                )
                return message
            return OutboxRecord(message.outbox_id, message.event, existing["status"])
        self.connection.execute(
            "INSERT INTO agent_outbox(outbox_id, run_id, sequence, event_id, "
            "event_json, status) VALUES (?, ?, ?, ?, ?, ?)",
            (
                message.outbox_id,
                message.run_id,
                message.sequence,
                message.event_id,
                event_json,
                message.status,
            ),
        )
        return message

    async def append_with_outbox(
        self, event: AgentEvent, *, outbox: OutboxRecord | None = None
    ) -> AgentEvent:
        """Atomically append one event and its publication envelope."""

        message = outbox or OutboxRecord(f"{event.run_id}:{event.sequence}", event)
        if message.event != event:
            raise ValueError("outbox event must match appended event")
        with self.connection:
            self._append_locked(event)
            self._insert_outbox_locked(message)
        return event

    async def append_many(self, events: Iterable[AgentEvent]) -> tuple[AgentEvent, ...]:
        with self.connection:
            appended = tuple(events)
            for event in appended:
                self._append_locked(event)
                self._insert_outbox_locked(
                    OutboxRecord(f"{event.run_id}:{event.sequence}", event)
                )
            return appended

    async def register_run(self, run_id: str, request: dict) -> None:
        request_json = canonical_json(request)
        with self.connection:
            row = self.connection.execute(
                "SELECT request_json FROM run_requests WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is not None:
                if row["request_json"] != request_json:
                    raise PersistenceConflictError(
                        f"run_id {run_id!r} already contains a different request"
                    )
                return
            self.connection.execute(
                "INSERT INTO run_requests(run_id, request_json) VALUES (?, ?)",
                (run_id, request_json),
            )

    async def get_run_request(self, run_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT request_json FROM run_requests WHERE run_id = ?", (run_id,)
        ).fetchone()
        return decode_json(row["request_json"]) if row is not None else None

    async def find_run(self, session_id: str, request_id: str) -> str | None:
        rows = self.connection.execute(
            "SELECT run_id, request_json FROM run_requests"
        ).fetchall()
        for row in rows:
            request = decode_json(row["request_json"])
            if (
                request.get("session_id") == session_id
                and request.get("request_id") == request_id
            ):
                return str(row["run_id"])
        return None

    async def events_after(self, run_id: str, sequence: int) -> tuple[AgentEvent, ...]:
        rows = self.connection.execute(
            "SELECT event_json FROM agent_events WHERE run_id = ? AND sequence > ? ORDER BY sequence",
            (run_id, sequence),
        ).fetchall()
        return tuple(_event_from_dict(decode_json(row["event_json"])) for row in rows)

    async def events_for_session(self, session_id: str) -> tuple[AgentEvent, ...]:
        """Return session facts in append order for a fresh-turn rebuild.

        Run sequences intentionally restart at one for every Run, so a
        session-level replay cannot order by ``sequence`` alone.  SQLite's
        append rowid provides the durable insertion order while the JSON
        payload remains the single event schema source of truth.
        """

        rows = self.connection.execute(
            "SELECT event_json FROM agent_events ORDER BY rowid"
        ).fetchall()
        events: list[AgentEvent] = []
        for row in rows:
            event = _event_from_dict(decode_json(row["event_json"]))
            if event.session_id == session_id:
                events.append(event)
        return tuple(events)

    async def get_control_ack(self, run_id: str, command_id: str) -> dict | None:
        """Read an idempotent control ACK persisted for a Run."""

        row = self.connection.execute(
            "SELECT digest, ack_json FROM control_commands WHERE run_id = ? AND command_id = ?",
            (run_id, command_id),
        ).fetchone()
        if row is None:
            return None
        return {"digest": row["digest"], "ack": decode_json(row["ack_json"])}

    async def put_control_ack(
        self,
        run_id: str,
        command_id: str,
        digest: str,
        ack: dict,
    ) -> None:
        """Persist a control result with conflict detection."""

        ack_json = canonical_json(ack)
        with self.connection:
            row = self.connection.execute(
                "SELECT digest, ack_json FROM control_commands WHERE run_id = ? AND command_id = ?",
                (run_id, command_id),
            ).fetchone()
            if row is not None:
                if row["digest"] != digest or row["ack_json"] != ack_json:
                    raise PersistenceConflictError(
                        f"command_id {command_id!r} already contains a different ACK"
                    )
                return
            self.connection.execute(
                "INSERT INTO control_commands(run_id, command_id, digest, ack_json) VALUES (?, ?, ?, ?)",
                (run_id, command_id, digest, ack_json),
            )

    async def commit_checkpoint(
        self,
        checkpoint: RunCheckpoint,
        *,
        events: Iterable[AgentEvent] = (),
        outbox: Iterable[OutboxRecord] = (),
        effects: Iterable[EffectRecord] = (),
    ) -> RunCheckpoint:
        events = tuple(events)
        outbox = tuple(outbox)
        effects = tuple(effects)
        for event in events:
            if event.run_id != checkpoint.run_id:
                raise ValueError("checkpoint and event run_id must match")
        for message in outbox:
            if message.run_id != checkpoint.run_id:
                raise ValueError("checkpoint and outbox run_id must match")
        for effect in effects:
            if effect.run_id != checkpoint.run_id:
                raise ValueError("checkpoint and effect run_id must match")
        with self.connection:
            for event in events:
                self._append_locked(event)
            for message in outbox:
                self._insert_outbox_locked(message)
            for effect in effects:
                self._upsert_effect_locked(effect)
            existing = self.connection.execute(
                "SELECT * FROM run_checkpoints WHERE checkpoint_id = ?",
                (checkpoint.checkpoint_id,),
            ).fetchone()
            if existing is not None:
                if _checkpoint_from_row(existing) != checkpoint:
                    raise PersistenceConflictError(
                        f"checkpoint_id {checkpoint.checkpoint_id!r} already contains different data"
                    )
                return checkpoint
            self.connection.execute(
                """INSERT INTO run_checkpoints(
                    checkpoint_id, run_id, session_id, sequence, state_json,
                    event_hash, schema_version, plugin_lock_json,
                    plugin_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint.checkpoint_id,
                    checkpoint.run_id,
                    checkpoint.session_id,
                    checkpoint.sequence,
                    canonical_json(checkpoint.state),
                    checkpoint.event_hash,
                    checkpoint.schema_version,
                    canonical_json(checkpoint.plugin_lock),
                    canonical_json(checkpoint.plugin_snapshot),
                ),
            )
        return checkpoint

    def _upsert_effect_locked(self, effect: EffectRecord) -> EffectRecord:
        """Apply one effect transition inside the event boundary transaction.

        ``SQLiteEffectLedger`` remains the public effect port, but terminal
        transitions emitted by the Kernel are written through this method so
        the corresponding ``tool.call.completed`` event, checkpoint, outbox,
        and effect row commit or roll back together.
        """

        row = self.connection.execute(
            "SELECT * FROM effect_ledger WHERE effect_id = ?", (effect.effect_id,)
        ).fetchone()
        if row is None:
            self.connection.execute(
                """INSERT INTO effect_ledger(
                    effect_id, run_id, idempotency_key, request_digest, status, result_json
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    effect.effect_id,
                    effect.run_id,
                    effect.idempotency_key,
                    effect.request_digest,
                    effect.status.value,
                    canonical_json(dict(effect.result)) if effect.result is not None else None,
                ),
            )
            return effect

        existing = _effect_from_row(row)
        if (
            existing.run_id != effect.run_id
            or existing.idempotency_key != effect.idempotency_key
            or existing.request_digest != effect.request_digest
        ):
            raise PersistenceConflictError(
                f"effect_id {effect.effect_id!r} already contains a different request"
            )
        # A terminal effect is immutable. Replaying the same boundary is
        # idempotent; attempting to rewrite it is a durable conflict.
        if existing.status in {
            EffectStatus.SUCCEEDED,
            EffectStatus.FAILED,
            EffectStatus.UNKNOWN,
        }:
            if existing.status != effect.status or existing.result != effect.result:
                raise PersistenceConflictError(
                    f"effect_id {effect.effect_id!r} is already terminal"
                )
            return existing
        self.connection.execute(
            "UPDATE effect_ledger SET status = ?, result_json = ? WHERE effect_id = ?",
            (
                effect.status.value,
                canonical_json(dict(effect.result)) if effect.result is not None else None,
                effect.effect_id,
            ),
        )
        return effect

    async def outbox_after(
        self, run_id: str, sequence: int = 0
    ) -> tuple[OutboxRecord, ...]:
        rows = self.connection.execute(
            "SELECT * FROM agent_outbox WHERE run_id = ? AND sequence > ? "
            "ORDER BY sequence",
            (run_id, sequence),
        ).fetchall()
        return tuple(_outbox_from_row(row) for row in rows)

    async def mark_outbox_published(self, outbox_id: str) -> OutboxRecord | None:
        """Acknowledge delivery without changing the immutable event fact."""

        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM agent_outbox WHERE outbox_id = ?", (outbox_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "published":
                self.connection.execute(
                    "UPDATE agent_outbox SET status = 'published' WHERE outbox_id = ?",
                    (outbox_id,),
                )
                row = self.connection.execute(
                    "SELECT * FROM agent_outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
            return _outbox_from_row(row)

    async def load_recovery_bundle(self, run_id: str) -> RecoveryBundle:
        checkpoint_row = self.connection.execute(
            "SELECT * FROM run_checkpoints WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        event_rows = self.connection.execute(
            "SELECT event_json FROM agent_events WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        effect_rows = self.connection.execute(
            "SELECT * FROM effect_ledger WHERE run_id = ? ORDER BY effect_id",
            (run_id,),
        ).fetchall()
        return RecoveryBundle(
            checkpoint=_checkpoint_from_row(checkpoint_row) if checkpoint_row else None,
            events=tuple(_event_from_dict(decode_json(row["event_json"])) for row in event_rows),
            effects=tuple(_effect_from_row(row) for row in effect_rows),
        )
