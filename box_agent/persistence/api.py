"""Durable state contracts used to resume an Agent Run safely.

The persistence layer deliberately contains data-only records.  Storage
implementations may use SQLite, a service, or an embedded database without
changing the Agent Loop protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Protocol

from box_agent.api import AgentEvent


class PersistenceConflictError(RuntimeError):
    """Raised when the same durable identity is reused with different data."""


class LeaseConflictError(RuntimeError):
    """Raised when another live worker owns the Run lease."""


class EffectStatus(str, Enum):
    """State of an externally visible side effect."""

    PREPARED = "prepared"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    """Atomic snapshot of loop state at an event sequence."""

    checkpoint_id: str
    session_id: str
    run_id: str
    sequence: int
    state: Mapping[str, Any]
    event_hash: str
    schema_version: str = "1"
    plugin_lock: Mapping[str, str] = field(default_factory=dict)
    plugin_snapshot: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, name in (
            (self.checkpoint_id, "checkpoint_id"),
            (self.session_id, "session_id"),
            (self.run_id, "run_id"),
            (self.event_hash, "event_hash"),
            (self.schema_version, "schema_version"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        if not isinstance(self.state, Mapping):
            raise ValueError("state must be a mapping")
        if not isinstance(self.plugin_lock, Mapping):
            raise ValueError("plugin_lock must be a mapping")
        if not isinstance(self.plugin_snapshot, Mapping):
            raise ValueError("plugin_snapshot must be a mapping")
        normalized_snapshot: dict[str, Mapping[str, Any]] = {}
        for key, value in self.plugin_snapshot.items():
            if not isinstance(value, Mapping):
                raise ValueError("plugin_snapshot entries must be mappings")
            normalized_snapshot[str(key)] = dict(value)
        object.__setattr__(self, "state", dict(self.state))
        object.__setattr__(self, "plugin_lock", dict(self.plugin_lock))
        object.__setattr__(
            self,
            "plugin_snapshot",
            normalized_snapshot,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "state": dict(self.state),
            "event_hash": self.event_hash,
            "schema_version": self.schema_version,
            "plugin_lock": dict(self.plugin_lock),
            "plugin_snapshot": {
                key: dict(value) for key, value in self.plugin_snapshot.items()
            },
        }


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """One event waiting to be published after its durable boundary commits."""

    outbox_id: str
    event: AgentEvent
    status: str = "pending"

    def __post_init__(self) -> None:
        if not isinstance(self.outbox_id, str) or not self.outbox_id.strip():
            raise ValueError("outbox_id must be a non-empty string")
        if not isinstance(self.event, AgentEvent):
            raise ValueError("outbox event must be an AgentEvent")
        if self.status not in {"pending", "published"}:
            raise ValueError("outbox status must be pending or published")

    @property
    def event_id(self) -> str:
        return self.event.event_id

    @property
    def run_id(self) -> str:
        return self.event.run_id

    @property
    def sequence(self) -> int:
        return self.event.sequence

    def to_dict(self) -> dict[str, Any]:
        return {
            "outbox_id": self.outbox_id,
            "event": self.event.to_dict(),
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class EffectRecord:
    """Idempotency and reconciliation record for one external effect."""

    effect_id: str
    run_id: str
    idempotency_key: str
    request_digest: str
    status: EffectStatus = EffectStatus.PREPARED
    result: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.effect_id, "effect_id"),
            (self.run_id, "run_id"),
            (self.idempotency_key, "idempotency_key"),
            (self.request_digest, "request_digest"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.status, EffectStatus):
            object.__setattr__(self, "status", EffectStatus(self.status))
        if self.result is not None:
            object.__setattr__(self, "result", dict(self.result))

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id,
            "run_id": self.run_id,
            "idempotency_key": self.idempotency_key,
            "request_digest": self.request_digest,
            "status": self.status.value,
            "result": dict(self.result) if self.result is not None else None,
        }


@dataclass(frozen=True, slots=True)
class Lease:
    """Fencing token held by exactly one worker for a Run."""

    run_id: str
    owner_id: str
    epoch: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class RecoveryBundle:
    """Durable material required to reconstruct a Run."""

    checkpoint: RunCheckpoint | None
    events: tuple[AgentEvent, ...]
    effects: tuple[EffectRecord, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "effects", tuple(self.effects))


class EventLog(Protocol):
    async def append(self, event: AgentEvent) -> AgentEvent: ...

    async def append_event(self, event: AgentEvent) -> AgentEvent: ...

    async def append_with_outbox(
        self, event: AgentEvent, *, outbox: OutboxRecord | None = None
    ) -> AgentEvent: ...

    async def events_after(self, run_id: str, sequence: int) -> tuple[AgentEvent, ...]: ...

    async def events_for_session(self, session_id: str) -> tuple[AgentEvent, ...]: ...

    async def load_recovery_bundle(self, run_id: str) -> RecoveryBundle: ...

    async def commit_checkpoint(
        self,
        checkpoint: RunCheckpoint,
        *,
        events: Iterable[AgentEvent] = (),
        outbox: Iterable[OutboxRecord] = (),
        effects: Iterable[EffectRecord] = (),
    ) -> RunCheckpoint: ...

    async def outbox_after(
        self, run_id: str, sequence: int = 0
    ) -> tuple[OutboxRecord, ...]: ...

    async def mark_outbox_published(self, outbox_id: str) -> OutboxRecord | None: ...


class ControlAckStore(Protocol):
    """Optional durable idempotency store for host control commands."""

    async def get_control_ack(self, run_id: str, command_id: str) -> Mapping[str, Any] | None: ...

    async def put_control_ack(
        self, run_id: str, command_id: str, digest: str, ack: Mapping[str, Any]
    ) -> None: ...


class DurableRunStore(EventLog, Protocol):
    """Full durable store required by a restartable Agent Service."""

    async def register_run(self, run_id: str, request: Mapping[str, Any]) -> None: ...

    async def get_run_request(self, run_id: str) -> Mapping[str, Any] | None: ...

    async def find_run(self, session_id: str, request_id: str) -> str | None: ...


class EffectLedger(Protocol):
    async def prepare(
        self,
        *,
        effect_id: str,
        run_id: str,
        idempotency_key: str,
        request_digest: str,
    ) -> EffectRecord: ...

    async def complete(
        self,
        *,
        effect_id: str,
        status: EffectStatus,
        result: Mapping[str, Any] | None = None,
    ) -> EffectRecord: ...

    async def reconcile(self, effect_id: str) -> EffectRecord | None: ...


class LeaseStore(Protocol):
    async def acquire(self, run_id: str, *, owner_id: str, ttl_seconds: float) -> Lease: ...

    async def renew(self, lease: Lease, *, ttl_seconds: float) -> Lease: ...

    async def release(self, lease: Lease) -> bool: ...
