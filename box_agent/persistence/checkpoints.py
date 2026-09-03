"""Checkpoint-facing aliases kept separate from the event-log implementation."""

from .api import RecoveryBundle, RunCheckpoint
from .event_log import SQLiteEventLog


class SQLiteCheckpointStore(SQLiteEventLog):
    """Checkpoint-focused view over the same atomic SQLite Run store."""

    pass

__all__ = ["RecoveryBundle", "RunCheckpoint", "SQLiteCheckpointStore"]
