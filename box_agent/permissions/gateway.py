"""Permission Gateway implementations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from box_agent.api import PermissionDecision, PermissionRequest


class DenyAllPermissionGateway:
    """Safe default for hosts that have not supplied an approval UI/policy."""

    async def decide(
        self, request: PermissionRequest | Mapping[str, Any]
    ) -> PermissionDecision:
        return PermissionDecision(
            granted=False,
            reason="no permission gateway policy is configured",
        )


__all__ = ["DenyAllPermissionGateway"]
