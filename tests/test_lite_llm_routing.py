"""Stable model-binding and utility-routing contract tests.

These tests intentionally target the public run-scoped LLM port and utility
service.  Model selection is runtime policy, not ACP-agent object state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from box_agent.adapters.capabilities import LLMClientPort
from box_agent.api import Message, RunRequest
from box_agent.config import Config, LiteLLMConfig
from box_agent.llm import LLMClient
from box_agent.llm.binding import bind_session_llm, normalize_llm_binding
from box_agent.llm.model_routing import resolve_model_client
from box_agent.schema import LLMProvider, LLMResponse, StreamEvent
from box_agent.services.utility_prompt import (
    UtilityPromptService,
    default_utility_llm_resolver,
)


class _DummyLLM:
    provider = "openai"

    def __init__(self, model: str = "model-main", max_output_tokens: int = 80_000):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.calls: list[dict[str, Any]] = []

    def for_model(self, model: str, *, max_output_tokens: int | None = None):
        return _DummyLLM(model, max_output_tokens or self.max_output_tokens)

    async def generate(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, **kwargs})
        return LLMResponse(content=f"reply-from-{self.model}", finish_reason="stop")

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, **kwargs})
        yield StreamEvent(type="text", delta=f"reply-from-{self.model}")
        yield StreamEvent(type="finish", finish_reason="stop")


def _binding(model: str, *, automatic: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {
        "source": "builtin",
        "model": model,
        "contextWindow": 128_000,
        "maxTokens": 16_000,
    }
    if automatic:
        value["autoRouting"] = {
            "models": [
                {
                    "model": model,
                    "tags": ["analysis", "reasoning"],
                    "abilityLevel": 3,
                },
                {
                    "model": "summary-fast-model",
                    "tags": ["summary", "fast"],
                    "abilityLevel": 1,
                    "maxTokens": 8_000,
                },
            ]
        }
    return value


def test_internal_model_resolver_routes_only_with_explicit_auto_pool():
    manual = _DummyLLM("manual")
    locked, diagnostic = resolve_model_client(manual, task="提炼会话标题")
    assert locked is manual
    assert diagnostic == {"mode": "inherit", "reason": "no_auto_model_pool"}

    automatic = bind_session_llm(
        _DummyLLM(), {"llm_binding": _binding("reasoning-model", automatic=True)}
    )
    routed, diagnostic = resolve_model_client(
        automatic,
        task="提炼会话标题",
        task_tags=("summary", "fast"),
        max_output_tokens_cap=4_096,
    )
    assert routed.model == "summary-fast-model"
    assert routed.max_output_tokens == 4_096
    assert diagnostic["mode"] == "auto"


def test_run_scoped_port_isolates_model_and_auto_pool_between_turns():
    base = _DummyLLM()
    port = LLMClientPort(base)
    automatic = port.for_run(
        RunRequest(
            request_id="request-auto",
            session_id="session-1",
            turn_id="turn-1",
            user_input=Message.user("first"),
            metadata={"llm_binding": _binding("reasoning-model", automatic=True)},
        )
    )
    manual = port.for_run(
        RunRequest(
            request_id="request-manual",
            session_id="session-1",
            turn_id="turn-2",
            user_input=Message.user("second"),
            metadata={"llm_binding": _binding("explicit-model")},
        )
    )

    assert base.model == "model-main"
    assert automatic.model == "reasoning-model"
    assert automatic.max_output_tokens == 16_000
    assert len(automatic.auto_model_candidates) == 2
    assert manual.model == "explicit-model"
    assert manual.auto_model_candidates == ()


@pytest.mark.parametrize(
    ("binding", "message"),
    [
        ({"source": "builtin", "model": "x", "maxTokens": 0}, "maxTokens"),
        ({"source": "builtin", "model": "x", "contextWindow": 0}, "contextWindow"),
        (
            {
                "source": "builtin",
                "model": "x",
                "contextWindow": 16_000,
                "maxTokens": 16_000,
            },
            "must be smaller",
        ),
    ],
)
def test_binding_rejects_invalid_token_limits(binding, message):
    with pytest.raises(ValueError, match=message):
        normalize_llm_binding({"llm_binding": binding})


@pytest.mark.asyncio
async def test_utility_service_uses_run_binding_and_correlation():
    base = _DummyLLM()
    service = UtilityPromptService(default_utility_llm_resolver(base))
    result = await service.prompt(
        {
            "prompt": "请提炼标题",
            "_meta": {
                "purpose": "title",
                "session_id": "office-session",
                "turn_id": "office-turn",
                "llm_binding": _binding("explicit-model"),
            },
        }
    )

    assert result["text"] == "reply-from-explicit-model"


@pytest.mark.asyncio
async def test_utility_service_auto_routes_without_mutating_base():
    base = _DummyLLM()
    service = UtilityPromptService(default_utility_llm_resolver(base))
    result = await service.prompt(
        {
            "prompt": "请提炼标题",
            "_meta": {
                "purpose": "title",
                "llm_binding": _binding("reasoning-model", automatic=True),
            },
        }
    )

    assert result["text"] == "reply-from-summary-fast-model"
    assert base.model == "model-main"


def test_llm_client_for_model_preserves_transport_configuration(tmp_path: Path):
    client = LLMClient(
        api_key="test-key",
        provider=LLMProvider.OPENAI,
        api_base="https://example.invalid/v1",
        model="model-main",
        auth_file=str(tmp_path / "auth.json"),
        max_output_tokens=1_234,
        timeout=42,
    )
    bound = client.for_model("model-session", max_output_tokens=63_999)

    assert bound is not client
    assert client.model == "model-main"
    assert bound.model == "model-session"
    assert bound.api_base == client.api_base
    assert bound.api_key == client.api_key
    assert bound.auth_file == client.auth_file
    assert bound.timeout == client.timeout
    assert bound.max_output_tokens == 63_999
    assert bound._client is not client._client
    assert bound._client.client is client._client.client


def _write_config(tmp_path: Path, lite: dict[str, Any] | None) -> Config:
    data: dict[str, Any] = {
        "api_key": "main-key",
        "api_base": "https://api.anthropic.com",
        "provider": "anthropic",
        "model": "main-model",
    }
    if lite is not None:
        data["lite_llm"] = lite
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return Config.from_yaml(path)


def test_lite_llm_config_absent_keeps_compatibility_default(tmp_path: Path):
    cfg = _write_config(tmp_path, None)
    assert cfg.lite_llm._present is False
    assert cfg.lite_llm.api_base == ""
    assert LiteLLMConfig().max_output_tokens == 63_999


def test_lite_llm_config_parses_explicit_block(tmp_path: Path):
    cfg = _write_config(
        tmp_path,
        {
            "provider": "openai",
            "api_base": "https://api.openai.com/v1",
            "api_key": "lite-key",
            "model": "lite-model",
            "max_output_tokens": 32_000,
        },
    )
    assert cfg.lite_llm._present is True
    assert cfg.lite_llm.model == "lite-model"
    assert cfg.lite_llm.max_output_tokens == 32_000


def test_lite_llm_config_requires_endpoint_and_rejects_provider_ceiling(tmp_path: Path):
    with pytest.raises(ValueError, match="lite_llm.api_base"):
        _write_config(tmp_path, {"provider": "openai", "api_key": "x", "model": "y"})
    with pytest.raises(ValueError, match="65536 ceiling"):
        _write_config(
            tmp_path,
            {
                "provider": "openai",
                "api_base": "https://api.openai.com/v1",
                "api_key": "x",
                "model": "y",
                "max_output_tokens": 65_537,
            },
        )
