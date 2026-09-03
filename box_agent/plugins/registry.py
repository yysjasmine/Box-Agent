"""Scoped typed registry used by plugin capabilities."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .api import PluginScope, Registration


class PluginConflictError(RuntimeError, ValueError):
    """Raised when a key is already registered in the same scope."""


class PluginDependencyError(RuntimeError):
    """Raised when a plugin dependency is unavailable."""


class PluginDependencyCycleError(PluginDependencyError):
    """Raised when activation detects a dependency cycle."""


_SCOPES: frozenset[PluginScope] = frozenset({"global", "session", "run"})


@dataclass(slots=True)
class _Entry:
    value: Any
    registration: Registration
    priority: int
    order: int


class TypedRegistry:
    """Register values by key and lifecycle scope.

    The registry deliberately has no knowledge of Agent Loop state.  It only
    owns entries and their disposal callbacks.
    """

    def __init__(self, kind: str) -> None:
        if not kind.strip():
            raise ValueError("registry kind must be a non-empty string")
        self.kind = kind
        self._entries: dict[tuple[PluginScope, str], _Entry] = {}
        self._order = 0

    def register(
        self,
        key: str,
        value: Any,
        *,
        source: str,
        scope: PluginScope = "global",
        priority: int = 0,
        version: str = "unversioned",
        state_schema: str = "1",
    ) -> Registration:
        if not key.strip():
            raise ValueError("registry key must be a non-empty string")
        if not source.strip():
            raise ValueError("registration source must be a non-empty string")
        if scope not in _SCOPES:
            raise ValueError(f"unsupported registry scope: {scope}")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("registration version must be a non-empty string")
        if not isinstance(state_schema, str) or not state_schema.strip():
            raise ValueError("registration state_schema must be a non-empty string")

        lookup_key = (scope, key)
        if lookup_key in self._entries:
            raise PluginConflictError(
                f"{self.kind} key '{key}' is already registered in scope '{scope}'"
            )

        registration = Registration(
            registration_id=uuid4().hex,
            key=key,
            source=source,
            scope=scope,
            _dispose_callback=lambda: self._dispose_registration(registration),
            version=version,
            state_schema=state_schema,
        )
        self._order += 1
        self._entries[lookup_key] = _Entry(
            value=value,
            registration=registration,
            priority=priority,
            order=self._order,
        )
        return registration

    def get(self, key: str, *, scope: PluginScope = "global") -> Any | None:
        """Return the registered value for an exact key and scope."""

        self._validate_scope(scope)
        entry = self._entries.get((scope, key))
        return entry.value if entry is not None else None

    def resolve(self, key: str, *, scope: PluginScope = "global") -> Any | None:
        """Resolve a scoped value, falling back from run to global.

        A more specific scope wins.  Priority and registration order provide a
        deterministic tie-breaker for future registry implementations that
        allow multiple candidates.
        """

        entry = self._resolve_entry(key, scope=scope)
        return entry.value if entry is not None else None

    def resolve_registration(
        self,
        key: str,
        *,
        scope: PluginScope = "global",
    ) -> Registration | None:
        """Return the lifecycle/provenance record for the resolved value."""

        entry = self._resolve_entry(key, scope=scope)
        return entry.registration if entry is not None else None

    def _resolve_entry(
        self,
        key: str,
        *,
        scope: PluginScope,
    ) -> _Entry | None:
        """Resolve value and registration through one identical scope rule."""

        self._validate_scope(scope)
        scopes: tuple[PluginScope, ...]
        if scope == "run":
            scopes = ("run", "session", "global")
        elif scope == "session":
            scopes = ("session", "global")
        else:
            scopes = ("global",)
        # Scope specificity is the first decision.  A global default must not
        # override a run-local implementation merely because it has a higher
        # numeric priority; priority only breaks ties within one scope.
        for candidate_scope in scopes:
            candidates = [
                entry
                for (entry_scope, entry_key), entry in self._entries.items()
                if entry_scope == candidate_scope and entry_key == key
            ]
            if candidates:
                return max(candidates, key=lambda entry: (entry.priority, entry.order))
        return None

    def registrations(self) -> tuple[Registration, ...]:
        return tuple(entry.registration for entry in self._entries.values())

    def validate_source_replacement(
        self,
        keys: tuple[str, ...],
        *,
        source: str,
        scope: PluginScope = "global",
    ) -> None:
        """Validate an atomic source generation without mutating the registry."""

        if not source.strip():
            raise ValueError("registration source must be a non-empty string")
        self._validate_scope(scope)
        for key in keys:
            if not isinstance(key, str) or not key.strip():
                raise ValueError("registry key must be a non-empty string")
            existing = self._entries.get((scope, key))
            if existing is not None and existing.registration.source != source:
                raise PluginConflictError(
                    f"{self.kind} key '{key}' is already registered in scope "
                    f"'{scope}'"
                )

    def replace_source(
        self,
        values: Mapping[str, Any],
        *,
        source: str,
        scope: PluginScope = "global",
        priority: int = 0,
        version: str = "unversioned",
        state_schema: str = "1",
    ) -> tuple[Registration, ...]:
        """Atomically replace every registration owned by one source.

        Dynamic capabilities such as one MCP server are a generation, not a
        bag of independent mutations. Readers therefore observe either the
        old generation or the complete replacement. Conflicts are checked
        before the registry map changes, so a failed reload preserves the
        working generation.
        """

        if not isinstance(values, Mapping):
            raise TypeError("replacement values must be a mapping")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("registration version must be a non-empty string")
        if not isinstance(state_schema, str) or not state_schema.strip():
            raise ValueError("registration state_schema must be a non-empty string")
        keys = tuple(values)
        self.validate_source_replacement(keys, source=source, scope=scope)

        replacement_entries: dict[tuple[PluginScope, str], _Entry] = {}
        registrations: list[Registration] = []
        for key, value in values.items():
            registration = Registration(
                registration_id=uuid4().hex,
                key=key,
                source=source,
                scope=scope,
                _dispose_callback=lambda: self._dispose_registration(registration),
                version=version,
                state_schema=state_schema,
            )
            self._order += 1
            replacement_entries[(scope, key)] = _Entry(
                value=value,
                registration=registration,
                priority=priority,
                order=self._order,
            )
            registrations.append(registration)

        # One assignment is the visibility boundary for concurrent readers.
        self._entries = {
            lookup_key: entry
            for lookup_key, entry in self._entries.items()
            if entry.registration.source != source
        } | replacement_entries
        return tuple(registrations)

    def resolve_all(self, *, scope: PluginScope = "global") -> tuple[Any, ...]:
        """Resolve every visible key in deterministic priority order.

        Each logical key is resolved with the same scope-specificity rule as
        :meth:`resolve`, then independent capabilities are ordered by
        priority and registration order.  This supports additive plugin
        chains such as workflow selectors and hooks without exposing registry
        internals to an adapter.
        """

        self._validate_scope(scope)
        visible_scopes: tuple[PluginScope, ...]
        if scope == "run":
            visible_scopes = ("run", "session", "global")
        elif scope == "session":
            visible_scopes = ("session", "global")
        else:
            visible_scopes = ("global",)

        selected: dict[str, _Entry] = {}
        for candidate_scope in visible_scopes:
            for (entry_scope, key), entry in self._entries.items():
                if entry_scope == candidate_scope and key not in selected:
                    selected[key] = entry
        ordered = sorted(
            selected.values(),
            key=lambda entry: (-entry.priority, -entry.order),
        )
        return tuple(entry.value for entry in ordered)

    async def dispose_source(self, source: str) -> None:
        registrations = [
            entry.registration
            for entry in tuple(self._entries.values())
            if entry.registration.source == source
        ]
        for registration in registrations:
            await registration.dispose()

    async def dispose_all(self) -> None:
        for registration in tuple(self.registrations()):
            await registration.dispose()

    async def _dispose_registration(self, registration: Registration) -> None:
        lookup_key = (registration.scope, registration.key)
        current = self._entries.get(lookup_key)
        # A stale handle from a previous hot-reload generation must not remove
        # the current value that happens to reuse the same logical key.
        if current is not None and current.registration is registration:
            self._entries.pop(lookup_key, None)

    @staticmethod
    def _validate_scope(scope: str) -> None:
        if scope not in _SCOPES:
            raise ValueError(f"unsupported registry scope: {scope}")
