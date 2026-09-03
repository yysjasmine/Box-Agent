"""Stable permission request and decision values."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """A capability request that must be explicitly decided by a host."""

    scope: str
    reason: str
    requested_scope: str = ""
    resource: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.scope.strip() or not self.reason.strip():
            raise ValueError("permission scope and reason are required")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "reason": self.reason,
            "requested_scope": self.requested_scope,
            "resource": self.resource,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """Host decision for one permission request."""

    granted: bool
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "granted": self.granted,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }
