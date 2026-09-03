"""Composition entry point for automatic deliverable completion gates."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Final

from ..config import ToolLimitsConfig
from ..persistence.artifacts import OUTPUT_SUBDIR
from .delivery import (
    has_deliverable_intent,
    is_meta_prompt_rewrite_request,
    strip_negated_format_clauses,
)
from .execution_profile import ExecutionProfile
from .guards import CompletionGate, artifact_signatures_for_globs
from .turn_policy import text_is_short_acknowledgement
from .completion_intent import cancels_pending_completion_gate
from .presentation_contract import (
    IMAGE_GENERATION_AUTO,
    IMAGE_GENERATION_EXPLICIT_RETRY,
    IMAGE_GENERATION_POLICY_OPTION,
    WORKFLOW_KIND,
    image_generation_policy_update,
)
from .presentation_routing import (
    build_presentation_completion_gate,
    has_explicit_external_research_action,
)


_PENDING_GATE_CONTINUE_PHRASES: Final[tuple[str, ...]] = (
    "继续",
    "确认继续",
    "确认制作",
    "接着",
    "补完",
    "完成",
    "输出html",
    "生成html",
    "渲染html",
    "continue",
    "resume",
    "finish",
    "go on",
    "output html",
    "render html",
)

_GENERIC_COMPLETION_GATE_PATTERNS: Final[
    tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]
] = (
    (
        ("docx", "word", "简历", "文档"),
        (f"{OUTPUT_SUBDIR}/**/*.docx",),
    ),
    (
        ("xlsx", "excel", "spreadsheet", "表格"),
        (f"{OUTPUT_SUBDIR}/**/*.xlsx", f"{OUTPUT_SUBDIR}/**/*.xls"),
    ),
    (
        ("html", "网页", "报告", "md格式", "markdown"),
        (
            f"{OUTPUT_SUBDIR}/**/*.html",
            f"{OUTPUT_SUBDIR}/**/*.htm",
            f"{OUTPUT_SUBDIR}/**/*.md",
            f"{OUTPUT_SUBDIR}/**/*.pdf",
        ),
    ),
)

_IMAGE_ARTIFACT_GLOBS: Final[tuple[str, ...]] = (
    f"{OUTPUT_SUBDIR}/**/*.png",
    f"{OUTPUT_SUBDIR}/**/*.jpg",
    f"{OUTPUT_SUBDIR}/**/*.jpeg",
    f"{OUTPUT_SUBDIR}/**/*.webp",
)

_NATIVE_IMAGE_ACTION_KEYWORDS: Final[tuple[str, ...]] = (
    "生图",
    "生成一张",
    "画一张",
    "绘制一张",
    "生成图片",
    "生成图像",
    "generate an image",
    "create an image",
    "draw an image",
    "image generation",
    "text-to-image",
)

_NATIVE_IMAGE_OUTPUT_KEYWORDS: Final[tuple[str, ...]] = (
    "图片",
    "图像",
    "插画",
    "海报",
    "封面",
    "信息图",
    "全景图",
    "png",
    "jpg",
    "jpeg",
    "webp",
    "image",
    "illustration",
    "poster",
    "infographic",
)

_NON_NATIVE_IMAGE_DELIVERY_KEYWORDS: Final[tuple[str, ...]] = (
    "html",
    "svg",
    "网页",
    "web page",
    "截图",
    "screenshot",
    "ppt",
    "pptx",
    "powerpoint",
    "幻灯片",
    "演示文稿",
    "报告",
)

_HOST_EXECUTION_CONTRACT_RE: Final[re.Pattern[str]] = re.compile(
    r"<host_execution_contract\b(?P<attributes>[^>]*)>",
    re.IGNORECASE,
)
_HOST_ACCEPTANCE_CRITERIA_COUNT_RE: Final[re.Pattern[str]] = re.compile(
    r"""\bacceptance_criteria_count\s*=\s*["'](?P<count>\d+)["']""",
    re.IGNORECASE,
)


def _host_execution_contract(user_text: str) -> tuple[bool, int | None]:
    matches = list(_HOST_EXECUTION_CONTRACT_RE.finditer(user_text))
    if not matches:
        return False, None
    match = matches[-1]
    count_match = _HOST_ACCEPTANCE_CRITERIA_COUNT_RE.search(
        match.group("attributes")
    )
    if count_match is None:
        return True, None
    count = int(count_match.group("count"))
    return True, count if 1 <= count <= 50 else None


def _is_native_image_generation_request(
    text: str,
    positive_format_text: str,
) -> bool:
    has_action = any(keyword in text for keyword in _NATIVE_IMAGE_ACTION_KEYWORDS)
    has_image_output = any(
        keyword in text for keyword in _NATIVE_IMAGE_OUTPUT_KEYWORDS
    )
    has_non_native_delivery = any(
        keyword in positive_format_text
        for keyword in _NON_NATIVE_IMAGE_DELIVERY_KEYWORDS
    )
    return has_action and has_image_output and not has_non_native_delivery


