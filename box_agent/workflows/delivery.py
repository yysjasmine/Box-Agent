"""Shared deliverable-intent text helpers for completion routing."""

from __future__ import annotations

import re
from typing import Final


DELIVERABLE_INTENT_KEYWORDS: Final[tuple[str, ...]] = (
    "做一份",
    "做一个",
    "制作",
    "生成",
    "创建",
    "新建",
    "导出",
    "输出",
    "保存",
    "另出",
    "继续",
    "接着",
    "补完",
    "完成",
    "write",
    "create",
    "generate",
    "make",
    "remake",
    "build",
    "export",
    "save",
    "continue",
    "resume",
    "finish",
)

_DELIVERABLE_CLAUSE_RE: Final[re.Pattern[str]] = re.compile(r"[^。！？!?；;\n]+")
_DELIVERABLE_ACTION_PATTERN: Final[str] = "|".join(
    (
        rf"(?<![A-Za-z0-9_]){re.escape(keyword)}(?![A-Za-z0-9_])"
        if keyword.isascii()
        else re.escape(keyword)
    )
    for keyword in sorted(DELIVERABLE_INTENT_KEYWORDS, key=len, reverse=True)
)
_DELIVERABLE_ACTION_RE: Final[re.Pattern[str]] = re.compile(
    _DELIVERABLE_ACTION_PATTERN,
    re.IGNORECASE,
)
_ACTION_CONNECTOR_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:然后|随后|接着|之后|再|并且|并|"
    r"\b(?:and(?:\s+then)?|then|afterwards)\b)",
    re.IGNORECASE,
)
_ACTION_AFTER_CONNECTOR_RE: Final[re.Pattern[str]] = re.compile(r"后\s*$")
_INFORMATIONAL_ACTION_PREFIXES: Final[tuple[str, ...]] = (
    "关于",
    "如何",
    "怎么",
    "怎样",
    "介绍",
    "文档",
    "教程",
    "能力",
    "用法",
    "阅读",
    "了解",
    "学习",
    "掌握",
    "研究",
    "查阅",
    "说明",
    "how to",
    "learn",
    "read",
    "understand",
    "explain",
    "documentation",
    "guide",
)
_NEGATED_ACTION_PREFIXES: Final[tuple[str, ...]] = (
    "不要",
    "不用",
    "无需",
    "禁止",
    "避免",
    "do not",
    "don't",
    "without",
    "avoid",
    "never",
)
_INFORMATIONAL_ACTION_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:"
    r"(?:能力(?:介绍|说明)?|用法|方法)"
    r"|(?:的(?:模型(?:的)?)?|模型(?:的)?)"
    r"(?:介绍|能力(?:介绍|说明)?|文档|教程|用法|方法|说明)"
    r"|[^，,。！？!?；;\n]{1,32}?的"
    r"(?:介绍|能力(?:介绍|说明)?|教程|用法|方法|说明)"
    r"(?:部分|一下)?\s*$"
    r")",
    re.IGNORECASE,
)

NEGATED_FORMAT_CLAUSE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:不要|不用|禁止|避免|无需|不能|不可|别|"
    r"\b(?:do\s+not|don't|without|avoid|never|no)\b)"
    r"(?:[^，。；;.!?\n]|\.(?=[A-Za-z0-9]))*",
    re.IGNORECASE,
)

_META_REWRITE_ACTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:优化|润色|改写|重写|整理|调整|修改|编辑|精简|扩写|格式化|"
    r"改(?=(?:得|成|为|一下|下|自然|专业|清晰|通顺|简洁))|"
    r"\b(?:polish|rewrite|rephrase|refine|revise|edit|improve)\b|"
    r"\bformat\b(?=\s+(?:(?:the|this|that|following|above)\s+)?"
    r"(?:prompt|text|copy|content)\b))",
    re.IGNORECASE,
)
_META_PROMPT_TARGET_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:prompt(?:s)?|提示词|提示语|系统提示|指令|需求描述|任务描述|请求文本)",
    re.IGNORECASE,
)
_META_REFERENTIAL_TEXT_TARGET_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:"
    r"(?:"
    r"(?:以上|上述|上面|前述|以下|下面|下方|前面|后面)"
    r"(?:的|这段|这句)?"
    r"|(?:这|该)(?:段|句|些|个|份|几个)?"
    r")"
    r"(?:文字|文本|文案|内容|格式|表达|措辞|句子|段落|字句)"
    r"|(?:(?:the\s+)?(?:above|following|previous|this|that)\s+"
    r"(?:text|copy|wording|content|format))"
    r")",
    re.IGNORECASE,
)
_META_REWRITE_CLAUSE_RE: Final[re.Pattern[str]] = re.compile(
    r"[^。！？!?；;\n]+"
)
_META_EXECUTION_CONNECTOR_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:后|之后|然后|再|接着|随后|并(?:按|根据)?|"
    r"\b(?:then|afterwards|and(?:\s+then)?)\b)",
    re.IGNORECASE,
)
_ARTIFACT_EXECUTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:制作|生成|创建|新建|输出|导出|交付|做(?:一份|一个|个|份)?|"
    r"\b(?:create|generate|make|build|produce|output|export|deliver)\b)"
    r"[^。！？!?；;\n]{0,48}"
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s*deck|slides?|"
    r"presentation|html|网页|web\s*page)",
    re.IGNORECASE,
)
_DIRECT_PRESENTATION_REMAKE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[。！？!?；;\n])\s*(?:(?:请|请帮我|帮我|麻烦(?:帮我)?)?\s*"
    r"重新(?:做|制作|生成|创建)|(?:please\s+)?remake)"
    r"[^。！？!?；;\n]{0,32}"
    r"(?:pptx?|powerpoint|演示文稿|幻灯片|slide\s*deck|slides?|presentation)",
    re.IGNORECASE,
)


