"""SQLite idempotency ledger for external tool effects."""

from __future__ import annotations

import sqlite3
from typing import Any

from .api import EffectRecord, EffectStatus, PersistenceConflictError
from .sqlite import SQLiteStore, canonical_json, decode_json


def _record_from_row(row) -> EffectRecord:
    return EffectRecord(
        effect_id=row["effect_id"],
        run_id=row["run_id"],
        idempotency_key=row["idempotency_key"],
        request_digest=row["request_digest"],
        status=EffectStatus(row["status"]),
        result=decode_json(row["result_json"]) if row["result_json"] is not None else None,
    )


class SQLiteEffectLedger(SQLiteStore):
    """Prepare/complete/reconcile effect records across process restarts."""

    async def prepare(
        self,
        *,
        effect_id: str,
        run_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> EffectRecord:
        requested = EffectRecord(
            effect_id=effect_id,
            run_id=run_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM effect_ledger WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if row is not None:
                existing = _record_from_row(row)
                if (
                    existing.run_id != run_id
                    or existing.idempotency_key != idempotency_key
                    or existing.request_digest != request_digest
                ):
                    raise PersistenceConflictError(
                        f"effect_id {effect_id!r} already contains a different request"
                    )
                return existing
            try:
                self.connection.execute(
                    """INSERT INTO effect_ledger(
                        effect_id, run_id, idempotency_key, request_digest, status, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (effect_id, run_id, idempotency_key, request_digest, requested.status.value, None),
                )
            except sqlite3.IntegrityError:
                winner = self.connection.execute(
                    """SELECT * FROM effect_ledger
                       WHERE effect_id = ? OR (run_id = ? AND idempotency_key = ?)""",
                    (effect_id, run_id, idempotency_key),
                ).fetchone()
                if winner is None:
                    raise PersistenceConflictError(
                        f"effect identity for {effect_id!r} conflicts with durable data"
                    )
                existing = _record_from_row(winner)
                if (
                    existing.effect_id != effect_id
                    or existing.run_id != run_id
                    or existing.idempotency_key != idempotency_key
                    or existing.request_digest != request_digest
                ):
                    raise PersistenceConflictError(
                        f"effect identity for {effect_id!r} conflicts with durable data"
                    )
                return existing
        return requested

    async def complete(
        self,
        *,
        effect_id: str,
        status: EffectStatus,
        result: dict[str, Any] | None = None,
    ) -> EffectRecord:
        status = EffectStatus(status)
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM effect_ledger WHERE effect_id = ?", (effect_id,)
            ).fetchone()
            if row is None:
                raise PersistenceConflictError(f"unknown effect_id {effect_id!r}")
            existing = _record_from_row(row)
            requested_result = dict(result) if result is not None else None
            if existing.status in {
                EffectStatus.SUCCEEDED,
                EffectStatus.FAILED,
                EffectStatus.UNKNOWN,
            }:
                if existing.status != status or existing.result != requested_result:
                    raise PersistenceConflictError(
                        f"effect_id {effect_id!r} is already terminal"
                    )
                return existing
            self.connection.execute(
                "UPDATE effect_ledger SET status = ?, result_json = ? WHERE effect_id = ?",
                (
                    status.value,
                    canonical_json(requested_result) if requested_result is not None else None,
                    effect_id,
                ),
            )
            return EffectRecord(
                effect_id=existing.effect_id,
                run_id=existing.run_id,
                idempotency_key=existing.idempotency_key,
                request_digest=existing.request_digest,
                status=status,
                result=requested_result,
            )

    async def reconcile(self, effect_id: str) -> EffectRecord | None:
        row = self.connection.execute(
            "SELECT * FROM effect_ledger WHERE effect_id = ?", (effect_id,)
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    async def unresolved(self, run_id: str) -> tuple[EffectRecord, ...]:
        rows = self.connection.execute(
            """SELECT * FROM effect_ledger
               WHERE run_id = ? AND status IN (?, ?, ?)
               ORDER BY effect_id""",
            (
                run_id,
                EffectStatus.PREPARED.value,
                EffectStatus.RUNNING.value,
                EffectStatus.UNKNOWN.value,
            ),
        ).fetchall()
        return tuple(_record_from_row(row) for row in rows)
