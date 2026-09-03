"""Durable recovery for an interrupted controlled-presentation workflow."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

from ..config import ToolLimitsConfig
from .guards import CompletionGate
from .presentation_checkpoint import (
    CONTROLLED_PRESENTATION_CHECKPOINT_MARKER,
    completion_gate_progress_text,
)
from .presentation_contract import RESEARCH_MODE_OPTION
from .presentation_routing import build_presentation_completion_gate


_log = logging.getLogger(__name__)


def _read_json_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _is_controlled_deck(path: Path) -> bool:
    value = _read_json_object(path)
    if value is None or value.get("schema_version") != 1:
        return False
    if not isinstance(value.get("theme_id"), str) or not value["theme_id"].strip():
        return False
    slides = value.get("slides")
    return isinstance(slides, list) and bool(slides) and all(
        isinstance(slide, dict)
        and isinstance(slide.get("id"), str)
        and isinstance(slide.get("layout_id"), str)
        and isinstance(slide.get("props"), dict)
        for slide in slides
    )


def _is_controlled_outline(path: Path) -> bool:
    value = _read_json_object(path)
    if value is None:
        return False
    slides = value.get("slides")
    return isinstance(slides, list) and bool(slides) and all(
        isinstance(slide, dict)
        and isinstance(slide.get("title"), str)
        and isinstance(slide.get("message"), str)
        and isinstance(slide.get("bullets"), list)
        for slide in slides
    )


def recover_presentation_completion_gate(
    workspace_dir: str | Path,
    tool_limits: ToolLimitsConfig | None = None,
) -> CompletionGate | None:
    """Recover an incomplete presentation gate from canonical artifacts."""
    workspace = Path(workspace_dir)
    output = workspace / "output"
    if not output.is_dir():
        return None

    research_root = output / "research"
    has_research = research_root.is_dir() and any(
        path.is_file() for path in research_root.rglob("*")
    )
    has_checkpoint = (
        _is_controlled_deck(output / "deck.json")
        or _is_controlled_outline(output / "outline.json")
    )
    if not has_checkpoint:
        foreign_deck = next(output.rglob("deck.json"), None)
        if foreign_deck is not None:
            _log.info(
                "workflow/recovery_rejected reason=foreign_deck_schema path=%s",
                foreign_deck,
            )
        return None

    gate = build_presentation_completion_gate(
        "继续制作 PPT",
        workspace,
        tool_limits=tool_limits,
    )
    if gate is None:
        return None
    gate = replace(
        gate,
        workflow_options={
            **gate.workflow_options,
            RESEARCH_MODE_OPTION: "deep" if has_research else "auto",
        },
    )
    checkpoint = completion_gate_progress_text(gate, str(workspace))
    if checkpoint and (
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}complete" in checkpoint
    ):
        return None
    return gate


__all__ = ["recover_presentation_completion_gate"]
