"""Stateful projections from stable Agent events to ACP host metadata."""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Mapping

from box_agent.api import AgentEvent, Usage


def _token_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


class ACPEventProjection:
    """Build office-facing turn usage without owning orchestration policy.

    The projection consumes only durable ``AgentEvent`` values. It never
    looks up a live Tool, Skill, model client, or registry, so replay and live
    rendering produce the same host metadata.
    """

    def __init__(
        self,
        *,
        acp_session_id: str,
        correlation_session_id: str,
        task_id: str,
        turn_id: str,
    ) -> None:
        self._acp_session_id = acp_session_id
        self._correlation_session_id = correlation_session_id
        self._task_id = task_id
        self._turn_id = turn_id
        self._tool_counts: dict[str, int] = {}
        self._mcp_counts: dict[tuple[str, str], int] = {}
        self._skills: list[str] = []
        self._skill_invocations: list[dict[str, Any]] = []
        self._pending_skills: dict[str, str] = {}
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0
        self._model_calls = 0

    def observe(self, event: AgentEvent) -> dict[str, Any] | None:
        """Consume one event and return a changed usage snapshot, if any."""

        if event.type == "tool.call.requested":
            return self._observe_tool_request(event.payload)
        if event.type == "tool.call.completed":
            return self._observe_tool_result(event.payload)
        if event.type == "model.usage":
            self._prompt_tokens += _token_count(event.payload.get("input_tokens"))
            self._completion_tokens += _token_count(
                event.payload.get("output_tokens")
            )
            self._total_tokens += _token_count(event.payload.get("total_tokens"))
            self._model_calls += 1
            return self.snapshot()
        return None

    def finalize(self, usage: Usage | None) -> dict[str, Any]:
        """Return the terminal snapshot, using the Run total as authority."""

        if usage is not None:
            self._prompt_tokens = _token_count(usage.input_tokens)
            self._completion_tokens = _token_count(usage.output_tokens)
            self._total_tokens = _token_count(usage.total_tokens)
            if self._total_tokens > 0 and self._model_calls == 0:
                self._model_calls = 1
        return self.snapshot()

    def snapshot(
        self,
        current: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "turn_usage",
            "version": 3,
            "sessionId": self._correlation_session_id,
            "session_id": self._correlation_session_id,
            "acpSessionId": self._acp_session_id,
            "taskId": self._task_id,
            "task_id": self._task_id,
            "turnId": self._turn_id,
            "turn_id": self._turn_id,
            "skills": list(self._skills),
            "skillInvocations": [dict(item) for item in self._skill_invocations],
            "tools": [
                {"name": name, "count": count}
                for name, count in self._tool_counts.items()
            ],
            "mcp": [
                {
                    "server": server,
                    "tool": tool,
                    "name": f"{server}.{tool}",
                    "count": count,
                }
                for (server, tool), count in self._mcp_counts.items()
            ],
            "tokenUsage": {
                "promptTokens": self._prompt_tokens,
                "completionTokens": self._completion_tokens,
                "totalTokens": self._total_tokens,
                "calls": self._model_calls,
            },
        }
        if current:
            payload["current"] = dict(current)
        return payload

    def _observe_tool_request(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        tool_name = str(payload.get("tool_name", "") or "").strip()
        if not tool_name:
            return None
        metadata = payload.get("metadata", {})
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        source = str(metadata.get("registration_source", "") or "")
        server = str(metadata.get("mcp_server", "") or "").strip()
        mcp_tool_name = str(metadata.get("mcp_tool_name", tool_name) or tool_name)
        if not server and source.startswith("mcp.server:"):
            server = source.removeprefix("mcp.server:").strip()
        if not server and tool_name.startswith("mcp__"):
            parts = tool_name.split("__", 2)
            if len(parts) == 3 and parts[1] and parts[2]:
                server, mcp_tool_name = parts[1], parts[2]
        if server:
            key = (server, mcp_tool_name)
            self._mcp_counts[key] = self._mcp_counts.get(key, 0) + 1
            current = {
                "type": "mcp",
                "name": f"{server}.{mcp_tool_name}",
                "server": server,
                "tool": mcp_tool_name,
            }
        else:
            self._tool_counts[tool_name] = self._tool_counts.get(tool_name, 0) + 1
            current = {"type": "tool", "name": tool_name}
        if tool_name == "get_skill":
            arguments = payload.get("arguments", {})
            if isinstance(arguments, Mapping):
                skill_name = str(arguments.get("skill_name", "") or "").strip()
                call_id = str(payload.get("call_id", "") or "")
                if call_id and skill_name:
                    self._pending_skills[call_id] = skill_name
        return self.snapshot(current)

    def _observe_tool_result(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        call_id = str(payload.get("call_id", "") or "")
        skill_name = self._pending_skills.pop(call_id, "")
        if (
            not skill_name
            or payload.get("status") != "succeeded"
            or str(payload.get("tool_name", "") or "") != "get_skill"
        ):
            return None
        if skill_name not in self._skills:
            self._skills.append(skill_name)
        invocation_key = "\x1f".join(
            (
                self._correlation_session_id,
                self._turn_id,
                skill_name,
            )
        )
        invocation_id = (
            "skill_"
            + sha256(invocation_key.encode("utf-8")).hexdigest()[:32]
        )
        if not any(
            item.get("invocationId") == invocation_id
            for item in self._skill_invocations
        ):
            self._skill_invocations.append(
                {
                    "invocationId": invocation_id,
                    "skillName": skill_name,
                    "activationSource": "get_skill",
                    "status": "succeeded",
                    "usageRole": "primary",
                }
            )
        return self.snapshot({"type": "skill", "name": skill_name})


__all__ = ["ACPEventProjection"]