def should_resume_pending_completion_gate(
    user_text: str,
    *,
    waiting_for_user_input: bool,
) -> bool:
    """Recognize an answer or terse continuation of a retained deliverable."""
    normalized = " ".join(user_text.casefold().split()).strip()
    if (
        not normalized
        or cancels_pending_completion_gate(normalized)
        or is_meta_prompt_rewrite_request(user_text)
    ):
        return False
    if image_generation_policy_update(user_text) is not None:
        return True
    if waiting_for_user_input:
        return True
    compact = normalized.replace(" ", "")
    if len(normalized) <= 80 and any(
        phrase in normalized or phrase.replace(" ", "") in compact
        for phrase in _PENDING_GATE_CONTINUE_PHRASES
    ):
        return True
    return text_is_short_acknowledgement(normalized)


def rebase_pending_completion_gate(
    gate: CompletionGate,
    user_text: str,
) -> CompletionGate:
    """Apply explicit latest-turn workflow constraints without dropping delivery."""
    if gate.workflow_checkpoint_kind != WORKFLOW_KIND:
        return gate
    image_policy = image_generation_policy_update(user_text)
    if image_policy is None:
        return gate
    return replace(
        gate,
        workflow_options={
            **gate.workflow_options,
            IMAGE_GENERATION_POLICY_OPTION: image_policy,
        },
    )


def pending_completion_gate_for_storage(
    gate: CompletionGate,
) -> CompletionGate:
    """Remove one-turn workflow overrides before retaining a pending gate."""
    if (
        gate.workflow_checkpoint_kind == WORKFLOW_KIND
        and gate.workflow_options.get(IMAGE_GENERATION_POLICY_OPTION)
        == IMAGE_GENERATION_EXPLICIT_RETRY
    ):
        return replace(
            gate,
            workflow_options={
                **gate.workflow_options,
                IMAGE_GENERATION_POLICY_OPTION: IMAGE_GENERATION_AUTO,
            },
        )
    return gate


def completion_gate_has_workflow_lifecycle(gate: CompletionGate) -> bool:
    """Return whether a workflow owns cross-turn state for this gate."""
    return gate.workflow_checkpoint_kind is not None


def build_auto_completion_gate(
    user_text: str,
    workspace_dir: str | Path,
    *,
    confirmed_presentation: bool = False,
    allow_controlled_presentation: bool = True,
    tool_limits: ToolLimitsConfig | None = None,
    execution_profile: ExecutionProfile = "standard",
) -> CompletionGate | None:
    """Create an evidence-backed gate for a recognized deliverable request."""
    requires_host_receipt, execution_result_criteria_count = (
        _host_execution_contract(user_text)
    )
    if not confirmed_presentation and is_meta_prompt_rewrite_request(user_text):
        if not requires_host_receipt:
            return None
        return CompletionGate(
            required_tools=frozenset({"report_execution_result"}),
            execution_result_criteria_count=execution_result_criteria_count,
            max_continuations=3,
            deadline_seconds=900.0,
        )
    if (
        not confirmed_presentation
        and not has_deliverable_intent(user_text)
        and not requires_host_receipt
    ):
        return None

    presentation_gate = (
        build_presentation_completion_gate(
            user_text,
            workspace_dir,
            confirmed_presentation=confirmed_presentation,
            tool_limits=tool_limits,
            execution_profile=execution_profile,
        )
        if allow_controlled_presentation
        else None
    )
    if presentation_gate is not None:
        if not requires_host_receipt:
            return presentation_gate
        return replace(
            presentation_gate,
            required_tools=(
                presentation_gate.required_tools
                | frozenset({"report_execution_result"})
            ),
            execution_result_criteria_count=execution_result_criteria_count,
        )

    text = user_text.strip().lower()
    positive_format_text = strip_negated_format_clauses(text)
    native_image_generation = _is_native_image_generation_request(
        text,
        positive_format_text,
    )
    patterns: list[str] = (
        list(_IMAGE_ARTIFACT_GLOBS) if native_image_generation else []
    )
    if not native_image_generation:
        for keywords, artifact_patterns in _GENERIC_COMPLETION_GATE_PATTERNS:
            if any(keyword in positive_format_text for keyword in keywords):
                patterns.extend(artifact_patterns)
    required_tools = (
        frozenset({"report_execution_result"})
        if requires_host_receipt
        else frozenset()
    )
    if not patterns:
        if not required_tools:
            return None
        return CompletionGate(
            required_tools=required_tools,
            execution_result_criteria_count=execution_result_criteria_count,
            max_continuations=3,
            deadline_seconds=900.0,
        )

    deduped_patterns = tuple(dict.fromkeys(patterns))
    workspace = str(workspace_dir)
    return CompletionGate(
        required_tools=(
            (
                frozenset({"generate_image"})
                if native_image_generation
                else frozenset()
            )
            | required_tools
        ),
        execution_result_criteria_count=execution_result_criteria_count,
        restrict_tools_until_required_succeed=native_image_generation,
        required_changed_artifact_globs=deduped_patterns,
        baseline_artifact_signatures=artifact_signatures_for_globs(
            deduped_patterns,
            workspace,
        ),
        max_continuations=3,
        deadline_seconds=900.0,
    )
