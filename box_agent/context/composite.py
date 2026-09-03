"""Composition helpers for independently registered context capabilities."""

from __future__ import annotations

import inspect
from typing import Any

from .api import ContextBuildRequest, ContextBuildResult, ContextItem, HostProjection
from .in_memory import ContextProviderAdapter


class CompositeContextEngine:
    """Run additive contributors, a provider, and a compactor behind one port."""

    def __init__(
        self,
        *,
        provider: Any | None = None,
        contributors: tuple[Any, ...] = (),
        compactor: Any | None = None,
    ) -> None:
        self._provider = provider
        self._contributors = tuple(contributors)
        self._compactor = compactor

    async def assemble(self, request: ContextBuildRequest) -> ContextBuildResult:
        items = list(request.items)
        item_ids = {item.item_id for item in items}
        contributor_projections: list[HostProjection] = []
        contributor_metadata: list[dict[str, Any]] = []
        for contributor in self._contributors:
            provide = getattr(contributor, "provide", None)
            if not callable(provide):
                raise TypeError("context contributor must provide provide(request)")
            value = provide(request)
            value = await value if inspect.isawaitable(value) else value
            if isinstance(value, ContextBuildResult):
                contributed = value.items
                metadata = dict(value.metadata)
                contributor_projections.extend(value.host_projections)
                if metadata:
                    contributor_metadata.append(metadata)
            else:
                contributed = value
            if not isinstance(contributed, (list, tuple)) or not all(
                isinstance(item, ContextItem) for item in contributed
            ):
                raise TypeError("context contributor must return ContextItem values")
            for item in contributed:
                if item.item_id in item_ids:
                    raise ValueError(
                        f"context contributor emitted duplicate item_id: {item.item_id}"
                    )
                item_ids.add(item.item_id)
                items.append(item)
        enriched_request = ContextBuildRequest(
            items=tuple(items),
            token_budget=request.token_budget,
            session_id=request.session_id,
            run_id=request.run_id,
            metadata=dict(request.metadata),
        )
        if self._provider is None:
            result = ContextBuildResult(
                items=enriched_request.items,
                estimated_tokens=sum(
                    item.estimated_tokens for item in enriched_request.items
                ),
            )
        else:
            assemble = getattr(self._provider, "assemble", None)
            provide = getattr(self._provider, "provide", None)
            if callable(assemble):
                value = assemble(enriched_request)
            elif callable(provide):
                value = ContextProviderAdapter(self._provider).assemble(
                    enriched_request
                )
            else:
                raise TypeError("context provider must provide assemble(request) or provide(request)")
            result = await value if inspect.isawaitable(value) else value
            if not isinstance(result, ContextBuildResult):
                raise TypeError("context provider must return ContextBuildResult")

        result_metadata = dict(result.metadata)
        merged_projections = [*contributor_projections, *result.host_projections]
        if contributor_metadata:
            result_metadata["contributors"] = contributor_metadata
        result = ContextBuildResult(
            items=result.items,
            estimated_tokens=result.estimated_tokens,
            compacted=result.compacted,
            removed_item_ids=result.removed_item_ids,
            metadata=result_metadata,
            manifest=result.manifest,
            host_projections=tuple(merged_projections),
        )

        if self._compactor is None:
            return result
        compact = getattr(self._compactor, "compact", None)
        if not callable(compact):
            raise TypeError("context compactor must provide compact(request)")
        compact_request = ContextBuildRequest(
            items=result.items,
            token_budget=request.token_budget,
            session_id=request.session_id,
            run_id=request.run_id,
            metadata=dict(request.metadata),
        )
        value = compact(compact_request)
        value = await value if inspect.isawaitable(value) else value
        if isinstance(value, ContextBuildResult):
            return ContextBuildResult(
                items=value.items,
                estimated_tokens=value.estimated_tokens,
                compacted=value.compacted,
                removed_item_ids=value.removed_item_ids,
                metadata={**result.metadata, **value.metadata},
                manifest=value.manifest,
                host_projections=(
                    *result.host_projections,
                    *value.host_projections,
                ),
            )
        if isinstance(value, (list, tuple)) and all(isinstance(item, ContextItem) for item in value):
            return ContextBuildResult(
                items=tuple(value),
                estimated_tokens=sum(item.estimated_tokens for item in value),
                compacted=True,
                removed_item_ids=tuple(
                    item.item_id for item in result.items if item not in value
                ),
                metadata=dict(result.metadata),
                host_projections=result.host_projections,
            )
        raise TypeError("context compactor must return ContextBuildResult or ContextItem values")

    async def restore(self, *, resource_id: str, content_version: str | None = None) -> Any:
        restore = getattr(self._provider, "restore", None)
        if not callable(restore):
            return None
        value = restore(resource_id=resource_id, content_version=content_version)
        return await value if inspect.isawaitable(value) else value


__all__ = ["CompositeContextEngine"]
