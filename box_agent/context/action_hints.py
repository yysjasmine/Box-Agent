"""Action Hint guidance: instruct the model to emit ``action_hint`` markdown
fences for the frontend to render as clickable settings shortcuts.

Two trigger scenarios:

* ``onboarding``: the user's MEMORY.md is scarce (very short, or contains no
  identity hint such as a name).
* ``browser-tools``: the user needs a capability that the available browser
  backend cannot provide and the Playwright MCP server is missing or disabled.

The model decides at generation time *whether* a hint fits the conversation;
this module only injects the contract and lists which tabs are eligible for
this session.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .api import ContextBuildRequest, ContextItem

_MEMORY_MIN_CHARS = 30
# Lower-cased identity keywords. If any is present we consider the user has
# shared at least their name and skip the onboarding hint.
_IDENTITY_KEYWORDS = ("name", "姓名", "我叫", "我是", "叫我")

_HINT_FORMAT_BLOCK = (
    "```action_hint\n"
    "{\n"
    '  "action": "open_settings",\n'
    '  "params": {"tab": "<tab-name>"},\n'
    '  "display_text": "<面向用户的一句话引导文案>"\n'
    "}\n"
    "```"
)
_ACTION_HINT_START = "```action_hint"
_ACTION_HINT_FENCE = "```"
_ALLOWED_ACTION_HINT_TABS = frozenset({"onboarding", "browser-tools"})
_ACTION_HINT_BLOCK_RE = re.compile(
    r"```action_hint[ \t]*(?:\r?\n)?(?P<payload>\{[\s\S]*?\})[ \t\r\n]*```"
)
_JSON_STRING_RE = r'"(?P<value>(?:\\.|[^"\\])*)"'


def is_memory_scarce(memory_text: str | None) -> bool:
    """Return True when MEMORY.md is empty, very short, or has no name hint."""
    if not memory_text:
        return True
    stripped = memory_text.strip()
    if len(stripped) < _MEMORY_MIN_CHARS:
        return True
    lowered = stripped.lower()
    return not any(keyword in lowered for keyword in _IDENTITY_KEYWORDS)


def is_playwright_unavailable(
    mcp_config_path: Path | None,
    *,
    mcp_globally_enabled: bool = True,
) -> bool:
    """Return True when the Playwright MCP server is absent or disabled.

    Missing file or unreadable JSON is treated as "unavailable" — the model
    is told it cannot rely on a browser tool either way. ``mcp_globally_enabled``
    short-circuits to True when the runtime has MCP turned off entirely
    (config ``tools.enable_mcp = false``); in that case no entry in mcp.json
    is going to load.
    """
    if not mcp_globally_enabled:
        return True
    if mcp_config_path is None or not mcp_config_path.exists():
        return True
    try:
        data = json.loads(mcp_config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True

    servers = data.get("mcpServers") or data.get("servers") or {}
    if not isinstance(servers, dict):
        return True

    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        haystack = f"{name} {entry.get('command', '')} {' '.join(map(str, entry.get('args', []) or []))}".lower()
        if "playwright" not in haystack:
            continue
        if entry.get("disabled", False):
            continue
        return False  # Found an enabled playwright entry
    return True


def is_playwright_unavailable_from_env_context(env_context: object | None) -> bool:
    """Return True when host env_context says Playwright is not actually usable."""
    browser_tools = getattr(env_context, "browser_tools", None)
    return getattr(browser_tools, "available", None) is False


def build_action_hints_prompt(
    *,
    memory_scarce: bool,
    playwright_unavailable: bool,
) -> str:
    """Build the system-prompt section that defines the action_hint contract.

    Returns an empty string when no scenario is active so the prompt stays
    lean. Each enabled scenario contributes one bullet describing the tab
    and when to use it.
    """
    rules: list[str] = []
    if memory_scarce:
        rules.append(
            '- 用户问候、自我介绍、问"你是谁/你能做什么"等关系建立类话题，且当前对用户了解很少时 → '
            '使用 `"tab": "onboarding"`，引导用户去"个人记忆"页完善信息。'
        )
    if playwright_unavailable:
        rules.append(
            "- 用户提出不依赖当前真实浏览器状态的自动化测试、截图、网络检查、批量网页操作等需求，"
            '但当前会话没有可用的 Playwright 工具时 → 使用 `"tab": "browser-tools"`，引导用户启用 Playwright。'
            "若需求依赖当前页、登录态或内网，且 `user_browser_*` 工具可用，"
            "应直接使用用户浏览器，不要仅因受管浏览器缺失就输出该提示。"
        )

    if not rules:
        return ""

    return (
        "## 用户引导提示 (Action Hint)\n"
        "当用户的当前问题真正契合下述场景时，在你的回复末尾追加一个 `action_hint` 围栏块。"
        "前端会解析它并渲染为可点击链接，引导用户打开对应的设置页。\n\n"
        "### 格式契约\n"
        "只接受下面这种三反引号 `action_hint` 围栏（不要用 XML 标签）：\n"
        f"{_HINT_FORMAT_BLOCK}\n\n"
        "### 触发场景（仅以下场景启用）\n"
        + "\n".join(rules)
        + "\n\n"
        "### 约束\n"
        "- 必须使用三个反引号包裹的 ```action_hint``` 代码围栏，"
        "禁止使用 `<action_hint>...</action_hint>` 这类 XML/HTML 标签包裹，"
        "否则前端无法识别。\n"
        "- 开始围栏这一行只能写 ```action_hint，不要在同一行追加 `{...}` 或其他内容；"
        "JSON 必须从下一行开始。\n"
        "- 一次回复最多输出一个 `action_hint` 块。\n"
        "- 块内必须是合法 JSON，且 `tab` 字段必须取自上述列表；"
        "`display_text` 必须是一行短文案，不要包含换行符。\n"
        "- 用户语境不契合时不要输出，避免打扰。\n"
        "- 正文先正常回答用户的问题，再追加这个块；不要把它放在正文中间。"
    )


def normalize_action_hint_blocks(text: str) -> str:
    """Normalize known model drift back to the fenced action_hint contract.

    The model sometimes writes JSON on the opening fence line, for example
    `````action_hint {...}`````. The frontend only treats the fence as a
    machine-readable hint when JSON starts on the next line, so ACP repairs the
    narrow, validated shape before streaming it to the host.
    """

    def replace(match: re.Match[str]) -> str:
        normalized_payload = _normalize_action_hint_payload(match.group("payload"))
        if normalized_payload is None:
            return match.group(0)
        return f"{_ACTION_HINT_START}\n{normalized_payload}\n{_ACTION_HINT_FENCE}"

    return _ACTION_HINT_BLOCK_RE.sub(replace, text)


class ActionHintStreamNormalizer:
    """Incrementally normalize action_hint text without delaying normal output."""

    def __init__(self) -> None:
        self._buffer = ""
        self._holding_action_hint = False

    def push(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self._buffer += chunk
        return self._drain()

    def finish(self) -> list[str]:
        output = self._drain()
        if self._buffer:
            output.append(normalize_action_hint_blocks(self._buffer))
            self._buffer = ""
        self._holding_action_hint = False
        return output

    def _drain(self) -> list[str]:
        output: list[str] = []
        while self._buffer:
            if not self._holding_action_hint:
                start = self._buffer.find(_ACTION_HINT_START)
                if start == -1:
                    keep = _partial_action_hint_start_len(self._buffer)
                    emit_len = len(self._buffer) - keep
                    if emit_len > 0:
                        output.append(self._buffer[:emit_len])
                        self._buffer = self._buffer[emit_len:]
                    break
                if start > 0:
                    output.append(self._buffer[:start])
                    self._buffer = self._buffer[start:]
                self._holding_action_hint = True

            close = self._buffer.find(_ACTION_HINT_FENCE, len(_ACTION_HINT_START))
            if close == -1:
                break

            block_end = close + len(_ACTION_HINT_FENCE)
            block = self._buffer[:block_end]
            output.append(normalize_action_hint_blocks(block))
            self._buffer = self._buffer[block_end:]
            self._holding_action_hint = False

        return output


def _normalize_action_hint_payload(payload: str) -> str | None:
    data = _load_jsonish_action_hint_payload(payload.strip())
    if not isinstance(data, dict):
        return None

    action = data.get("action")
    params = data.get("params")
    tab = params.get("tab") if isinstance(params, dict) else None
    display_text = data.get("display_text")
    if (
        action != "open_settings"
        or tab not in _ALLOWED_ACTION_HINT_TABS
        or not isinstance(display_text, str)
    ):
        return None

    display_text = display_text.replace("\r", "").replace("\n", "").strip()
    if not display_text:
        return None

    normalized = {
        "action": "open_settings",
        "params": {"tab": tab},
        "display_text": display_text,
    }
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _load_jsonish_action_hint_payload(payload: str) -> dict[str, object] | None:
    try:
        data = json.loads(payload)
    except ValueError:
        repaired = _escape_control_chars_inside_json_strings(payload)
        try:
            data = json.loads(repaired)
        except ValueError:
            data = _extract_action_hint_payload(payload)
    return data if isinstance(data, dict) else None


def _escape_control_chars_inside_json_strings(text: str) -> str:
    chars: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if not in_string:
            chars.append(char)
            if char == '"':
                in_string = True
            continue

        if escaped:
            chars.append(char)
            escaped = False
        elif char == "\\":
            chars.append(char)
            escaped = True
        elif char == '"':
            chars.append(char)
            in_string = False
        elif char == "\n":
            chars.append("\\n")
        elif char == "\r":
            chars.append("\\r")
        elif char == "\t":
            chars.append("\\t")
        else:
            chars.append(char)
    return "".join(chars)


def _extract_action_hint_payload(payload: str) -> dict[str, object] | None:
    action = _extract_jsonish_string(payload, r'"action"\s*:\s*' + _JSON_STRING_RE)
    tab = _extract_jsonish_string(payload, r'"tab"\s*:\s*' + _JSON_STRING_RE)
    display_text = _extract_jsonish_string(
        payload,
        r'"display_text"\s*:\s*' + _JSON_STRING_RE,
    )
    if action is None or tab is None or display_text is None:
        return None
    return {
        "action": action,
        "params": {"tab": tab},
        "display_text": display_text,
    }


def _extract_jsonish_string(payload: str, pattern: str) -> str | None:
    match = re.search(pattern, payload, flags=re.DOTALL)
    if match is None:
        return None
    value = _escape_control_chars_inside_json_strings(f'"{match.group("value")}"')
    try:
        loaded = json.loads(value)
    except ValueError:
        return match.group("value")
    return loaded if isinstance(loaded, str) else None


def _partial_action_hint_start_len(text: str) -> int:
    max_len = min(len(text), len(_ACTION_HINT_START) - 1)
    for size in range(max_len, 0, -1):
        if _ACTION_HINT_START.startswith(text[-size:]):
            return size
    return 0


class ActionHintContextContributor:
    """Contribute the host UI hint contract through the Context plugin seam."""

    def __init__(
        self,
        *,
        memory_manager: Any | None = None,
        mcp_config_path: Path | None = None,
        mcp_globally_enabled: bool = True,
    ) -> None:
        self._memory_manager = memory_manager
        self._mcp_config_path = mcp_config_path
        self._mcp_globally_enabled = bool(mcp_globally_enabled)

    def provide(self, request: ContextBuildRequest) -> tuple[ContextItem, ...]:
        memory_text = None
        read_core = getattr(self._memory_manager, "read_core", None)
        if callable(read_core):
            try:
                memory_text = read_core()
            except Exception:
                memory_text = None

        metadata: Mapping[str, Any] = request.metadata
        from .environment import EnvContext

        env_context = EnvContext.from_meta(
            metadata.get("env_context", metadata.get("envContext"))
        )
        prompt = build_action_hints_prompt(
            memory_scarce=is_memory_scarce(memory_text),
            playwright_unavailable=(
                is_playwright_unavailable(
                    self._mcp_config_path,
                    mcp_globally_enabled=self._mcp_globally_enabled,
                )
                or is_playwright_unavailable_from_env_context(env_context)
            ),
        )
        if not prompt:
            return ()
        scope = request.session_id or request.run_id or "run"
        return (
            ContextItem(
                item_id=f"{scope}:context:action-hints",
                kind="system",
                content=prompt,
                priority=500,
                pinned=False,
                metadata={"role": "system", "contributor": "action_hints"},
            ),
        )


__all__ = [
    "ActionHintContextContributor",
    "ActionHintStreamNormalizer",
    "build_action_hints_prompt",
    "is_memory_scarce",
    "is_playwright_unavailable",
    "is_playwright_unavailable_from_env_context",
    "normalize_action_hint_blocks",
]
