"""Expert-scoped Tool contributions for the plugin-composed runtime."""

from __future__ import annotations

from typing import Any

from box_agent.context.experts import ExpertSessionContext

from .skill_tool import GetSkillTool


class ExpertSkillToolContributor:
    """Authorize only the disabled/recommended Skills named by one expert."""

    def __init__(self, skill_loader: Any) -> None:
        self._skill_loader = skill_loader

    def provide_tools(self, request: Any) -> tuple[Any, ...]:
        metadata = getattr(request, "metadata", {})
        expert = ExpertSessionContext.from_meta(metadata)
        if expert is None:
            return ()
        requested = frozenset(expert.skill_names())
        if not requested:
            return ()

        scoped_loader = self._skill_loader.with_expert_skill_sources(list(requested))
        enabled = frozenset(self._skill_loader.list_skills())
        available = frozenset(scoped_loader.list_skills(include_disabled=True))
        blocked = available - enabled - requested
        return (
            GetSkillTool(
                scoped_loader,
                include_disabled=True,
                blocked_skill_names=blocked,
                explicitly_allowed_skill_names=set(requested),
            ),
        )


__all__ = ["ExpertSkillToolContributor"]
