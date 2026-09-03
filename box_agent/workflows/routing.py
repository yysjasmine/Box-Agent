"""Host-neutral routing predicates shared by workflow adapters."""

from __future__ import annotations

from .turn_policy import text_requests_plan_start

_PRESENTATION_MARKERS = frozenset(
    {
        "ppt",
        "pptx",
        "powerpoint",
        "presentation",
        "slides",
        "幻灯片",
        "演示文稿",
    }
)


def text_requests_native_plan(text: str | None) -> bool:
    """Return whether a plan-first request belongs to the Plan policy.

    Presentation requests stay out of the generic classifier because the
    higher-priority presentation selector owns them.
    """

    lowered = str(text or "").lower()
    if any(marker in lowered for marker in _PRESENTATION_MARKERS):
        return False
    return text_requests_plan_start(str(text or ""))


__all__ = [
    "text_requests_native_plan",
]
