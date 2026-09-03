"""Tool contracts and lazy built-in tool exports.

The stable API/kernel path needs the lightweight ``Tool`` contract and the
registry engine, but it must not import the optional MCP loader merely because
the ``box_agent.tools`` package is initialized. Built-ins keep their historical
names and are imported on first attribute access.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .base import Tool, ToolResult
from .engine import RegistryToolEngine
from .runtime_context import (
    RuntimeInvocation,
    current_runtime_invocation,
    scoped_runtime_invocation,
)

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "BashTool": (".bash_tool", "BashTool"),
    "JsonlQueryTool": (".file", "JsonlQueryTool"),
    "ReadTool": (".file", "ReadTool"),
    "AppendTool": (".file_tools", "AppendTool"),
    "EditTool": (".file_tools", "EditTool"),
    "SearchFilesTool": (".file_tools", "SearchFilesTool"),
    "WriteTool": (".file_tools", "WriteTool"),
    "StagedFileWriteTool": (".staged_file_write_tool", "StagedFileWriteTool"),
    "ObsidianCreateNoteTool": (".obsidian_tool", "ObsidianCreateNoteTool"),
    "ObsidianDailyNoteTool": (".obsidian_tool", "ObsidianDailyNoteTool"),
    "ObsidianUpdateNoteTool": (".obsidian_tool", "ObsidianUpdateNoteTool"),
    "PlanReadTool": (".plan_tool", "PlanReadTool"),
    "PlanStore": (".plan_tool", "PlanStore"),
    "PlanWriteTool": (".plan_tool", "PlanWriteTool"),
    "RequestUserDecisionTool": (
        ".request_user_decision_tool",
        "RequestUserDecisionTool",
    ),
    "RequestUserInputTool": (".request_user_input_tool", "RequestUserInputTool"),
    "TodoReadTool": (".todo_tool", "TodoReadTool"),
    "TodoStore": (".todo_tool", "TodoStore"),
    "TodoWriteTool": (".todo_tool", "TodoWriteTool"),
    "ImageInspectionTool": (".image_inspection_tool", "ImageInspectionTool"),
    "VisionReviewTool": (".vision_review_tool", "VisionReviewTool"),
    "SkillHubInstallTool": (".skillhub_install_tool", "SkillHubInstallTool"),
    "SkillHubSearchTool": (".skillhub_search_tool", "SkillHubSearchTool"),
    "add_workspace_tools": (".setup", "add_workspace_tools"),
    "await_skill_discovery": (".setup", "await_skill_discovery"),
    "initialize_base_tools": (".setup", "initialize_base_tools"),
}

__all__ = [
    "Tool",
    "ToolResult",
    "RegistryToolEngine",
    "ReadTool",
    "JsonlQueryTool",
    "SearchFilesTool",
    "WriteTool",
    "AppendTool",
    "EditTool",
    "StagedFileWriteTool",
    "BashTool",
    "ObsidianCreateNoteTool",
    "ObsidianUpdateNoteTool",
    "ObsidianDailyNoteTool",
    "PlanStore",
    "PlanWriteTool",
    "PlanReadTool",
    "RequestUserInputTool",
    "RequestUserDecisionTool",
    "TodoStore",
    "TodoWriteTool",
    "TodoReadTool",
    "ImageInspectionTool",
    "VisionReviewTool",
    "SkillHubInstallTool",
    "SkillHubSearchTool",
    "RuntimeInvocation",
    "current_runtime_invocation",
    "scoped_runtime_invocation",
    "add_workspace_tools",
    "await_skill_discovery",
    "initialize_base_tools",
]


def __getattr__(name: str) -> Any:
    """Load an optional built-in only when a caller requests it."""

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
