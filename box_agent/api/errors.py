"""Structured errors that cross the Agent host boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    PROTOCOL_VERSION_UNSUPPORTED = "PROTOCOL_VERSION_UNSUPPORTED"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    RUN_NOT_ACTIVE = "RUN_NOT_ACTIVE"
    COMMAND_ID_REUSED = "COMMAND_ID_REUSED"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_INVALID_ARGUMENTS = "TOOL_INVALID_ARGUMENTS"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PERMISSION_TIMEOUT = "PERMISSION_TIMEOUT"
    EFFECT_REQUIRES_RECONCILIATION = "EFFECT_REQUIRES_RECONCILIATION"
    LEASE_LOST = "LEASE_LOST"
    UNSUPPORTED_COMMAND = "UNSUPPORTED_COMMAND"
    MODEL_PROVIDER_ERROR = "MODEL_PROVIDER_ERROR"
    PLUGIN_CONFLICT = "PLUGIN_CONFLICT"
    PLUGIN_DEPENDENCY_CYCLE = "PLUGIN_DEPENDENCY_CYCLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Safe, serializable error information with no exception object."""

    code: ErrorCode | str
    category: str
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.category, str) or not self.category.strip():
            raise ValueError("category must be a non-empty string")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be a non-empty string")
        object.__setattr__(self, "details", dict(self.details))

    def to_dict(self) -> dict[str, Any]:
        code = self.code.value if isinstance(self.code, ErrorCode) else self.code
        return {
            "code": code,
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
        }
