"""Small ACP metadata normalizers shared by native and legacy façades."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_SENSITIVE_KEY_PARTS = (
    "apikey",
    "accesstoken",
    "authtoken",
    "authorization",
    "credential",
    "password",
    "secret",
)


def thinking_enabled_hint(metadata: Any) -> bool | None:
    """Return the host's thinking hint without exposing host names to Kernel.

    ACP clients use several historical spellings.  A neutral
    ``thinking_enabled`` value is expected to win before this helper is
    called; the remaining order keeps the legacy ``deep_think`` spelling
    ahead of the provider-oriented ``chatTemplateKwargs`` envelope.
    """

    if not isinstance(metadata, Mapping):
        return None
    for key in ("deep_think", "deepThink"):
        if key in metadata:
            return bool(metadata[key])
    for key in ("chatTemplateKwargs", "chat_template_kwargs"):
        value = metadata.get(key)
        if isinstance(value, Mapping) and "thinking" in value:
            return bool(value["thinking"])
    return None


def sanitize_metadata(metadata: Any) -> dict[str, Any]:
    """Drop credential-bearing host metadata before it enters a run/session.

    ACP metadata is copied into durable session and event records by the
    native adapter.  Remove common credential spellings recursively; values
    such as ``baseURL`` and ``chatTemplateKwargs`` remain available for
    routing and thinking normalization.
    """

    if not isinstance(metadata, Mapping):
        return {}

    def clean(value: Any) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for raw_key, raw_value in value.items():
                key = str(raw_key)
                normalized = "".join(char for char in key.casefold() if char.isalnum())
                if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                    continue
                result[key] = clean(raw_value)
            return result
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, tuple):
            return tuple(clean(item) for item in value)
        return value

    cleaned = clean(metadata)
    return cleaned if isinstance(cleaned, dict) else {}


def user_decision_response(metadata: Any) -> dict[str, str] | None:
    """Normalize one host response to ``request_user_decision``.

    This is wire translation, not workflow policy: the normalized envelope is
    inserted into the next typed user message so any Workflow plugin can
    consume it without depending on ACP field spellings.
    """

    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get("userDecision", metadata.get("user_decision"))
    if not isinstance(raw, Mapping):
        return None

    def text(*keys: str, limit: int) -> str:
        for key in keys:
            value = raw.get(key)
            if value is not None:
                return str(value).strip()[:limit]
        return ""

    request_id = text("request_id", "requestId", limit=128)
    decision_kind = text("decision_kind", "decisionKind", limit=128)
    option_id = text("selected_option_id", "selectedOptionId", limit=128)
    option_label = text(
        "selected_option_label", "selectedOptionLabel", limit=500
    )
    custom_text = text("custom_text", "customText", limit=2_000)
    trigger = text("trigger", limit=32).lower() or "user"
    if not request_id or not decision_kind or not (option_id or custom_text):
        return None
    if trigger not in {"user", "timeout"}:
        trigger = "user"
    return {
        "request_id": request_id,
        "decision_kind": decision_kind,
        "selected_option_id": option_id,
        "selected_option_label": option_label,
        "custom_text": custom_text,
        "trigger": trigger,
    }


__all__ = [
    "sanitize_metadata",
    "thinking_enabled_hint",
    "user_decision_response",
]
