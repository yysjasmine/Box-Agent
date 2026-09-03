"""Plugin discovery-independent host and lifecycle coordinator."""

from __future__ import annotations

import re
import inspect
from collections.abc import Iterable, Mapping
from importlib.metadata import entry_points
from typing import Any

from .api import Plugin, PluginContext
from .registry import PluginDependencyCycleError, PluginDependencyError, TypedRegistry


DEFAULT_REGISTRY_KINDS = (
    "context",
    "tools",
    "permissions",
    "memory",
    "sessions",
    "effects",
    "llm",
    "hooks",
    "workflows",
    "workflow.selectors",
    "host.extensions",
    "host.projections",
    "artifacts.processors",
    "session.metadata",
    "session.lifecycle",
    "control.routes",
    # Canonical typed registration points. Historical short names above are
    # aliases of these registries, not parallel storage.
    "context.providers",
    "context.compactors",
    "context.contributors",
    "tools.descriptors",
    "tools.executors",
    "tools.engines",
    "tools.session_contributors",
    "permission.policies",
    "permission.brokers",
    "memory.providers",
    "memory.stores",
    "memory.writers",
    "llm.providers",
)

# Historical names remain writable compatibility views, but they must never
# become a second registry or require dual writes.  Every alias below points
# to the exact canonical TypedRegistry instance.
REGISTRY_ALIASES = {
    "context": "context.providers",
    "tools": "tools.executors",
    "permissions": "permission.policies",
    "memory": "memory.providers",
    "llm": "llm.providers",
}

DEFAULT_PLUGIN_ENTRYPOINT_GROUP = "box_agent.plugins"


