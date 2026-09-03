"""Host-supplied LLM binding validation shared by every adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .model_routing import normalize_auto_routing


def normalize_llm_binding(meta: Any) -> dict[str, Any] | None:
    """Parse the data-only, session-scoped model binding extension."""

    if not isinstance(meta, Mapping):
        return None
    raw = meta.get("llm_binding") or meta.get("llmBinding")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("llm_binding must be an object")
    raw = dict(raw)

    source = str(raw.get("source") or "").strip()
    model = str(raw.get("model") or "").strip()
    if source != "builtin":
        raise ValueError(f"unsupported llm_binding source: {source or '<empty>'}")
    if (
        not model
        or len(model) > 200
        or any(ord(char) < 32 or ord(char) == 127 for char in model)
    ):
        raise ValueError("llm_binding.model is invalid")
    raw_max_tokens = raw.get("maxTokens", raw.get("max_tokens"))
    raw_context_window = raw.get("contextWindow", raw.get("context_window"))
    if raw_max_tokens is not None and (
        isinstance(raw_max_tokens, bool)
        or not isinstance(raw_max_tokens, int)
        or raw_max_tokens <= 0
    ):
        raise ValueError("llm_binding.maxTokens is invalid")
    if raw_context_window is not None and (
        isinstance(raw_context_window, bool)
        or not isinstance(raw_context_window, int)
        or raw_context_window <= 0
    ):
        raise ValueError("llm_binding.contextWindow is invalid")
    if (
        raw_context_window is not None
        and raw_max_tokens is not None
        and raw_max_tokens >= raw_context_window
    ):
        raise ValueError("llm_binding.maxTokens must be smaller than contextWindow")
    binding: dict[str, Any] = {"source": source, "model": model}
    if raw_context_window is not None:
        binding["contextWindow"] = raw_context_window
    if raw_max_tokens is not None:
        binding["maxTokens"] = raw_max_tokens
    auto_routing = normalize_auto_routing(
        raw.get("autoRouting", raw.get("auto_routing"))
    )
    if auto_routing is not None:
        binding["autoRouting"] = auto_routing
    return binding


def bind_session_llm(
    client: Any,
    metadata: Any,
    *,
    session_id: str = "",
    turn_id: str = "",
    title: str = "",
    call_kind: str = "",
) -> Any:
    """Create an isolated legacy-client view for one Run or utility call."""

    from box_agent.client_info import ClientInfo
    from box_agent.llm.llm_wrapper import SessionBoundLLM

    normalized_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    binding = normalize_llm_binding(normalized_metadata)
    selected = client
    if binding is not None:
        clone_for_model = getattr(selected, "for_model", None)
        if not callable(clone_for_model):
            raise ValueError("configured LLM client does not support session model binding")
        selected = clone_for_model(
            binding["model"],
            max_output_tokens=binding.get("maxTokens"),
        )
    bound = selected if isinstance(selected, SessionBoundLLM) else SessionBoundLLM(selected)
    auto_routing = (binding or {}).get("autoRouting", {})
    candidates = (
        auto_routing.get("models", ())
        if isinstance(auto_routing, dict)
        else ()
    )
    bound.set_auto_model_candidates(
        candidate for candidate in candidates if isinstance(candidate, dict)
    )
    client_info = ClientInfo.from_meta(
        normalized_metadata.get(
            "client_info",
            normalized_metadata.get("clientInfo"),
        )
    )
    bound.set_request_context(
        session_id=session_id,
        turn_id=turn_id,
        title=title,
        call_kind=call_kind,
        client_info=client_info,
    )
    return bound


__all__ = ["bind_session_llm", "normalize_llm_binding"]
