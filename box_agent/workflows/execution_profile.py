"""Execution-policy profile shared by host adapters and orchestration."""

from __future__ import annotations

from typing import Final, Literal, cast

ExecutionProfile = Literal["fast", "standard", "deep"]

DEFAULT_EXECUTION_PROFILE: Final[ExecutionProfile] = "standard"
FAST_OPTIONAL_SKILLS: Final[frozenset[str]] = frozenset({"research-synthesis"})


def normalize_execution_profile(value: object) -> ExecutionProfile:
    """Normalize host metadata while preserving established session behavior."""
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"fast", "standard", "deep"}:
            return cast(ExecutionProfile, normalized)
    return DEFAULT_EXECUTION_PROFILE
