"""Reference Context Engine used for tests and local plugin development."""

from __future__ import annotations

import hashlib
import json

from .api import ContextBuildRequest, ContextBuildResult, ContextItem, ContextManifest


class ContextProviderAdapter:
    """Adapt a minimal plugin exposing ``provide(request)`` to ContextEngine."""

    def __init__(self, provider) -> None:
        self._provider = provider

    async def assemble(self, request: ContextBuildRequest) -> ContextBuildResult:
        value = self._provider.provide(request)
        if hasattr(value, "__await__"):
            value = await value
        if isinstance(value, ContextBuildResult):
            return value
        items = tuple(value or ())
        if not all(isinstance(item, ContextItem) for item in items):
            raise TypeError("context provider must return ContextItem values")
        return ContextBuildResult(
            items=items,
            estimated_tokens=sum(item.estimated_tokens for item in items),
        )

    async def restore(self, *, resource_id: str, content_version: str | None = None):
        restore = getattr(self._provider, "restore", None)
        if not callable(restore):
            return None
        value = restore(resource_id=resource_id, content_version=content_version)
        return await value if hasattr(value, "__await__") else value


class InMemoryContextEngine:
    """Deterministic priority compaction with resource-based restoration."""

    def __init__(self) -> None:
        self._resources: dict[tuple[str, str | None], ContextItem] = {}

    async def assemble(self, request: ContextBuildRequest) -> ContextBuildResult:
        estimated_before = sum(item.estimated_tokens for item in request.items)
        for item in request.items:
            if item.resource_id is not None:
                self._resources[(item.resource_id, item.content_version)] = item

        total = 0
        selected_ids: set[str] = set()
        # Pinned content is an invariant: it survives compaction even if a
        # caller supplied a budget smaller than the pinned material.
        for item in request.items:
            if item.pinned:
                selected_ids.add(item.item_id)
                total += item.estimated_tokens

        candidates = sorted(
            (
                (index, item)
                for index, item in enumerate(request.items)
                if not item.pinned
            ),
            key=lambda pair: (-pair[1].priority, -pair[0]),
        )
        for _, item in candidates:
            if total + item.estimated_tokens > request.token_budget:
                continue
            selected_ids.add(item.item_id)
            total += item.estimated_tokens

        selected = tuple(item for item in request.items if item.item_id in selected_ids)
        removed = tuple(item.item_id for item in request.items if item.item_id not in selected_ids)
        digest = hashlib.sha256(
            json.dumps(
                [
                    {
                        "item_id": item.item_id,
                        "kind": item.kind,
                        "content": item.content,
                        "metadata": item.metadata,
                    }
                    for item in selected
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        return ContextBuildResult(
            items=selected,
            estimated_tokens=total,
            compacted=bool(removed),
            removed_item_ids=removed,
            metadata={"estimated_before_tokens": estimated_before},
            manifest=ContextManifest(
                provider="context.in_memory",
                version="1",
                sources=tuple(
                    item.resource_id or item.item_id for item in selected
                ),
                content_hash=digest,
                pinned=tuple(item.item_id for item in selected if item.pinned),
            ),
        )

    async def restore(
        self,
        *,
        resource_id: str,
        content_version: str | None = None,
    ) -> ContextItem | None:
        exact = self._resources.get((resource_id, content_version))
        if exact is not None:
            return exact
        if content_version is None:
            candidates = [
                item
                for (stored_resource_id, _), item in self._resources.items()
                if stored_resource_id == resource_id
            ]
            return candidates[-1] if candidates else None
        return None
