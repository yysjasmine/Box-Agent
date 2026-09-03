"""Explicit host-selected session modes implemented as Context plugins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .api import ContextBuildRequest, ContextItem
from .project import build_project_startup_context_prompt


class SessionModeContextContributor:
    """Contribute deterministic mode policy; never infer a mode with an LLM."""

    def __init__(
        self,
        mode_prompts: Mapping[str, str],
        *,
        sandbox_prompt_builder: Callable[[bool], str] | None = None,
        file_delivery_prompt_builder: Callable[[bool], str] | None = None,
    ) -> None:
        self._mode_prompts = {
            str(mode).strip(): str(prompt).strip()
            for mode, prompt in mode_prompts.items()
            if str(mode).strip() and str(prompt).strip()
        }
        self._sandbox_prompt_builder = sandbox_prompt_builder
        self._file_delivery_prompt_builder = file_delivery_prompt_builder

    def provide(self, request: ContextBuildRequest) -> tuple[ContextItem, ...]:
        metadata: Mapping[str, Any] = request.metadata
        mode = str(metadata.get("session_mode") or "").strip()
        workspace_value = metadata.get("workspace_dir")
        artifact_mode = str(metadata.get("artifact_mode") or "output").strip().lower()
        items: list[ContextItem] = []
        use_output_dir = artifact_mode != "project"
        workspace_sections = [
            builder(use_output_dir).strip()
            for builder in (
                self._sandbox_prompt_builder,
                self._file_delivery_prompt_builder,
            )
            if builder is not None
        ]
        workspace_content = "\n\n".join(
            section for section in workspace_sections if section
        )
        if workspace_content:
            items.append(
                self._item(
                    request,
                    "workspace-execution",
                    workspace_content,
                    930,
                )
            )
        if artifact_mode == "project":
            items.append(self._item(
                request, "project-mode",
                "## Project Workspace Mode\n"
                "- This session edits an existing project workspace.\n"
                "- Do not create or use an `output/` folder unless the user explicitly asks.\n"
                "- Project files, tests, and build results are the deliverable.",
                920,
            ))
        prompt = self._mode_prompts.get(mode)
        if prompt:
            items.append(self._item(request, f"mode:{mode}", prompt, 900))
        if mode == "code_agent" and isinstance(workspace_value, str) and workspace_value.strip():
            items.append(self._item(
                request, "project-startup",
                build_project_startup_context_prompt(Path(workspace_value)), 850,
            ))
        return tuple(items)

    @staticmethod
    def _item(
        request: ContextBuildRequest, suffix: str, content: str, priority: int,
    ) -> ContextItem:
        scope = request.session_id or request.run_id or "run"
        return ContextItem(
            item_id=f"{scope}:context:{suffix}", kind="system", content=content,
            priority=priority, pinned=True,
            metadata={"role": "system", "contributor": "session_mode"},
        )


__all__ = ["SessionModeContextContributor"]
