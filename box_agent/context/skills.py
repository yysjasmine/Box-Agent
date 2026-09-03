"""Skill catalog contribution for the pluggable Context Engine."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from .api import ContextBuildRequest, ContextItem


class SkillCatalogContextContributor:
    """Contribute query-filtered Skill metadata without loading instructions.

    Full Skill instructions remain owned by the selected workflow or the
    ``get_skill`` tool.  This contributor restores the progressive-disclosure
    catalog for every host that uses the Kernel Context Engine.
    """

    def __init__(self, skill_loader: Any) -> None:
        self._skill_loader = skill_loader

    async def provide(self, request: ContextBuildRequest) -> tuple[ContextItem, ...]:
        query_parts: list[str] = []
        for item in request.items:
            role = item.metadata.get("role")
            if item.kind != "user" and role != "user":
                continue
            if isinstance(item.content, str) and item.content.strip():
                query_parts.append(item.content.strip())
        query = " ".join(query_parts)
        render = getattr(self._skill_loader, "get_skills_metadata_prompt", None)
        if not callable(render):
            return ()
        content = render(query=query)
        if not isinstance(content, str) or not content.strip():
            return ()
        digest = sha256(content.encode("utf-8")).hexdigest()
        matched_names: tuple[str, ...] = ()
        filter_by_query = getattr(self._skill_loader, "filter_by_query", None)
        if callable(filter_by_query):
            try:
                matched_names = tuple(
                    str(skill.name) for skill in filter_by_query(query)
                )
            except Exception:
                matched_names = ()
        return (
            ContextItem(
                item_id="skills:catalog",
                kind="skill_catalog",
                content=content,
                priority=850,
                pinned=True,
                resource_id="box-agent://skills/catalog",
                content_version=digest,
                metadata={
                    "role": "system",
                    "matched_skill_names": list(matched_names),
                    "content_hash": digest,
                    "progressive_disclosure": True,
                },
            ),
        )


__all__ = ["SkillCatalogContextContributor"]
