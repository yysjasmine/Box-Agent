"""SQLite-backed logical session store."""

from __future__ import annotations

from box_agent.api import SessionInfo

from .sqlite import SQLiteStore, canonical_json, decode_json


class SQLiteSessionStore(SQLiteStore):
    """Persist session identity and metadata independently of run events."""

    async def get(self, session_id: str) -> SessionInfo | None:
        row = self.connection.execute(
            "SELECT * FROM agent_sessions WHERE session_id = ? AND closed = 0",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return SessionInfo(
            session_id=row["session_id"],
            created_at=row["created_at"],
            metadata=decode_json(row["metadata_json"]),
        )

    async def put(self, session: SessionInfo) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT * FROM agent_sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["created_at"] != session.created_at
                    or decode_json(row["metadata_json"]) != dict(session.metadata)
                ):
                    raise ValueError(
                        f"session_id {session.session_id!r} already contains different data"
                    )
                self.connection.execute(
                    "UPDATE agent_sessions SET closed = 0 WHERE session_id = ?",
                    (session.session_id,),
                )
                return
            self.connection.execute(
                "INSERT INTO agent_sessions(session_id, created_at, metadata_json, closed) VALUES (?, ?, ?, 0)",
                (session.session_id, session.created_at, canonical_json(session.metadata)),
            )

    async def update(self, session: SessionInfo) -> None:
        """Replace session metadata without changing its durable identity."""

        with self.connection:
            row = self.connection.execute(
                "SELECT created_at FROM agent_sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown session_id: {session.session_id!r}")
            if row["created_at"] != session.created_at:
                raise ValueError(
                    f"session_id {session.session_id!r} has a different creation time"
                )
            self.connection.execute(
                "UPDATE agent_sessions SET metadata_json = ?, closed = 0 WHERE session_id = ?",
                (canonical_json(session.metadata), session.session_id),
            )

    async def close(self, session_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE agent_sessions SET closed = 1 WHERE session_id = ?",
                (session_id,),
            )


__all__ = ["SQLiteSessionStore"]