class PluginHost:
    """Activate plugins against typed registries and clean them up safely."""

    def __init__(
        self,
        *,
        registries: Mapping[str, TypedRegistry] | None = None,
        config: Mapping[str, Any] | None = None,
        services: Mapping[str, Any] | None = None,
    ) -> None:
        provided = dict(registries or {})
        for alias, canonical in REGISTRY_ALIASES.items():
            alias_registry = provided.get(alias)
            canonical_registry = provided.get(canonical)
            if (
                alias_registry is not None
                and canonical_registry is not None
                and alias_registry is not canonical_registry
            ):
                raise ValueError(
                    f"'{alias}' and '{canonical}' must reference the same registry instance"
                )
            registry = canonical_registry or alias_registry or TypedRegistry(canonical)
            provided[alias] = registry
            provided[canonical] = registry
        self._registries = provided
        for kind in DEFAULT_REGISTRY_KINDS:
            self._registries.setdefault(kind, TypedRegistry(kind))
        self._config = dict(config or {})
        self._services = dict(services or {})
        self._plugins: dict[str, Plugin] = {}

    @property
    def plugins(self) -> tuple[Plugin, ...]:
        return tuple(self._plugins.values())

    @property
    def registries(self) -> Mapping[str, TypedRegistry]:
        """Read-only view used to build a ``PluginContext`` or inspect wiring."""

        return dict(self._registries)

    @classmethod
    def discover(
        cls,
        *,
        groups: Iterable[str] = (DEFAULT_PLUGIN_ENTRYPOINT_GROUP,),
    ) -> tuple[Plugin, ...]:
        """Discover plugin factories exported through Python entry points.

        Discovery is deliberately separate from activation: loading a
        distribution must not mutate registries or global runtime state. The
        returned instances can be passed to :meth:`activate_many`, which then
        performs dependency validation and transactional activation.

        An entry point may expose a plugin instance, a plugin class, or a
        zero-argument factory. Every loaded value must provide a
        ``PluginManifest`` through ``manifest``; malformed values fail closed
        before any plugin is activated.
        """

        discovered: list[tuple[str, str, Any]] = []
        for group in tuple(groups):
            if not isinstance(group, str) or not group.strip():
                raise ValueError("plugin entry-point groups must be non-empty strings")
            selected = _select_entry_points(group)
            for endpoint in selected:
                discovered.append(
                    (
                        str(getattr(endpoint, "name", "")),
                        str(getattr(endpoint, "value", "")),
                        endpoint,
                    )
                )

        plugins: list[Plugin] = []
        seen_names: set[str] = set()
        for _, _, endpoint in sorted(discovered, key=lambda item: (item[0], item[1])):
            endpoint_name = str(getattr(endpoint, "name", "<unknown>"))
            if endpoint_name in seen_names:
                raise PluginDependencyError(
                    f"duplicate plugin entry point '{endpoint_name}'"
                )
            seen_names.add(endpoint_name)
            try:
                value = endpoint.load()
                if isinstance(value, type):
                    value = value()
                elif callable(value) and not hasattr(value, "manifest"):
                    value = value()
            except Exception as exc:
                raise PluginDependencyError(
                    f"failed to load plugin entry point '{endpoint_name}': "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            _validate_plugin_value(value, endpoint_name)
            plugins.append(value)
        return tuple(plugins)

    def lock_snapshot(self) -> dict[str, str]:
        """Return plugin and registry versions used to fence a Run replay.

        Most production registrations come from :class:`Plugin` manifests,
        but the low-level registry API is intentionally public for lightweight
        integrations. Include those entries in the lock as well so a restart
        cannot silently replay a Run with a different capability graph. Hosts
        that need code-level fencing should pass an explicit ``version`` when
        registering a raw capability.
        """

        lock = {
            plugin.manifest.id: plugin.manifest.version
            for plugin in sorted(self._plugins.values(), key=lambda item: item.manifest.id)
        }
        for kind in sorted(set(self._registries) - set(REGISTRY_ALIASES)):
            registry = self._registries[kind]
            registrations = sorted(
                registry.registrations(),
                key=lambda item: (item.scope, item.key, item.source),
            )
            for registration in registrations:
                lock_key = (
                    f"registration:{kind}:{registration.scope}:{registration.key}"
                )
                lock[lock_key] = (
                    f"{registration.source}@{registration.version}"
                    f";schema={registration.state_schema}"
                )
        return lock

    async def snapshot(self) -> dict[str, dict[str, Any]]:
        """Capture deterministic state for every active plugin.

        Plugins without state are still represented so a replay can verify
        that the same version/schema was active at the checkpoint.
        """

        snapshots: dict[str, dict[str, Any]] = {}
        for plugin in sorted(self._plugins.values(), key=lambda item: item.manifest.id):
            capture = getattr(plugin, "snapshot", None)
            state: Any = {}
            if callable(capture):
                state = capture()
                if inspect.isawaitable(state):
                    state = await state
            snapshots[plugin.manifest.id] = {
                "version": plugin.manifest.version,
                "schema_version": plugin.manifest.state_schema,
                "state": state if isinstance(state, Mapping) else state,
            }
        return snapshots

    async def restore_snapshot(
        self, snapshot: Mapping[str, Mapping[str, Any]] | None
    ) -> None:
        """Restore plugin state after validating version and schema fences."""

        for plugin_id, record in (snapshot or {}).items():
            plugin = self._plugins.get(str(plugin_id))
            if plugin is None:
                raise PluginDependencyError(
                    f"snapshot references inactive plugin '{plugin_id}'"
                )
            expected_version = str(record.get("version", ""))
            expected_schema = str(record.get("schema_version", ""))
            if expected_version != plugin.manifest.version:
                raise PluginDependencyError(
                    f"plugin '{plugin_id}' snapshot version does not match active plugin"
                )
            if expected_schema != plugin.manifest.state_schema:
                raise PluginDependencyError(
                    f"plugin '{plugin_id}' snapshot schema does not match active plugin"
                )
            restore = getattr(plugin, "restore", None)
            state = record.get("state")
            if callable(restore):
                value = restore(state)
                if inspect.isawaitable(value):
                    await value
            elif state not in (None, {}, []):
                raise PluginDependencyError(
                    f"plugin '{plugin_id}' cannot restore non-empty state"
                )

    async def activate(self, plugin: Plugin) -> None:
        _validate_plugin_value(plugin, "plugin")
        manifest = plugin.manifest
        if manifest.id in self._plugins:
            raise PluginDependencyError(f"plugin '{manifest.id}' is already active")
        missing = [dependency for dependency in manifest.requires if dependency not in self._plugins]
        if missing:
            raise PluginDependencyError(
                f"plugin '{manifest.id}' is missing dependencies: {', '.join(sorted(missing))}"
            )
        incompatible = [
            f"{dependency} ({constraint})"
            for dependency, constraint in manifest.requires.items()
            if not _version_satisfies(self._plugins[dependency].manifest.version, constraint)
        ]
        if incompatible:
            raise PluginDependencyError(
                f"plugin '{manifest.id}' has incompatible dependencies: {', '.join(incompatible)}"
            )

        context = PluginContext(
            registries=self._registries,
            config=self._config,
            services=self._services,
        )
        try:
            await plugin.activate(context)
        except Exception:
            for registry in self._unique_registries():
                await registry.dispose_source(manifest.id)
            dispose = getattr(plugin, "dispose", None)
            if dispose is not None:
                await dispose()
            raise
        self._plugins[manifest.id] = plugin

    async def activate_many(self, plugins: Iterable[Plugin]) -> None:
        """Activate a plugin graph in dependency order.

        Discovery code can hand the host an unordered manifest set.  The host
        resolves dependencies deterministically and reports an actual cycle
        separately from a genuinely missing dependency.
        """

        plugin_list = tuple(plugins)
        pending = {plugin.manifest.id: plugin for plugin in plugin_list}
        duplicate_ids = len(pending) != len(plugin_list)
        if duplicate_ids:
            raise PluginDependencyError("plugin graph contains duplicate ids")
        activated: list[str] = []
        try:
            while pending:
                progressed = False
                for plugin_id in sorted(tuple(pending)):
                    plugin = pending[plugin_id]
                    dependencies = set(plugin.manifest.requires)
                    missing = dependencies - set(self._plugins) - set(pending)
                    if missing:
                        raise PluginDependencyError(
                            f"plugin '{plugin_id}' is missing dependencies: {', '.join(sorted(missing))}"
                        )
                    if not dependencies.issubset(self._plugins):
                        continue
                    await self.activate(plugin)
                    activated.append(plugin_id)
                    pending.pop(plugin_id)
                    progressed = True
                if not progressed:
                    cycle_ids = ", ".join(sorted(pending))
                    raise PluginDependencyCycleError(
                        f"plugin dependency cycle detected: {cycle_ids}"
                    )
        except BaseException:
            # A graph activation is transactional from the host's point of
            # view: do not leave already-activated providers behind when a
            # later dependency is missing or cyclic.
            for plugin_id in reversed(activated):
                await self.deactivate(plugin_id)
            raise

    async def deactivate(self, plugin_id: str) -> None:
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return

        error: BaseException | None = None
        try:
            await plugin.deactivate()
        except BaseException as exc:  # pragma: no cover - exercised by integration tests
            error = exc
        finally:
            for registry in self._unique_registries():
                await registry.dispose_source(plugin_id)
            await plugin.dispose()
            self._plugins.pop(plugin_id, None)
        if error is not None:
            raise error

    async def dispose(self) -> None:
        # Dependents must release their registrations before the providers they
        # depend on.  Activation order is dependency-first, so reverse it for
        # deterministic, dependency-safe teardown.
        for plugin_id in reversed(tuple(self._plugins)):
            await self.deactivate(plugin_id)

    def _unique_registries(self) -> tuple[TypedRegistry, ...]:
        """Return physical registries once, excluding compatibility aliases."""

        return tuple(dict.fromkeys(self._registries.values()))


def _version_satisfies(version: str, constraint: str) -> bool:
    """Support the small semver subset needed by plugin manifests."""

    if not constraint or constraint.strip() in {"*", "any"}:
        return True

    def parts(value: str) -> tuple[int, ...]:
        numbers = re.findall(r"\d+", value)
        values = [int(number) for number in numbers[:3]]
        return tuple(values + [0] * (3 - len(values)))

    actual = parts(version)
    for expression in (part.strip() for part in constraint.split(",")):
        match = re.fullmatch(r"(>=|<=|>|<|==|=|~\s*)?\s*v?(\d+(?:\.\d+){0,2})", expression)
        if match is None:
            return False
        operator = (match.group(1) or "==").replace(" ", "")
        expected = parts(match.group(2))
        if operator in {"=", "=="} and actual != expected:
            return False
        if operator == ">=" and actual < expected:
            return False
        if operator == "<=" and actual > expected:
            return False
        if operator == ">" and actual <= expected:
            return False
        if operator == "<" and actual >= expected:
            return False
        if operator == "~" and (actual[0], actual[1:2]) != (expected[0], expected[1:2]):
            return False
    return True


def _select_entry_points(group: str) -> tuple[Any, ...]:
    """Return deterministic entry points across Python metadata APIs."""

    try:
        available = entry_points()
    except Exception as exc:
        raise PluginDependencyError(
            f"failed to enumerate plugin entry points: {type(exc).__name__}: {exc}"
        ) from exc
    selector = getattr(available, "select", None)
    if callable(selector):
        return tuple(selector(group=group))
    if isinstance(available, Mapping):
        return tuple(available.get(group, ()))
    return tuple(
        endpoint
        for endpoint in available
        if str(getattr(endpoint, "group", "")) == group
    )


def _validate_plugin_value(value: Any, name: str) -> None:
    manifest = getattr(value, "manifest", None)
    if manifest is None:
        raise PluginDependencyError(
            f"plugin entry '{name}' does not expose a manifest"
        )
    for attribute in ("id", "version", "state_schema"):
        field = getattr(manifest, attribute, None)
        if not isinstance(field, str) or not field.strip():
            raise PluginDependencyError(
                f"plugin entry '{name}' has an invalid manifest.{attribute}"
            )
    requires = getattr(manifest, "requires", {})
    if not isinstance(requires, Mapping) or any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(constraint, str)
        or not constraint.strip()
        for key, constraint in requires.items()
    ):
        raise PluginDependencyError(
            f"plugin entry '{name}' has invalid manifest.requires"
        )
    for method_name in ("activate", "deactivate", "dispose"):
        if not callable(getattr(value, method_name, None)):
            raise PluginDependencyError(
                f"plugin entry '{name}' does not implement lifecycle method "
                f"'{method_name}'"
            )