def _local_action_start(
    clause: str,
    action_start: int,
    *,
    comma_is_boundary: bool,
) -> int:
    """Return the start governed by connectors and optional comma locality."""
    prefix = clause[:action_start]
    boundary = (
        max(prefix.rfind("，"), prefix.rfind(","))
        if comma_is_boundary
        else -1
    )
    for match in _ACTION_CONNECTOR_RE.finditer(prefix):
        boundary = max(boundary, match.end() - 1)
    after_match = _ACTION_AFTER_CONNECTOR_RE.search(prefix)
    if after_match is not None:
        boundary = max(boundary, after_match.start())
    return boundary + 1


def _local_action_bounds(clause: str, action_start: int) -> tuple[int, int]:
    """Return the connector-delimited delivery segment for one action."""
    following_connector = next(
        _ACTION_CONNECTOR_RE.finditer(clause, action_start),
        None,
    )
    end = (
        following_connector.start()
        if following_connector is not None
        else len(clause)
    )
    return (
        _local_action_start(clause, action_start, comma_is_boundary=False),
        end,
    )


def _local_action_prefix(clause: str, action_start: int) -> str:
    """Return text governing one action after its nearest local boundary."""
    start = _local_action_start(clause, action_start, comma_is_boundary=True)
    return clause[start:action_start].strip().casefold()


def _action_is_informational(clause: str, action: re.Match[str]) -> bool:
    local_prefix = _local_action_prefix(clause, action.start())
    if any(marker in local_prefix for marker in _NEGATED_ACTION_PREFIXES):
        return True
    if any(marker in local_prefix for marker in _INFORMATIONAL_ACTION_PREFIXES):
        return True
    return bool(_INFORMATIONAL_ACTION_SUFFIX_RE.match(clause[action.end() :]))


def extract_deliverable_clauses(text: str) -> tuple[str, ...]:
    """Extract local segments containing an executable artifact-delivery action."""
    clauses: list[str] = []
    for clause_match in _DELIVERABLE_CLAUSE_RE.finditer(text):
        clause = clause_match.group(0).strip()
        if not clause:
            continue
        for action in _DELIVERABLE_ACTION_RE.finditer(clause):
            if _action_is_informational(clause, action):
                continue
            start, end = _local_action_bounds(clause, action.start())
            segment = clause[start:end].strip(" ，,")
            if segment and segment not in clauses:
                clauses.append(segment)
    return tuple(clauses)


def has_deliverable_intent(text: str) -> bool:
    return bool(extract_deliverable_clauses(text))


def strip_negated_format_clauses(text: str) -> str:
    return NEGATED_FORMAT_CLAUSE_RE.sub(" ", text)


def _last_meta_rewrite_span(
    text: str,
) -> tuple[int, int, int, int, int] | None:
    last_match: tuple[int, int, int, int, int] | None = None
    for clause_match in _META_REWRITE_CLAUSE_RE.finditer(text):
        clause = clause_match.group(0)
        actions = list(_META_REWRITE_ACTION_RE.finditer(clause))
        targets = list(_META_PROMPT_TARGET_RE.finditer(clause))
        targets.extend(_META_REFERENTIAL_TEXT_TARGET_RE.finditer(clause))
        clause_matches: list[tuple[int, int, int, int, int, int]] = []
        for action in actions:
            for target in targets:
                distance = max(
                    0,
                    max(action.start(), target.start())
                    - min(action.end(), target.end()),
                )
                if distance > 64:
                    continue
                start = clause_match.start() + min(
                    action.start(),
                    target.start(),
                )
                end = clause_match.start() + max(
                    action.end(),
                    target.end(),
                )
                clause_matches.append(
                    (
                        distance,
                        start,
                        end,
                        clause_match.start() + action.start(),
                        clause_match.start() + target.start(),
                        clause_match.start() + target.end(),
                    )
                )
        if clause_matches:
            _, start, end, action_start, target_start, target_end = min(
                clause_matches,
                key=lambda candidate: (
                    candidate[0],
                    -candidate[2],
                ),
            )
            last_match = (
                start,
                end,
                action_start,
                target_start,
                target_end,
            )
    return last_match


def is_meta_prompt_rewrite_request(text: str) -> bool:
    """Return whether the turn edits request text instead of executing it.

    Artifact words inside the text being polished are references, not delivery
    intent. An explicit follow-on action (for example, "then create the PPT")
    still wins, as does a direct request to remake a presentation.
    """
    normalized = re.sub(
        r"[^\S\n]+",
        " ",
        text.casefold(),
    ).strip()
    if not normalized:
        return False

    positive_text = strip_negated_format_clauses(normalized)
    meta_match = _last_meta_rewrite_span(positive_text)
    if meta_match is None:
        return False
    start, end, action_start, target_start, target_end = meta_match

    if _DIRECT_PRESENTATION_REMAKE_RE.search(positive_text[:start]):
        return False
    if any(
        execution.start() < start and execution.end() > end
        for execution in _ARTIFACT_EXECUTION_RE.finditer(positive_text)
    ):
        return False
    if target_start < action_start:
        execution = _ARTIFACT_EXECUTION_RE.search(positive_text, target_end)
        if execution is not None and execution.start() < action_start:
            return False

    positive_suffix = positive_text[end:]
    connector = _META_EXECUTION_CONNECTOR_RE.search(positive_suffix)
    execution = _ARTIFACT_EXECUTION_RE.search(positive_suffix)
    if (
        connector is not None
        and execution is not None
        and connector.start() <= execution.start()
    ):
        return False
    return True
