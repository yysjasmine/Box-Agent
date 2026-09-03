"""Lazy public boundary for LLM clients and provider implementations."""

from __future__ import annotations

import importlib


_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "LLMClientBase": (".base", "LLMClientBase"),
    "AnthropicClient": (".anthropic_client", "AnthropicClient"),
    "OpenAIClient": (".openai_client", "OpenAIClient"),
    "LLMClient": (".llm_wrapper", "LLMClient"),
    "SessionBoundLLM": (".llm_wrapper", "SessionBoundLLM"),
    "run_lightweight_prompt": (".lightweight", "run_lightweight_prompt"),
    "LightweightResult": (".lightweight", "LightweightResult"),
    "LightweightPromptError": (".lightweight", "LightweightPromptError"),
    "LightweightTimeout": (".lightweight", "LightweightTimeout"),
    "LightweightInvalidArgs": (".lightweight", "LightweightInvalidArgs"),
}


def __getattr__(name: str):
    """Load a provider SDK only when its public client is requested."""

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(importlib.import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))

__all__ = [
    "LLMClientBase",
    "AnthropicClient",
    "OpenAIClient",
    "LLMClient",
    "SessionBoundLLM",
    "run_lightweight_prompt",
    "LightweightResult",
    "LightweightPromptError",
    "LightweightTimeout",
    "LightweightInvalidArgs",
]
