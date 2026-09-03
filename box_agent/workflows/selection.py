"""Plugin-owned prompt-to-workflow selection.

Selectors classify host-neutral prompt data.  They do not start a Run, touch
ACP, or own workflow state; their only output is the data-only
``workflow_id``/``workflow_options`` envelope consumed by every host adapter.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from ..config import ToolLimitsConfig
from ..tools.skill_loader import SkillLoader
from .external_skill import (
    EXTERNAL_SKILL_WORKFLOW_KIND,
    build_external_skill_completion_gate,
    external_skill_workflow_selection,
)
from .completion_gate import completion_gate_to_payload
from .completion_intent import cancels_pending_completion_gate
from .presentation_contract import (
    IMAGE_GENERATION_POLICY_OPTION,
    WORKFLOW_KIND as CONTROLLED_PRESENTATION_WORKFLOW_KIND,
    image_generation_policy_update,
)
from .presentation_provider import (
    parse_host_presentation_config,
    resolve_presentation_skill_provider,
)
from .presentation_routing import build_presentation_completion_gate


class WorkflowSelector(Protocol):
    """SPI for selecting one registered workflow from prompt data."""

    def select(
        self,
        prompt: str | Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        ...


def prompt_text(prompt: str | Sequence[Mapping[str, Any]]) -> str:
    """Normalize transport content without depending on an ACP type."""

    if isinstance(prompt, str):
        return prompt
    return " ".join(
        str(block.get("text", ""))
        for block in prompt
        if isinstance(block, Mapping)
    )


def _explicit_workflow_id(metadata: Mapping[str, Any]) -> str | None:
    candidates = [metadata]
    nested = metadata.get("options")
    if isinstance(nested, Mapping):
        candidates.append(nested)
    for candidate in candidates:
        for key in ("workflow_id", "workflowId", "workflow"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _normalize_selection(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    workflow_id = value.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return None
    options = value.get("workflow_options", {})
    if not isinstance(options, Mapping):
        return None
    normalized = {
        "workflow_id": workflow_id.strip(),
        "workflow_options": {str(key): item for key, item in options.items()},
    }
    # Durable Run options are a protocol envelope, never live plugin objects.
    json.dumps(normalized, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return normalized


@dataclass(frozen=True, slots=True)
class WorkflowSelectorChain:
    """Evaluate independently registered selectors in host-defined order."""

    selectors: tuple[WorkflowSelector, ...]

    def __call__(
        self,
        prompt: str | Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if _explicit_workflow_id(metadata) is not None:
            return None
        for selector in self.selectors:
            select = getattr(selector, "select", None)
            if not callable(select):
                continue
            try:
                selection = _normalize_selection(select(prompt, metadata))
            except Exception:
                # One optional extension cannot suppress the remaining
                # deterministic selector chain or invalidate a host prompt.
                continue
            if selection is not None:
                return selection
        return None


@dataclass(frozen=True, slots=True)
class ExternalSkillWorkflowSelector:
    """Select an installed Skill explicitly invoked with a slash command."""

    skill_loader: SkillLoader | None

    def select(
        self,
        prompt: str | Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        if _explicit_workflow_id(metadata) is not None:
            return None
        return external_skill_workflow_selection(
            self.skill_loader,
            prompt_text(prompt),
        )


@dataclass(frozen=True, slots=True)
class PresentationWorkflowSelector:
    """Select controlled or provider-backed presentation execution."""

    workspace_dir: str | Path
    skill_loader: SkillLoader | None = None
    tool_limits: ToolLimitsConfig | None = None

    def select(
        self,
        prompt: str | Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        if _explicit_workflow_id(metadata) is not None:
            return None
        text = prompt_text(prompt).strip()
        if not text or cancels_pending_completion_gate(text):
            return None
        host_config = parse_host_presentation_config(metadata)
        profile = metadata.get("execution_profile", "standard")
        if profile not in {"fast", "standard", "deep"}:
            profile = "standard"
        gate = build_presentation_completion_gate(
            text,
            self.workspace_dir,
            confirmed_presentation=host_config is not None,
            tool_limits=self.tool_limits,
            execution_profile=profile,
        )
        if gate is None:
            return None

        matched_names: tuple[str, ...] = ()
        loader = self.skill_loader
        if loader is not None:
            try:
                matched_names = tuple(
                    skill.name for skill in loader.filter_by_query(text)
                )
            except Exception:
                matched_names = ()
        provider = (
            resolve_presentation_skill_provider(
                loader,
                matched_names,
                preferred_skill=(
                    host_config.preferred_skill if host_config is not None else None
                ),
                query=text,
            )
            if loader is not None
            else None
        )
        if provider is not None and not provider.uses_controlled_workflow:
            skill = loader.get_skill(provider.skill_name) if loader is not None else None
            if skill is None:
                return None
            external_gate = build_external_skill_completion_gate(
                user_text=text,
                workspace_dir=self.workspace_dir,
                skill=skill,
                tool_limits=self.tool_limits,
            )
            return {
                "workflow_id": EXTERNAL_SKILL_WORKFLOW_KIND,
                "workflow_options": dict(external_gate.workflow_options),
            }

        options = dict(gate.workflow_options)
        options["presentation_skill_name"] = (
            provider.skill_name if provider is not None else "pptx"
        )
        image_policy = image_generation_policy_update(text)
        if image_policy is not None:
            options[IMAGE_GENERATION_POLICY_OPTION] = image_policy
        return {
            "workflow_id": CONTROLLED_PRESENTATION_WORKFLOW_KIND,
            "workflow_options": options,
        }


@dataclass(frozen=True, slots=True)
class CompletionGateWorkflowSelector:
    """Select evidence-backed execution for generic deliverable requests."""

    workspace_dir: str | Path
    gate_builder: Callable[..., Any]
    tool_limits: ToolLimitsConfig | None = None

    def select(
        self,
        prompt: str | Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        if _explicit_workflow_id(metadata) is not None:
            return None
        text = prompt_text(prompt).strip()
        if not text or cancels_pending_completion_gate(text):
            return None
        profile = metadata.get("execution_profile", "standard")
        if profile not in {"fast", "standard", "deep"}:
            profile = "standard"
        gate = self.gate_builder(
            text,
            self.workspace_dir,
            allow_controlled_presentation=False,
            tool_limits=self.tool_limits,
            execution_profile=profile,
        )
        if gate is None:
            return None
        return {
            "workflow_id": "completion_gate",
            "workflow_options": {
                "completion_gate": completion_gate_to_payload(gate)
            },
        }


def workflow_selector_from_registry(host: Any) -> WorkflowSelectorChain:
    """Compose the selector SPI from the Plugin Host registration order."""

    registry = getattr(host, "registries", {}).get("workflow.selectors")
    selectors = registry.resolve_all(scope="run") if registry is not None else ()
    return WorkflowSelectorChain(tuple(selectors))


__all__ = [
    "CompletionGateWorkflowSelector",
    "ExternalSkillWorkflowSelector",
    "PresentationWorkflowSelector",
    "WorkflowSelector",
    "WorkflowSelectorChain",
    "prompt_text",
    "workflow_selector_from_registry",
]
