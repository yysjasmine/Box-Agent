"""Generic lifecycle policy for explicitly invoked third-party Skills.

The Skill remains an opaque instruction package.  Box-Agent only owns the
host-side lifecycle: explicit selection, bounded execution, artifact handoff,
and durable pause/resume metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from collections.abc import Mapping
from typing import Any, ClassVar

from ..persistence.artifacts import artifact_scan_root
from ..config import ToolLimitsConfig
from ..api import Message, WorkflowContinuation
from ..context import ContextItem
from .guards import (
    CompletionGate,
    artifact_signatures_for_globs,
    completion_gate_gaps,
    completion_gate_text,
    completion_gate_tool_satisfies_requirements,
)
from ..tools.base import ToolResult
from ..tools.permissions import extract_absolute_paths
from ..tools.skill_loader import Skill, SkillLoader
from ..persistence.workflow_checkpoint_store import (
    WorkflowPauseCheckpoint,
    checkpoint_resume_instruction,
)
from .contract import WorkflowAction, WorkflowCheckpointUpdate
from .contract import is_natural_end_reason
from .state import workflow_state_from_recovery


EXTERNAL_SKILL_WORKFLOW_KIND = "external_skill"
EXTERNAL_SKILL_CHECKPOINT_MARKER = "[BOX_AGENT_EXTERNAL_SKILL_CHECKPOINT]"
SKILL_NAME_OPTION = "skill_name"
SKILL_SOURCE_OPTION = "skill_source"
SKILL_ROOT_OPTION = "skill_root"
TASK_TEXT_OPTION = "task_text"
ARTIFACT_GLOBS_OPTION = "artifact_globs"
OBSERVED_PATHS_OPTION = "observed_paths"
LAST_FAILURES_OPTION = "last_failures"

_MAX_TASK_TEXT_CHARS = 4_000
_MAX_OBSERVED_PATHS = 12
_MAX_FAILURES = 3
_MAX_FAILURE_CHARS = 800
_MAX_BASELINE_ARTIFACTS = 256
_DEFAULT_EXTERNAL_SKILL_LIMITS = ToolLimitsConfig().external_skill
_DEFAULT_MAX_TOOL_CALLS = _DEFAULT_EXTERNAL_SKILL_LIMITS.max_tool_calls
_COMPLETION_RESERVE_TOOL_CALLS = (
    _DEFAULT_EXTERNAL_SKILL_LIMITS.completion_reserve_calls
)
_EXPLICIT_SKILL_RE = re.compile(
    r"(?<![\w./:-])/(?P<name>[a-z0-9][a-z0-9._-]{0,127})"
    r"(?=$|[\s,，.。!！?？;；:：)）])",
    re.IGNORECASE,
)
_AUTHORING_RE = re.compile(
    r"(?:create|generate|build|author|export|render|produce|convert|"
    r"创建|制作|生成|导出|渲染|转换)",
    re.IGNORECASE,
)
_DELIVERY_FORMATS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![a-z0-9])pptx(?![a-z0-9])", re.IGNORECASE), "pptx"),
    (re.compile(r"(?<![a-z0-9])docx(?![a-z0-9])", re.IGNORECASE), "docx"),
    (re.compile(r"(?<![a-z0-9])xlsx(?![a-z0-9])", re.IGNORECASE), "xlsx"),
    (re.compile(r"(?<![a-z0-9])pdf(?![a-z0-9])", re.IGNORECASE), "pdf"),
    (re.compile(r"(?<![a-z0-9])csv(?![a-z0-9])", re.IGNORECASE), "csv"),
    (re.compile(r"(?<![a-z0-9])html?(?![a-z0-9])", re.IGNORECASE), "html"),
    (re.compile(r"(?<![a-z0-9])mp4(?![a-z0-9])", re.IGNORECASE), "mp4"),
    (re.compile(r"(?<![a-z0-9])zip(?![a-z0-9])", re.IGNORECASE), "zip"),
)


def explicit_skill_invocation_name(user_text: str) -> str | None:
    """Return the first standalone slash Skill token, if present."""
    match = _EXPLICIT_SKILL_RE.search(user_text)
    return match.group("name") if match is not None else None


def resolve_explicit_skill_invocation(
    skill_loader: SkillLoader | None,
    user_text: str,
) -> Skill | None:
    """Resolve only an installed, enabled Skill named by a standalone slash token."""
    if skill_loader is None:
        return None
    requested = explicit_skill_invocation_name(user_text)
    if requested is None:
        return None
    canonical = next(
        (name for name in skill_loader.list_skills() if name.casefold() == requested.casefold()),
        None,
    )
    if canonical is None:
        return None
    skill = skill_loader.get_skill(canonical)
    if skill is None or skill.broken:
        return None
    return skill


def external_skill_workflow_selection(
    skill_loader: SkillLoader | None,
    user_text: str,
) -> dict[str, Any] | None:
    """Translate one installed slash Skill into a data-only workflow request."""

    skill = resolve_explicit_skill_invocation(skill_loader, user_text)
    if skill is None:
        return None
    skill_root = str(skill.skill_path.parent) if skill.skill_path is not None else None
    return {
        "workflow_id": EXTERNAL_SKILL_WORKFLOW_KIND,
        "workflow_options": external_skill_workflow_options(
            skill_name=skill.name,
            skill_source=skill.source,
            skill_root=skill_root,
            task_text=user_text,
            artifact_globs=infer_skill_delivery_globs(skill),
        ),
    }


def infer_skill_delivery_globs(skill: Skill) -> tuple[str, ...]:
    """Infer a conservative file contract from static Skill metadata only."""
    routing_text = " ".join(
        (
            skill.name,
            " ".join(skill.keywords or ()),
            skill.description,
        )
    )
    if _AUTHORING_RE.search(routing_text) is None:
        return ()
    formats: list[str] = []
    for pattern, extension in _DELIVERY_FORMATS:
        if pattern.search(routing_text) is not None and extension not in formats:
            formats.append(extension)
    if (
        not formats
        and "presentation.authoring" in (skill.capabilities or ())
    ):
        formats.append("pptx")
    return tuple(f"output/**/*.{extension}" for extension in formats)


def external_skill_workflow_options(
    *,
    skill_name: str,
    skill_source: str,
    skill_root: str | None,
    task_text: str,
    artifact_globs: tuple[str, ...],
    observed_paths: tuple[str, ...] = (),
    last_failures: tuple[str, ...] = (),
) -> dict[str, str]:
    """Return the bounded data-only contract used for durable recovery."""
    return {
        SKILL_NAME_OPTION: skill_name.strip(),
        SKILL_SOURCE_OPTION: skill_source.strip(),
        SKILL_ROOT_OPTION: (skill_root or "").strip(),
        TASK_TEXT_OPTION: task_text.strip()[:_MAX_TASK_TEXT_CHARS],
        ARTIFACT_GLOBS_OPTION: json.dumps(artifact_globs, ensure_ascii=False),
        OBSERVED_PATHS_OPTION: json.dumps(observed_paths, ensure_ascii=False),
        LAST_FAILURES_OPTION: json.dumps(last_failures, ensure_ascii=False),
    }


def build_external_skill_completion_gate(
    *,
    user_text: str,
    workspace_dir: str | Path,
    skill: Skill,
    tool_limits: ToolLimitsConfig | None = None,
) -> CompletionGate:
    """Build a generic lifecycle gate for one explicit Skill invocation."""
    artifact_globs = infer_skill_delivery_globs(skill)
    skill_root = str(skill.skill_path.parent) if skill.skill_path is not None else None
    effective_limits = tool_limits or ToolLimitsConfig()
    completion_limits = effective_limits.completion
    limits = effective_limits.external_skill
    return CompletionGate(
        required_changed_artifact_globs=artifact_globs,
        baseline_artifact_signatures=artifact_signatures_for_globs(
            artifact_globs,
            str(workspace_dir),
        ),
        max_continuations=completion_limits.max_continuations,
        deadline_seconds=completion_limits.deadline_seconds,
        max_tool_calls=limits.max_tool_calls,
        max_delegated_tool_calls=limits.max_delegated_tool_calls,
        completion_reserve_tool_calls=(
            limits.completion_reserve_calls if artifact_globs else 0
        ),
        pause_tools=frozenset({"request_user_input", "request_user_decision"}),
        workflow_checkpoint_kind=EXTERNAL_SKILL_WORKFLOW_KIND,
        workflow_options=external_skill_workflow_options(
            skill_name=skill.name,
            skill_source=skill.source,
            skill_root=skill_root,
            task_text=user_text,
            artifact_globs=artifact_globs,
        ),
    )


def build_external_skill_completion_gate_from_options(
    *,
    workspace_dir: str | Path,
    workflow_options: Mapping[str, Any],
    tool_limits: ToolLimitsConfig | None = None,
) -> CompletionGate:
    """Rebuild a generic gate from validated, data-only checkpoint options."""
    artifact_globs = _decode_string_tuple(
        _option_text(workflow_options, ARTIFACT_GLOBS_OPTION),
        limit=8,
    )
    options = external_skill_workflow_options(
        skill_name=_option_text(workflow_options, SKILL_NAME_OPTION) or "unknown",
        skill_source=_option_text(workflow_options, SKILL_SOURCE_OPTION) or "unknown",
        skill_root=_option_text(workflow_options, SKILL_ROOT_OPTION),
        task_text=_option_text(workflow_options, TASK_TEXT_OPTION) or "",
        artifact_globs=artifact_globs,
        observed_paths=_decode_string_tuple(
            _option_text(workflow_options, OBSERVED_PATHS_OPTION),
            limit=_MAX_OBSERVED_PATHS,
        ),
        last_failures=_decode_string_tuple(
            _option_text(workflow_options, LAST_FAILURES_OPTION),
            limit=_MAX_FAILURES,
        ),
    )
    effective_limits = tool_limits or ToolLimitsConfig()
    completion_limits = effective_limits.completion
    limits = effective_limits.external_skill
    return CompletionGate(
        required_changed_artifact_globs=artifact_globs,
        baseline_artifact_signatures=artifact_signatures_for_globs(
            artifact_globs,
            str(workspace_dir),
        ),
        max_continuations=completion_limits.max_continuations,
        deadline_seconds=completion_limits.deadline_seconds,
        max_tool_calls=limits.max_tool_calls,
        max_delegated_tool_calls=limits.max_delegated_tool_calls,
        completion_reserve_tool_calls=(
            limits.completion_reserve_calls if artifact_globs else 0
        ),
        pause_tools=frozenset({"request_user_input", "request_user_decision"}),
        workflow_checkpoint_kind=EXTERNAL_SKILL_WORKFLOW_KIND,
        workflow_options=options,
    )


def external_skill_policy_from_options(
    *,
    workspace_dir: str | None,
    artifact_root_dir: str | Path | None,
    workflow_options: Mapping[str, Any] | None,
) -> ExternalSkillRunPolicy:
    """Create the built-in host policy without executing third-party code."""
    options = workflow_options or {}
    return ExternalSkillRunPolicy(
        workspace_dir=workspace_dir,
        artifact_root_dir=artifact_root_dir,
        skill_name=_option_text(options, SKILL_NAME_OPTION),
        skill_source=_option_text(options, SKILL_SOURCE_OPTION),
        skill_root=_option_text(options, SKILL_ROOT_OPTION),
        task_text=_option_text(options, TASK_TEXT_OPTION),
        artifact_globs=_decode_string_tuple(
            _option_text(options, ARTIFACT_GLOBS_OPTION),
            limit=8,
        ),
        observed_paths=list(
            _decode_string_tuple(
                _option_text(options, OBSERVED_PATHS_OPTION),
                limit=_MAX_OBSERVED_PATHS,
            )
        ),
        last_failures=list(
            _decode_string_tuple(
                _option_text(options, LAST_FAILURES_OPTION),
                limit=_MAX_FAILURES,
            )
        ),
    )


def _option_text(options: Mapping[str, Any], key: str) -> str | None:
    value = options.get(key)
    return value if isinstance(value, str) and value else None


def _decode_string_tuple(raw: str | None, *, limit: int) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        values = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if isinstance(value, str))[:limit]


def _skill_content_hash(skill: Skill) -> tuple[str, str]:
    """Return the prompt and a path-independent instruction digest."""

    prompt = skill.to_prompt()
    identity = {
        "name": skill.name,
        "description": skill.description,
        "content": skill.content,
        "broken": skill.broken,
        "broken_reason": skill.broken_reason,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return prompt, digest


def _is_relative_to(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(slots=True)
class ExternalSkillRunPolicy:
    """Opaque third-party Skill execution wrapped in a trusted host lifecycle."""

    workspace_dir: str | None
    artifact_root_dir: str | Path | None
    skill_name: str | None = None
    skill_source: str | None = None
    skill_root: str | None = None
    task_text: str | None = None
    artifact_globs: tuple[str, ...] = ()
    # Optional loader supplied by the host.  The loader is a capability
    # dependency, not durable workflow state: checkpoints persist only the
    # selected skill identity/options and resolve the immutable instructions
    # again after a restart.
    observed_paths: list[str] = field(default_factory=list)
    last_failures: list[str] = field(default_factory=list)
    stage: str | None = None
    _last_checkpoint_text: str | None = None
    _resume_checkpoint: WorkflowPauseCheckpoint | None = None
    skill_loader: SkillLoader | None = field(default=None, repr=False, compare=False)
    skill_content_hash: str | None = field(default=None, repr=False, compare=False)
    # The generic artifact/continuation gate is workflow-owned state.  It is
    # initialized per Run after the selected Skill and workspace are known;
    # the frozen CompletionGate itself never crosses the public request DTO.
    _completion_gate: CompletionGate | None = field(default=None, init=False, repr=False)
    _succeeded_tools: set[str] = field(default_factory=set, init=False, repr=False)
    _continuation_count: int = field(default=0, init=False, repr=False)
    _tool_call_total: int = field(default=0, init=False, repr=False)
    _paused: bool = field(default=False, init=False, repr=False)
    _released: bool = field(default=False, init=False, repr=False)
    _baseline_artifact_signatures: dict[str, tuple[int, int]] = field(
        default_factory=dict, init=False, repr=False
    )

    kind: ClassVar[str] = EXTERNAL_SKILL_WORKFLOW_KIND
    checkpoint_injection_id: ClassVar[str] = EXTERNAL_SKILL_CHECKPOINT_MARKER
    evidence_read_batch_size: ClassVar[int] = 1

    def for_run(self, request: Any, bundle: Any | None = None) -> "ExternalSkillRunPolicy":
        """Clone the policy and restore bounded state for one native Run."""

        # Keep the host-owned loader by identity.  Besides avoiding duplicate
        # mutable catalogs, this lets a provider use a loader that owns locks
        # or file handles and therefore cannot be deep-copied.
        bound = deepcopy(
            self,
            {id(self.skill_loader): self.skill_loader}
            if self.skill_loader is not None
            else {},
        )
        # Do not duplicate a live loader (it owns source signatures and may be
        # refreshed by the host).  The run-local mutable lifecycle state above
        # is still isolated by ``deepcopy``.
        bound.skill_loader = self.skill_loader
        metadata = getattr(request, "metadata", {})
        if isinstance(metadata, Mapping) and isinstance(metadata.get("workspace_dir"), str):
            bound.workspace_dir = metadata["workspace_dir"] or bound.workspace_dir
        options = getattr(getattr(request, "options", None), "workflow_options", {})
        if isinstance(options, Mapping):
            bound.skill_name = _option_text(options, SKILL_NAME_OPTION) or bound.skill_name
            bound.skill_source = _option_text(options, SKILL_SOURCE_OPTION) or bound.skill_source
            bound.skill_root = _option_text(options, SKILL_ROOT_OPTION) or bound.skill_root
            bound.task_text = _option_text(options, TASK_TEXT_OPTION) or bound.task_text
            if not bound.artifact_globs:
                bound.artifact_globs = _decode_string_tuple(
                    _option_text(options, ARTIFACT_GLOBS_OPTION), limit=8
                )
            if not bound.observed_paths:
                bound.observed_paths.extend(
                    _decode_string_tuple(
                        _option_text(options, OBSERVED_PATHS_OPTION),
                        limit=_MAX_OBSERVED_PATHS,
                    )
                )
            if not bound.last_failures:
                bound.last_failures.extend(
                    _decode_string_tuple(
                        _option_text(options, LAST_FAILURES_OPTION),
                        limit=_MAX_FAILURES,
                    )
                )
        # A native caller may select ``external_skill`` directly and provide
        # only a slash invocation in the user message.  Resolve that identity
        # at the adapter boundary so the context engine can still receive the
        # exact Skill instructions.
        if not bound.skill_name and bound.skill_loader is not None:
            content = getattr(getattr(request, "user_input", None), "content", "")
            text = content if isinstance(content, str) else str(content)
            requested = explicit_skill_invocation_name(text)
            if requested:
                skill = resolve_explicit_skill_invocation(bound.skill_loader, text)
                if skill is not None:
                    bound.skill_name = skill.name
                    bound.skill_source = skill.source
                    bound.skill_root = (
                        str(skill.skill_path.parent) if skill.skill_path is not None else None
                    )
        state = workflow_state_from_recovery(bundle, self.kind)
        if isinstance(state, Mapping):
            bound._restore_native_state(state)
        bound._initialize_completion_gate(request)
        return bound

    def _initialize_completion_gate(self, request: Any | None = None) -> None:
        """Build one immutable evidence baseline for this Run."""

        skill = self._resolve_skill()
        workspace = self.workspace_dir or "."
        if skill is not None:
            user_input = getattr(request, "user_input", None)
            content = getattr(user_input, "content", "")
            task_text = self.task_text or (content if isinstance(content, str) else "")
            self._completion_gate = build_external_skill_completion_gate(
                user_text=task_text,
                workspace_dir=workspace,
                skill=skill,
            )
        else:
            self._completion_gate = build_external_skill_completion_gate_from_options(
                workspace_dir=workspace,
                workflow_options=self.checkpoint_options(),
            )
        if self._baseline_artifact_signatures:
            self._completion_gate = replace(
                self._completion_gate,
                baseline_artifact_signatures=dict(self._baseline_artifact_signatures),
            )

    def build_checkpoint_payload(self) -> dict[str, Any]:
        """Persist only bounded, data-only lifecycle state."""

        if self._completion_gate is None:
            self._initialize_completion_gate()
        skill = self._resolve_skill()
        if skill is not None and self.skill_content_hash is None:
            _, self.skill_content_hash = _skill_content_hash(skill)

        state = {
            "stage": self.stage,
            "options": self.checkpoint_options(),
            # Persist identity, not instruction text.  A resumed run can
            # detect that the selected Skill changed without serializing the
            # prompt into the durable checkpoint.
            "skill_content_hash": self.skill_content_hash,
            "observed_paths": list(self.observed_paths[-_MAX_OBSERVED_PATHS:]),
            "last_failures": list(self.last_failures[-_MAX_FAILURES:]),
            "succeeded_tools": sorted(self._succeeded_tools),
            "continuations": self._continuation_count,
            "tool_calls": self._tool_call_total,
            "paused": self._paused,
            "released": self._released,
            "baseline_artifact_signatures": [
                {
                    "path": path,
                    "size": signature[0],
                    "mtime_ns": signature[1],
                }
                for path, signature in list(
                    self._completion_gate.baseline_artifact_signatures.items()
                )[:_MAX_BASELINE_ARTIFACTS]
            ]
            if self._completion_gate is not None
            else [],
        }
        return {self.kind: state}

    def on_event(self, event: Any) -> None:
        """Restore a checkpoint when installed behind ``WorkflowEventHook``."""

        event_type = getattr(event, "type", None)
        payload = getattr(event, "payload", None)
        if isinstance(event, Mapping):
            event_type = event.get("type", event_type)
            payload = event.get("payload", payload)
        if event_type != "workflow.checkpoint" or not isinstance(payload, Mapping):
            return
        state = payload.get("workflow_state")
        if isinstance(state, Mapping):
            value = state.get(self.kind)
            if isinstance(value, Mapping):
                self._restore_native_state(value)

    def _restore_native_state(self, state: Mapping[str, Any]) -> None:
        options = state.get("options")
        if isinstance(options, Mapping):
            self.skill_name = self.skill_name or _option_text(options, SKILL_NAME_OPTION)
            self.skill_source = self.skill_source or _option_text(options, SKILL_SOURCE_OPTION)
            self.skill_root = self.skill_root or _option_text(options, SKILL_ROOT_OPTION)
            self.task_text = self.task_text or _option_text(options, TASK_TEXT_OPTION)
            if not self.artifact_globs:
                self.artifact_globs = _decode_string_tuple(
                    _option_text(options, ARTIFACT_GLOBS_OPTION), limit=8
                )
        stage = state.get("stage")
        if isinstance(stage, str) and stage:
            self.stage = stage
        content_hash = state.get("skill_content_hash")
        if isinstance(content_hash, str) and content_hash:
            self.skill_content_hash = content_hash
        observed = state.get("observed_paths")
        if isinstance(observed, (list, tuple)):
            self.observed_paths = [
                value for value in observed[-_MAX_OBSERVED_PATHS:] if isinstance(value, str)
            ]
        failures = state.get("last_failures")
        if isinstance(failures, (list, tuple)):
            self.last_failures = [
                value for value in failures[-_MAX_FAILURES:] if isinstance(value, str)
            ]
        succeeded = state.get("succeeded_tools")
        if isinstance(succeeded, (list, tuple, set)):
            self._succeeded_tools = {
                value for value in succeeded if isinstance(value, str) and value
            }
        continuations = state.get("continuations")
        if isinstance(continuations, int) and not isinstance(continuations, bool):
            self._continuation_count = max(0, continuations)
        tool_calls = state.get("tool_calls")
        if isinstance(tool_calls, int) and not isinstance(tool_calls, bool):
            self._tool_call_total = max(0, tool_calls)
        self._paused = bool(state.get("paused", self._paused))
        self._released = bool(state.get("released", self._released))
        baseline = state.get("baseline_artifact_signatures")
        if isinstance(baseline, (list, tuple)):
            restored: dict[str, tuple[int, int]] = {}
            for item in baseline[:_MAX_BASELINE_ARTIFACTS]:
                if not isinstance(item, Mapping):
                    continue
                path = item.get("path")
                size = item.get("size")
                mtime_ns = item.get("mtime_ns")
                if (
                    isinstance(path, str)
                    and path
                    and isinstance(size, int)
                    and not isinstance(size, bool)
                    and isinstance(mtime_ns, int)
                    and not isinstance(mtime_ns, bool)
                ):
                    restored[path] = (size, mtime_ns)
            self._baseline_artifact_signatures = restored

    def attach_resume_checkpoint(self, checkpoint: WorkflowPauseCheckpoint) -> None:
        self._resume_checkpoint = checkpoint
        options = checkpoint.workflow_options
        self.skill_name = self.skill_name or options.get(SKILL_NAME_OPTION)
        self.skill_source = self.skill_source or options.get(SKILL_SOURCE_OPTION)
        self.skill_root = self.skill_root or options.get(SKILL_ROOT_OPTION)
        self.task_text = self.task_text or options.get(TASK_TEXT_OPTION)
        if not self.artifact_globs:
            self.artifact_globs = _decode_string_tuple(
                options.get(ARTIFACT_GLOBS_OPTION),
                limit=8,
            )
        if not self.observed_paths:
            self.observed_paths.extend(
                _decode_string_tuple(
                    options.get(OBSERVED_PATHS_OPTION),
                    limit=_MAX_OBSERVED_PATHS,
                )
            )
        if not self.last_failures:
            self.last_failures.extend(
                _decode_string_tuple(
                    options.get(LAST_FAILURES_OPTION),
                    limit=_MAX_FAILURES,
                )
            )

    def checkpoint_options(self) -> dict[str, str]:
        return external_skill_workflow_options(
            skill_name=self.skill_name or "unknown",
            skill_source=self.skill_source or "unknown",
            skill_root=self.skill_root,
            task_text=self.task_text or "",
            artifact_globs=self.artifact_globs,
            observed_paths=tuple(self.observed_paths),
            last_failures=tuple(self.last_failures),
        )

    def build_checkpoint(self) -> str:
        artifact_root = artifact_scan_root(self.workspace_dir, self.artifact_root_dir)
        delivered = bool(
            artifact_root is not None
            and artifact_root.is_dir()
            and any(path.is_file() for path in artifact_root.rglob("*"))
        )
        self.stage = "artifacts_published" if delivered else "skill_active"
        paths = ", ".join(self.observed_paths) if self.observed_paths else "none"
        failures = " | ".join(self.last_failures) if self.last_failures else "none"
        globs = ", ".join(self.artifact_globs) if self.artifact_globs else "none"
        checkpoint_text = (
            f"{EXTERNAL_SKILL_CHECKPOINT_MARKER}\n"
            f"skill_name={self.skill_name or 'unknown'}\n"
            f"skill_source={self.skill_source or 'unknown'}\n"
            f"stage={self.stage}\n"
            f"task={(self.task_text or '').strip() or 'Resume the explicit Skill task.'}\n"
            f"artifact_root={artifact_root or 'none'}\n"
            f"required_artifacts={globs}\n"
            f"observed_working_paths={paths}\n"
            f"recent_failures={failures}\n"
            "This is a Box-Agent host lifecycle contract, not executable Skill code. "
            "Continue following the named Skill without modifying its installed files. "
            "The Skill may keep intermediate work in its own approved directories, but "
            "before declaring completion publish final user-facing files to artifact_root. "
            "For missing facts, call request_user_input. For a finite choice that materially "
            "changes the user-visible result, call request_user_decision instead of only asking "
            "in plain text. Internal implementation choices are yours to make. Preserve "
            "completed work and continue from the next unfinished action."
        )
        if self._resume_checkpoint is not None:
            checkpoint_text = (
                f"{checkpoint_resume_instruction(self._resume_checkpoint)}\n\n"
                f"{checkpoint_text}"
            )
            self._resume_checkpoint = None
        return checkpoint_text

    def context_items(self, context: Any) -> tuple[ContextItem, ...]:
        """Expose the Skill lifecycle checkpoint through the context SPI."""

        del context
        checkpoint = self.build_checkpoint()
        if not checkpoint:
            return ()
        items = [
            ContextItem(
                item_id="workflow:external-skill",
                kind="workflow",
                content=checkpoint,
                priority=900,
                pinned=True,
                metadata={
                    "role": "system",
                    "workflow_context": True,
                    "workflow": self.kind,
                },
            ),
        ]
        skill = self._resolve_skill()
        if skill is not None:
            from ..tools.skill_preload import resolve_skill_preload_attributions

            attributions = resolve_skill_preload_attributions(
                self.skill_loader,
                [skill.name],
            )
            for index, attribution in enumerate(attributions):
                resolved = self.skill_loader.get_skill(attribution.skill_name)
                if resolved is None:
                    continue
                prompt, content_hash = _skill_content_hash(resolved)
                metadata = {
                    "role": "system",
                    "workflow_context": True,
                    "workflow": self.kind,
                    "skill_name": resolved.name,
                    "skill_source": resolved.source,
                    "skill_content_hash": content_hash,
                    "skill_prompt_hash": hashlib.sha256(
                        prompt.encode("utf-8")
                    ).hexdigest(),
                    "skill_usage_role": attribution.usage_role,
                    "dependency_of": attribution.dependency_of,
                }
                if resolved.name == skill.name:
                    expected_hash = self.skill_content_hash
                    if expected_hash is None:
                        self.skill_content_hash = content_hash
                    elif expected_hash != content_hash:
                        metadata["skill_content_hash_mismatch"] = True
                items.append(
                    ContextItem(
                        item_id=f"workflow:skill:{resolved.name}",
                        kind="skill",
                        content=prompt,
                        priority=880 - index,
                        pinned=True,
                        metadata=metadata,
                    )
                )
        return tuple(items)

    def _resolve_skill(self) -> Skill | None:
        loader = self.skill_loader
        name = self.skill_name
        if loader is None or not isinstance(name, str) or not name.strip():
            return None
        try:
            canonical = next(
                (
                    value
                    for value in loader.list_skills()
                    if isinstance(value, str) and value.casefold() == name.casefold()
                ),
                None,
            )
            if canonical is None:
                return None
            return loader.get_skill(canonical)
        except Exception:
            # Skill loading is an optional context contribution.  The
            # lifecycle checkpoint remains available when a source disappears
            # during resume, allowing the model/host to report the issue.
            return None

    def update_checkpoint(self, checkpoint_text: str) -> WorkflowCheckpointUpdate:
        changed = checkpoint_text != self._last_checkpoint_text
        self._last_checkpoint_text = checkpoint_text
        return WorkflowCheckpointUpdate(text=checkpoint_text, changed=changed)

    def next_deterministic_action(self) -> WorkflowAction | None:
        return None

    def _record_candidate_path(self, raw_path: str) -> None:
        if not raw_path or len(raw_path) > 2_048:
            return
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            return
        try:
            resolved = candidate.resolve()
        except OSError:
            return
        if not resolved.exists():
            return
        workspace = (
            Path(self.workspace_dir).expanduser().resolve()
            if self.workspace_dir
            else None
        )
        skill_root = (
            Path(self.skill_root).expanduser().resolve()
            if self.skill_root
            else None
        )
        if not (
            _is_relative_to(resolved, workspace)
            or _is_relative_to(resolved, skill_root)
        ):
            return
        rendered = str(resolved)
        if rendered not in self.observed_paths:
            self.observed_paths.append(rendered)
            del self.observed_paths[_MAX_OBSERVED_PATHS:]

    def record_tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        *,
        executed: bool = True,
    ) -> None:
        for key, value in arguments.items():
            if not isinstance(value, str):
                continue
            if key in {"command", "cmd"}:
                for path in extract_absolute_paths(value):
                    self._record_candidate_path(path)
            elif key in {
                "path",
                "file_path",
                "directory",
                "cwd",
                "workspace",
                "workspace_dir",
                "output_dir",
                "project_dir",
            }:
                self._record_candidate_path(value)
        content = getattr(result, "content", None)
        if isinstance(content, str):
            for path in extract_absolute_paths(content):
                self._record_candidate_path(path)
        if not bool(getattr(result, "success", False)):
            error = str(getattr(result, "error", "") or "Tool execution failed")
            compact = " ".join(error.split())[:_MAX_FAILURE_CHARS]
            if compact:
                self.last_failures.append(f"{tool_name}: {compact}")
                self.last_failures[:] = self.last_failures[-_MAX_FAILURES:]
            return
        gate = self._completion_gate
        if gate is not None and str(getattr(result, "content", "") or "").strip():
            if completion_gate_tool_satisfies_requirements(
                gate, tool_name, dict(arguments)
            ):
                self._succeeded_tools.add(tool_name)
            if tool_name in gate.pause_tools:
                self._paused = True

    def plan_scope_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        return None

    def tool_call_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        verified_evidence_urls: set[str],
        parallel: bool = False,
    ) -> str | None:
        del arguments, verified_evidence_urls, parallel
        gate = self._completion_gate
        if gate is None:
            self._initialize_completion_gate()
            gate = self._completion_gate
        if gate is None:
            return None
        if self._released or tool_name in gate.budget_exempt_tools:
            return None
        if gate.max_tool_calls is not None and self._tool_call_total >= gate.max_tool_calls:
            return f"Skill execution tool budget exhausted ({gate.max_tool_calls})."
        self._tool_call_total += 1
        return None

    def exempts_tool_budget(self, tool_name: str) -> bool:
        return False

    def uses_evidence_read_budget(self, tool_name: str) -> bool:
        return False

    def is_direct_evidence_read_tool(self, tool_name: str) -> bool:
        return False

    def direct_evidence_url(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> str | None:
        return None

    def allows_completion_continuation(self) -> bool:
        return True

    @property
    def max_tool_calls(self) -> int | None:
        """Expose the Skill gate's default tool budget to the generic Kernel."""

        gate = self._completion_gate
        return gate.max_tool_calls if gate is not None else _DEFAULT_MAX_TOOL_CALLS

    def next_continuation(
        self,
        *,
        stop_reason: str,
        final_content: str,
        step: int,
    ) -> WorkflowContinuation | None:
        del final_content, step
        gate = self._completion_gate
        if gate is None:
            self._initialize_completion_gate()
            gate = self._completion_gate
        if (
            gate is None
            or self._released
            or self._paused
            or not is_natural_end_reason(stop_reason)
            or self._continuation_count >= gate.max_continuations
        ):
            return None
        gaps = completion_gate_gaps(
            gate,
            self._succeeded_tools,
            str(self.workspace_dir) if self.workspace_dir is not None else None,
        )
        if not gaps:
            return None
        self._continuation_count += 1
        return WorkflowContinuation(
            continuation_id=f"external-skill:{self._continuation_count}",
            message=Message.user(completion_gate_text(gaps)),
            reason="external_skill_completion_gate",
            metadata={
                "workflow": self.kind,
                "continuation": self._continuation_count,
                "gaps": list(gaps),
            },
        )

    def pause_after_tool(self, tool_name: str, result: Any) -> str | None:
        gate = self._completion_gate
        if gate is None or tool_name not in gate.pause_tools:
            return None
        if not bool(getattr(result, "success", False)):
            return None
        return (
            "The Skill requested a user decision. Progress was checkpointed; "
            "continue this session after providing the decision."
        )

    def terminal_metadata(self, stop_reason: str, final_content: str) -> dict[str, Any]:
        del final_content
        gate = self._completion_gate
        gaps = (
            completion_gate_gaps(
                gate,
                self._succeeded_tools,
                str(self.workspace_dir) if self.workspace_dir is not None else None,
            )
            if gate is not None
            else []
        )
        return {
            "externalSkill": {
                "skillName": self.skill_name,
                "skillSource": self.skill_source,
                "stage": self.stage,
                "continuations": self._continuation_count,
                "toolCalls": self._tool_call_total,
                "paused": self._paused,
                "released": self._released,
                "gaps": list(gaps),
                "lastStopReason": stop_reason,
            }
        }

    def terminal_message(self, stop_reason: str, final_content: str) -> str | None:
        del final_content
        gate = self._completion_gate
        if (
            gate is None
            or self._released
            or self._paused
            or not is_natural_end_reason(stop_reason)
            or self._continuation_count < gate.max_continuations
        ):
            return None
        gaps = completion_gate_gaps(
            gate,
            self._succeeded_tools,
            str(self.workspace_dir) if self.workspace_dir is not None else None,
        )
        if not gaps:
            return None
        return (
            "The Skill reached its bounded completion boundary with delivery "
            "work remaining. Progress was saved; continue the session to resume."
        )

    def handle_control(self, command: Any) -> dict[str, Any] | None:
        kind = getattr(command, "kind", None)
        if kind not in {"workflow.external_skill.release", "external_skill.release"}:
            return None
        self._released = True
        return {"workflow": self.kind, "state": "released"}

    def suppresses_generic_final_summary(self) -> bool:
        return True
