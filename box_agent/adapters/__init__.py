"""Thin ACP/CLI/SDK adapters over the stable Agent Service."""

from .service import ServiceAdapter, request_from_payload
from .capabilities import LLMClientPort, MemoryManagerEngine
from .plugin_host import (
    PermissionNegotiatorGateway,
    build_kernel_service,
    build_plugin_host,
)
from .hosts import ACPServiceAdapter, CLIServiceAdapter, SDKServiceAdapter
from .acp_kernel import ACPPermissionGateway, KernelACPAgent
from .extensions import HostExtensionContext, HostExtensionRouter
from .projections import PostRunProjectionManager, PostRunProjectionRequest

__all__ = [
    "LLMClientPort",
    "MemoryManagerEngine",
    "PermissionNegotiatorGateway",
    "ACPServiceAdapter",
    "ACPPermissionGateway",
    "KernelACPAgent",
    "HostExtensionContext",
    "HostExtensionRouter",
    "PostRunProjectionManager",
    "PostRunProjectionRequest",
    "CLIServiceAdapter",
    "SDKServiceAdapter",
    "ServiceAdapter",
    "build_kernel_service",
    "build_plugin_host",
    "request_from_payload",
]
