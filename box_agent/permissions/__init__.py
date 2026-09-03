"""Pluggable permission gateway capability."""

from .gateway import DenyAllPermissionGateway
from .session import (
    SessionPermissionProfile,
    SessionPermissionResolver,
    build_file_access_prompt,
)

__all__ = [
    "DenyAllPermissionGateway",
    "SessionPermissionProfile",
    "SessionPermissionResolver",
    "build_file_access_prompt",
]
