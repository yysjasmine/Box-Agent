"""Shared, side-effect-free policies for classifying user turns."""

from __future__ import annotations

import re

__all__ = [
    "text_is_short_acknowledgement",
    "text_is_short_non_task_reply",
    "text_requests_plan_start",
]

_PLAN_START_TRIGGERS = (
    "先做规划",
    "先规划",
    "先做计划",
    "先给规划",
    "先给计划",
    "先给方案",
    "先出计划",
    "先出一个计划",
    "先出方案",
    "规划一下",
    "计划一下",
    "制定计划",
    "制定方案",
    "执行方案",
    "任务规划",
    "任务计划",
    "出一个计划",
    "出个计划",
    "给我一个计划",
    "给个计划",
    "做一个计划",
    "做个计划",
    "做个规划",
    "生成计划",
    "创建计划",
    "使用plan",
    "做一个plan",
    "做个plan",
    "生成plan",
    "创建plan",
    "make a plan",
    "make plan",
    "create a plan",
    "write a plan",
    "plan first",
    "planning first",
    "use plan",
    "plan mode",
)

_PLAN_START_NEGATIONS = (
    "不需要计划",
    "无需计划",
    "不要计划",
    "不用计划",
    "别计划",
    "不需要规划",
    "无需规划",
    "不要规划",
    "不用规划",
    "不需要方案",
    "无需方案",
    "不要方案",
    "不用方案",
    "不使用plan",
    "不用plan",
    "不要plan",
    "no plan",
    "without plan",
    "without a plan",
)

_PLAN_START_KEYWORDS = ("计划", "规划")
_STANDALONE_PLAN_RE = re.compile(r"(^|[^a-z])plan([^a-z]|$)")
_SHORT_ACK_STRIP_RE = re.compile(r"[\s,，.。!！?？;；:：\"'“”‘’`]+")
_SHORT_ACKNOWLEDGEMENTS = {
    "ok",
    "okay",
    "k",
    "yes",
    "yeah",
    "yep",
    "sure",
    "approve",
    "approved",
    "confirm",
    "confirmed",
    "continue",
    "proceed",
    "goahead",
    "execute",
    "runit",
    "run",
    "好",
    "好的",
    "可以",
    "可以的",
    "行",
    "行的",
    "没问题",
    "收到",
    "明白",
    "了解",
    "嗯",
    "嗯嗯",
    "继续",
    "继续执行",
    "执行",
    "确认",
    "已确认",
    "同意",
    "批准",
    "开始",
    "开始吧",
    "开始执行",
}
_SHORT_NON_TASK_REPLIES = _SHORT_ACKNOWLEDGEMENTS | {
    "hi",
    "hello",
    "hey",
    "thanks",
    "thankyou",
    "thx",
    "你好",
    "您好",
    "嗨",
    "哈喽",
    "谢谢",
    "谢谢你",
}


def text_requests_plan_start(text: str) -> bool:
    """Return whether ``text`` explicitly asks to start with a plan."""
    normalized = text.lower()
    compact = "".join(normalized.split())
    if any(negation in compact for negation in _PLAN_START_NEGATIONS):
        return False
    return (
        any(trigger in normalized for trigger in _PLAN_START_TRIGGERS)
        or any(keyword in normalized for keyword in _PLAN_START_KEYWORDS)
        or bool(_STANDALONE_PLAN_RE.search(normalized))
    )


def text_is_short_acknowledgement(text: str) -> bool:
    """Return whether ``text`` is a short approval/acknowledgement."""
    compact = _SHORT_ACK_STRIP_RE.sub("", text.strip().lower())
    if not compact or len(compact) > 40:
        return False
    return compact in _SHORT_ACKNOWLEDGEMENTS


def text_is_short_non_task_reply(text: str) -> bool:
    """Return whether ``text`` is a short reply without a concrete task."""
    compact = _SHORT_ACK_STRIP_RE.sub("", text.strip().lower())
    if not compact or len(compact) > 40:
        return False
    return compact in _SHORT_NON_TASK_REPLIES
