"""Plugin lifecycle and typed capability registries."""

from .api import Plugin, PluginContext, PluginManifest, PluginScope, Registration
from .host import (
    DEFAULT_PLUGIN_ENTRYPOINT_GROUP,
    DEFAULT_REGISTRY_KINDS,
    REGISTRY_ALIASES,
    PluginHost,
)
from .registry import (
    PluginConflictError,
    PluginDependencyCycleError,
    PluginDependencyError,
    TypedRegistry,
)

__all__ = [
    "Plugin",
    "PluginConflictError",
    "PluginContext",
    "PluginDependencyCycleError",
    "PluginDependencyError",
    "PluginHost",
    "DEFAULT_REGISTRY_KINDS",
    "REGISTRY_ALIASES",
    "DEFAULT_PLUGIN_ENTRYPOINT_GROUP",
    "PluginManifest",
    "PluginScope",
    "Registration",
    "TypedRegistry",
]
