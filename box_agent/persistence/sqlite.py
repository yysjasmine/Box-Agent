"""Small SQLite foundation shared by persistence implementations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_json(value: str) -> Any:
    return json.loads(value)


class SQLiteStore:
    """Own one connection and create all durable tables on first use."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_events (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_json TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS run_requests (
                run_id TEXT PRIMARY KEY,
                request_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                plugin_lock_json TEXT NOT NULL,
                plugin_snapshot_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS agent_outbox (
                outbox_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            );
            CREATE INDEX IF NOT EXISTS agent_outbox_run_sequence
                ON agent_outbox (run_id, sequence);
            CREATE TABLE IF NOT EXISTS effect_ledger (
                effect_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS effect_run_idempotency
                ON effect_ledger (run_id, idempotency_key);
            CREATE TABLE IF NOT EXISTS run_leases (
                run_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_sessions (
                session_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                closed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS control_commands (
                run_id TEXT NOT NULL,
                command_id TEXT NOT NULL,
                digest TEXT NOT NULL,
                ack_json TEXT NOT NULL,
                PRIMARY KEY (run_id, command_id)
            );
            """
        )
        checkpoint_columns = {
            row["name"]
            for row in self._connection.execute(
                "PRAGMA table_info(run_checkpoints)"
            ).fetchall()
        }
        if "plugin_snapshot_json" not in checkpoint_columns:
            self._connection.execute(
                "ALTER TABLE run_checkpoints ADD COLUMN plugin_snapshot_json TEXT NOT NULL DEFAULT '{}'"
            )
        self._connection.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def shutdown(self) -> None:
        """Release the database resource owned by this adapter.

        Persistence ports may use ``close`` for a domain operation (for
        example, closing a logical session).  Resource lifecycle therefore
        has its own unambiguous verb and must not rely on polymorphic
        dispatch through a domain port method.
        """

        self._connection.close()

    def close(self) -> None:
        """Backward-compatible resource alias for stores without a domain close."""

        self.shutdown()

    def __del__(self) -> None:  # pragma: no cover - best effort interpreter cleanup
        try:
            self.shutdown()
        except Exception:
            pass
