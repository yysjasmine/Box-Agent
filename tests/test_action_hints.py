"""Unit tests for box_agent.acp.action_hints."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from box_agent.context.action_hints import (
    ActionHintContextContributor,
    ActionHintStreamNormalizer,
    build_action_hints_prompt,
    is_memory_scarce,
    is_playwright_unavailable,
    is_playwright_unavailable_from_env_context,
    normalize_action_hint_blocks,
)
from box_agent.context import ContextBuildRequest


# ── is_memory_scarce ────────────────────────────────────────────


def test_memory_scarce_when_none() -> None:
    assert is_memory_scarce(None) is True


def test_memory_scarce_when_empty_string() -> None:
    assert is_memory_scarce("") is True
    assert is_memory_scarce("   \n  \n") is True


def test_memory_scarce_when_under_threshold() -> None:
    assert is_memory_scarce("hello") is True


def test_memory_scarce_when_long_but_no_name() -> None:
    long_no_name = "user likes dark mode and prefers concise answers when possible."
    assert is_memory_scarce(long_no_name) is True


def test_memory_not_scarce_when_has_english_name_keyword() -> None:
    text = "User profile:\n- name: Alice\n- role: data scientist with 5 years experience"
    assert is_memory_scarce(text) is False


def test_memory_not_scarce_when_has_chinese_self_intro() -> None:
    text = "用户偏好简洁回答，技术栈是 Python 和 Rust，我叫张伟。"
    assert is_memory_scarce(text) is False


# ── is_playwright_unavailable ───────────────────────────────────


def test_playwright_unavailable_when_path_none() -> None:
    assert is_playwright_unavailable(None) is True


def test_playwright_unavailable_when_file_missing(tmp_path: Path) -> None:
    assert is_playwright_unavailable(tmp_path / "missing.json") is True


def test_playwright_unavailable_when_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text("{not json", encoding="utf-8")
    assert is_playwright_unavailable(p) is True


def test_playwright_unavailable_when_no_servers(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    assert is_playwright_unavailable(p) is True


def test_playwright_unavailable_when_disabled(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "command": "npx",
                        "args": ["@playwright/mcp"],
                        "disabled": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert is_playwright_unavailable(p) is True


def test_playwright_available_when_enabled(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "command": "npx",
                        "args": ["@playwright/mcp"],
                        "disabled": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert is_playwright_unavailable(p) is False


def test_playwright_unavailable_when_mcp_globally_disabled(tmp_path: Path) -> None:
    """Even with an enabled playwright entry, ``enable_mcp=False`` means no MCP loads."""
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "command": "npx",
                        "args": ["@playwright/mcp"],
                        "disabled": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert is_playwright_unavailable(p, mcp_globally_enabled=False) is True


def test_playwright_detected_via_args_when_named_differently(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "browser": {
                        "command": "npx",
                        "args": ["-y", "@playwright/mcp"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert is_playwright_unavailable(p) is False


def test_playwright_unavailable_from_env_context_when_available_false() -> None:
    ctx = SimpleNamespace(browser_tools=SimpleNamespace(available=False))
    assert is_playwright_unavailable_from_env_context(ctx) is True


def test_playwright_available_from_env_context_when_available_true() -> None:
    ctx = SimpleNamespace(browser_tools=SimpleNamespace(available=True))
    assert is_playwright_unavailable_from_env_context(ctx) is False


def test_playwright_env_context_absent_does_not_force_unavailable() -> None:
    assert is_playwright_unavailable_from_env_context(None) is False
    assert is_playwright_unavailable_from_env_context(SimpleNamespace()) is False


# ── build_action_hints_prompt ───────────────────────────────────


def test_prompt_empty_when_no_scenarios() -> None:
    assert build_action_hints_prompt(memory_scarce=False, playwright_unavailable=False) == ""


def test_prompt_includes_onboarding_only() -> None:
    out = build_action_hints_prompt(memory_scarce=True, playwright_unavailable=False)
    assert '"tab": "onboarding"' in out
    assert "browser-tools" not in out


def test_prompt_includes_browser_tools_only() -> None:
    out = build_action_hints_prompt(memory_scarce=False, playwright_unavailable=True)
    assert "browser-tools" in out
    assert "onboarding" not in out
    assert "user_browser_*" in out
    assert "不要仅因受管浏览器缺失" in out


def test_prompt_includes_both_scenarios() -> None:
    out = build_action_hints_prompt(memory_scarce=True, playwright_unavailable=True)
    assert "onboarding" in out
    assert "browser-tools" in out
    assert "action_hint" in out
    assert "open_settings" in out


def test_prompt_forbids_xml_action_hint_tags() -> None:
    """Prompt must hard-forbid the <action_hint> XML wrapper the model
    occasionally emits, which the frontend cannot reliably parse."""
    out = build_action_hints_prompt(memory_scarce=True, playwright_unavailable=False)
    assert "<action_hint>" in out  # mentioned only as a prohibited form
    assert "禁止" in out
    # The positive example must use the triple-backtick fence.
    assert "```action_hint" in out


def test_prompt_forbids_json_on_action_hint_fence_line() -> None:
    out = build_action_hints_prompt(memory_scarce=True, playwright_unavailable=False)

    assert "不要在同一行追加" in out
    assert "JSON 必须从下一行开始" in out
    assert "display_text" in out
    assert "不要包含换行符" in out


def test_action_hint_guidance_is_a_context_plugin(tmp_path: Path) -> None:
    contributor = ActionHintContextContributor(
        mcp_config_path=tmp_path / "missing-mcp.json"
    )
    items = contributor.provide(
        ContextBuildRequest(
            session_id="session-1",
            run_id="run-1",
            items=(),
            token_budget=10_000,
            metadata={},
        )
    )

    assert len(items) == 1
    assert items[0].metadata["contributor"] == "action_hints"
    assert "onboarding" in items[0].content
    assert "browser-tools" in items[0].content


def test_normalize_action_hint_repairs_json_on_fence_line() -> None:
    malformed = (
        "```action_hint { "
        '"action": "open_settings", '
        '"params": {"tab": "onboarding"}, '
        '"display_text": "去个人记忆页完善偏好，我会更懂你的工作方式。\n" '
        "} ```"
    )

    out = normalize_action_hint_blocks(malformed)

    assert out.startswith("```action_hint\n")
    assert "```action_hint {" not in out
    payload = out.removeprefix("```action_hint\n").removesuffix("\n```")
    data = json.loads(payload)
    assert data == {
        "action": "open_settings",
        "params": {"tab": "onboarding"},
        "display_text": "去个人记忆页完善偏好，我会更懂你的工作方式。",
    }


def test_action_hint_stream_normalizer_handles_split_fence_start() -> None:
    normalizer = ActionHintStreamNormalizer()

    chunks = []
    chunks.extend(normalizer.push("好的。```action"))
    chunks.extend(
        normalizer.push(
            '_hint { "action": "open_settings", '
            '"params": {"tab": "browser-tools"}, '
            '"display_text": "去启用浏览器工具" } ```'
        )
    )
    chunks.extend(normalizer.finish())
    out = "".join(chunks)

    assert out.startswith("好的。```action_hint\n")
    assert "```action_hint {" not in out
    payload = out.split("```action_hint\n", 1)[1].removesuffix("\n```")
    assert json.loads(payload) == {
        "action": "open_settings",
        "params": {"tab": "browser-tools"},
        "display_text": "去启用浏览器工具",
    }
