"""Stable fingerprints for cache-sensitive LLM request surfaces."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from ..schema import Message
from ..tools.base import Tool


def _json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump())
        except Exception:
            pass

    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }

    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]

    if isinstance(value, str | int | float | bool) or value is None:
        return value

    return str(value)


def stable_json(value: Any) -> str:
    """Return deterministic JSON for hashing request metadata."""

    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [item for item in (str(item).strip() for item in value) if item]
    return [str(value)]


def _tool_schema(tool: Tool) -> dict[str, Any]:
    try:
        return _json_safe(tool.to_schema())
    except Exception as exc:
        name = getattr(tool, "name", type(tool).__name__)
        return {
            "name": str(name),
            "schema_error": f"{type(exc).__name__}: {exc}",
        }


def _mcp_tool_label(tool: Tool) -> str:
    name = str(getattr(tool, "name", type(tool).__name__))
    server_name = str(getattr(tool, "server_name", "") or getattr(tool, "_server_name", ""))
    if server_name:
        return f"{server_name}.{name}"
    if name.startswith("mcp__"):
        return name
    return ""


def build_cache_fingerprint(
    *,
    messages: list[Message],
    tools: list[Tool],
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a compact fingerprint for model-cache-sensitive request inputs."""

    system_messages = [
        {"index": index, "content": message.content}
        for index, message in enumerate(messages)
        if message.role == "system"
    ]
    system_chars = sum(len(str(item["content"] or "")) for item in system_messages)

    tool_schemas = []
    tool_names = []
    mcp_tool_schemas = []
    mcp_tool_names = []
    for tool in tools:
        tool_name = str(getattr(tool, "name", type(tool).__name__))
        tool_schema = _tool_schema(tool)
        tool_names.append(tool_name)
        tool_schemas.append(tool_schema)

        mcp_label = _mcp_tool_label(tool)
        if mcp_label:
            mcp_tool_names.append(mcp_label)
            mcp_tool_schemas.append(tool_schema)

    context = context or {}
    filtered_skill_names = _strings(context.get("filtered_skill_names"))
    preloaded_skill_names = _strings(context.get("preloaded_skill_names"))

    return {
        "system_prompt_hash": sha256_fingerprint(system_messages),
        "system_prompt_chars": system_chars,
        "system_message_count": len(system_messages),
        "tool_schema_hash": sha256_fingerprint(tool_schemas),
        "tool_names_hash": sha256_fingerprint(tool_names),
        "tool_count": len(tool_names),
        "tool_names": tool_names,
        "mcp_tool_schema_hash": sha256_fingerprint(mcp_tool_schemas),
        "mcp_tool_names_hash": sha256_fingerprint(mcp_tool_names),
        "mcp_tool_count": len(mcp_tool_names),
        "mcp_tool_names": mcp_tool_names,
        "filtered_skill_names_hash": sha256_fingerprint(filtered_skill_names),
        "filtered_skill_count": len(filtered_skill_names),
        "filtered_skill_names": filtered_skill_names,
        "preloaded_skill_names_hash": sha256_fingerprint(preloaded_skill_names),
        "preloaded_skill_count": len(preloaded_skill_names),
        "preloaded_skill_names": preloaded_skill_names,
    }
