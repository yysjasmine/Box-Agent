"""SQLite lease and fencing-token store."""

from __future__ import annotations

import time
from pathlib import Path

from .api import Lease, LeaseConflictError
from .sqlite import SQLiteStore


class SQLiteLeaseStore(SQLiteStore):
    """Ensure at most one live worker executes a Run at a time."""

    async def acquire(self, run_id: str, *, owner_id: str, ttl_seconds: float) -> Lease:
        if not owner_id.strip():
            raise ValueError("owner_id must be a non-empty string")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = time.time()
        expires_at = now + ttl_seconds
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is not None and row["expires_at"] > now:
                raise LeaseConflictError(
                    f"run {run_id!r} is leased by {row['owner_id']!r}"
                )
            epoch = (int(row["epoch"]) + 1) if row is not None else 1
            self.connection.execute(
                """INSERT INTO run_leases(run_id, owner_id, epoch, expires_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     owner_id = excluded.owner_id,
                     epoch = excluded.epoch,
                     expires_at = excluded.expires_at""",
                (run_id, owner_id, epoch, expires_at),
            )
        return Lease(run_id=run_id, owner_id=owner_id, epoch=epoch, expires_at=expires_at)

    async def renew(self, lease: Lease, *, ttl_seconds: float) -> Lease:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = time.time()
        expires_at = now + ttl_seconds
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM run_leases WHERE run_id = ?", (lease.run_id,)
            ).fetchone()
            if (
                row is None
                or row["owner_id"] != lease.owner_id
                or int(row["epoch"]) != lease.epoch
                or float(row["expires_at"]) <= now
            ):
                raise LeaseConflictError(f"stale lease for run {lease.run_id!r}")
            self.connection.execute(
                "UPDATE run_leases SET expires_at = ? WHERE run_id = ?",
                (expires_at, lease.run_id),
            )
        return Lease(
            run_id=lease.run_id,
            owner_id=lease.owner_id,
            epoch=lease.epoch,
            expires_at=expires_at,
        )

    async def release(self, lease: Lease) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE run_leases SET expires_at = 0
                   WHERE run_id = ? AND owner_id = ? AND epoch = ? AND expires_at > 0""",
                (lease.run_id, lease.owner_id, lease.epoch),
            )
        return cursor.rowcount == 1

    async def get(self, run_id: str) -> Lease | None:
        row = self.connection.execute(
            "SELECT * FROM run_leases WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        if float(row["expires_at"]) <= time.time():
            return None
        return Lease(
            run_id=row["run_id"],
            owner_id=row["owner_id"],
            epoch=int(row["epoch"]),
            expires_at=float(row["expires_at"]),
        )
