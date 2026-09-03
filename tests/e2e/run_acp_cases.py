"""Run deterministic ACP cases and write a browser-readable event report.

The runner intentionally uses the public ACP facade and plugin registries, so
a passing report is a direct smoke test for the single Kernel boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

if __package__ in {None, ""}:  # pragma: no cover - direct script invocation
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from box_agent.adapters import KernelACPAgent
from box_agent.api import (
    ContextBuildRequest,
    ContextBuildResult,
    ContextItem,
    MemoryEntry,
    MemoryQuery,
    MemoryRecall,
    Message,
    ModelChunk,
    PermissionDecision,
    ToolCallRequest,
    ToolCallResult,
    Usage,
    WorkflowContinuation,
)
from box_agent.kernel import AgentLoopKernel
from box_agent.persistence import SQLiteEventLog, SQLiteSessionStore
from box_agent.persistence.sqlite import SQLiteStore
from box_agent.plugins import PluginHost
from box_agent.services.kernel import KernelAgentService
from box_agent.tools.base import Tool, ToolResult


CASE_IDS = ("text", "tool_permission", "context_memory", "workflow_continuation", "resume")


class _ACPConnection:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def sessionUpdate(self, update: Any) -> None:
        self.updates.append(update)


class _ScriptedLLM:
    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self.calls = 0
        self.requests: list[Any] = []

    async def stream(self, request: Any):
        self.calls += 1
        self.requests.append(request)
        if self.case_id == "tool_permission" and self.calls == 1:
            yield ModelChunk(
                tool_calls=(
                    ToolCallRequest(
                        call_id="e2e-call-1",
                        tool_name="protected_echo",
                        arguments={"text": "side effect"},
                    ),
                ),
                finish_reason="tool_calls",
                usage=Usage(input_tokens=2, output_tokens=1, total_tokens=3),
            )
            return
        if self.case_id == "workflow_continuation":
            if self.calls == 1:
                yield ModelChunk(
                    content="draft",
                    finish_reason="stop",
                    usage=Usage(input_tokens=2, output_tokens=1, total_tokens=3),
                )
            else:
                yield ModelChunk(
                    content="continued",
                    finish_reason="stop",
                    usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5),
                )
            return
        yield ModelChunk(
            content=f"{self.case_id}:ok",
            finish_reason="stop",
            usage=Usage(input_tokens=2, output_tokens=1, total_tokens=3),
        )


class _ContextPlugin:
    async def assemble(self, request: ContextBuildRequest) -> ContextBuildResult:
        item = ContextItem(
            item_id="plugin-context",
            kind="system",
            content="context plugin says: remembered context",
            priority=50,
            metadata={"role": "system", "source": "e2e"},
        )
        return ContextBuildResult(
            items=(*request.items, item),
            estimated_tokens=sum(entry.estimated_tokens for entry in request.items) + item.estimated_tokens,
        )

    async def restore(self, *, resource_id: str, content_version: str | None = None):
        del resource_id, content_version
        return None


class _MemoryPlugin:
    def __init__(self) -> None:
        self.recalled = False

    async def recall(self, query: MemoryQuery) -> MemoryRecall:
        self.recalled = True
        return MemoryRecall(
            entries=(MemoryEntry("e2e-memory", "remembered memory", kind="fact"),),
            query=query,
        )

    async def write(self, entry: MemoryEntry) -> MemoryEntry:
        return entry

    async def flush(self) -> None:
        return None


class _ProtectedEcho(Tool):
    def __init__(self) -> None:
        self.executions = 0

    @property
    def name(self) -> str:
        return "protected_echo"

    @property
    def description(self) -> str:
        return "A tool that requires an explicit permission decision."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    async def preflight(self, arguments: dict[str, Any], *, context: Any | None = None):
        del arguments, context
        return ToolResult(
            success=False,
            permission_request={"scope": "e2e.tool", "reason": "approve protected echo"},
        )

    async def execute(self, text: str) -> ToolResult:
        self.executions += 1
        return ToolResult(success=True, content=text)


class _ContinuationWorkflow:
    kind = "e2e.workflow"
    checkpoint_injection_id = "e2e.workflow"
    evidence_read_batch_size = 0
    max_tool_calls = None

    def __init__(self) -> None:
        self.continuations = 0

    def context_items(self, context: Any):
        del context
        return ()

    def build_checkpoint(self) -> str | None:
        return None

    def build_checkpoint_payload(self) -> dict[str, Any]:
        return {"continuations": self.continuations}

    def next_continuation(
        self, *, stop_reason: str, final_content: str, step: int
    ) -> WorkflowContinuation | None:
        del stop_reason, final_content, step
        if self.continuations:
            return None
        self.continuations += 1
        return WorkflowContinuation(
            continuation_id="e2e-continuation-1",
            message=Message.user("continue the deterministic case"),
            reason="e2e",
        )


def _build_host(case_id: str) -> tuple[PluginHost, _ScriptedLLM, _ProtectedEcho]:
    host = PluginHost()
    llm = _ScriptedLLM(case_id)
    host.registries["llm.providers"].register("default", llm, source="e2e.llm", version="1")
    context = _ContextPlugin()
    host.registries["context.providers"].register("default", context, source="e2e.context", version="1")
    memory = _MemoryPlugin()
    host.registries["memory.providers"].register("default", memory, source="e2e.memory", version="1")
    protected = _ProtectedEcho()
    if case_id == "tool_permission":
        host.registries["tools.executors"].register(
            protected.name, protected, source="e2e.tool", version="1"
        )
    if case_id == "workflow_continuation":
        host.registries["workflows"].register(
            "default", _ContinuationWorkflow(), source="e2e.workflow", version="1"
        )
    return host, llm, protected


def _prompt_for(case_id: str) -> str:
    return {
        "text": "answer with a deterministic text",
        "tool_permission": "call the protected tool",
        "context_memory": "use the remembered context",
        "workflow_continuation": "finish the workflow",
        "resume": "answer before restart",
    }[case_id]


async def _run_case(case_id: str, root: Path) -> dict[str, Any]:
    started = time.perf_counter()
    db_path = root / f"{case_id}.sqlite3"
    host, llm, protected = _build_host(case_id)
    event_log = SQLiteEventLog(db_path)
    session_store = SQLiteSessionStore(db_path)
    service = KernelAgentService.from_plugin_host(
        host,
        event_log=event_log,
        session_store=session_store,
    )
    conn = _ACPConnection()
    agent = KernelACPAgent(conn, service, system_prompt="E2E system prompt", workspace_dir=str(root))
    await agent.initialize(SimpleNamespace())
    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(root),
            field_meta={
                "e2e_case": case_id,
                "session_id": f"host-{case_id}",
            },
        )
    )
    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[SimpleNamespace(type="text", text=_prompt_for(case_id))],
            field_meta={
                "turnId": f"turn-{case_id}",
                "taskId": f"task-{case_id}",
            },
        )
    )
    status = await service.get_status(session.sessionId)
    bundle = await event_log.load_recovery_bundle(status.run_id)
    events = [event.to_dict() for event in bundle.events]
    result = await (await service.attach(status.run_id)).wait()
    result_data = result.to_dict()
    assertions = _assert_case(case_id, events, result_data, protected, llm)
    turn_usage = [
        update.update.rawOutput
        for update in conn.updates
        if isinstance(getattr(update.update, "rawOutput", None), dict)
        and update.update.rawOutput.get("type") == "turn_usage"
    ]
    expected_usage = result_data.get("usage") or {}
    assertions.append(
        {
            "name": "acp-turn-usage-projection",
            "passed": bool(
                turn_usage
                and turn_usage[-1].get("sessionId") == f"host-{case_id}"
                and turn_usage[-1].get("taskId") == f"task-{case_id}"
                and turn_usage[-1].get("turnId") == f"turn-{case_id}"
                and turn_usage[-1].get("tokenUsage", {}).get("totalTokens")
                == expected_usage.get("total_tokens")
            ),
            "details": turn_usage[-1] if turn_usage else {},
        }
    )

    if case_id == "resume":
        event_log.close()
        SQLiteStore.close(session_store)
        replay_log = SQLiteEventLog(db_path)
        replay_sessions = SQLiteSessionStore(db_path)
        replay_host, _, _ = _build_host(case_id)
        replay_service = KernelAgentService.from_plugin_host(
            replay_host,
            event_log=replay_log,
            session_store=replay_sessions,
        )
        replay_agent = KernelACPAgent(_ACPConnection(), replay_service, workspace_dir=str(root))
        await replay_agent.loadSession(SimpleNamespace(sessionId=session.sessionId, cwd=str(root), field_meta={}))
        replay_handle = await replay_service.resume(status.run_id)
        replay_events = [event.to_dict() async for event in replay_handle.events()]
        assertions.append(
            {
                "name": "resume_events_equal",
                "passed": replay_events == events,
                "details": {"replayed": len(replay_events)},
            }
        )
        replay_log.close()
        SQLiteStore.close(replay_sessions)
    else:
        event_log.close()
        SQLiteStore.close(session_store)

    failed = [assertion for assertion in assertions if not assertion["passed"]]
    return {
        "id": case_id,
        "status": "failed" if failed else "passed",
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "events": events,
        "result": result_data,
        "assertions": assertions,
        "acp_updates": len(conn.updates),
    }


def _assert_case(
    case_id: str,
    events: list[dict[str, Any]],
    result: dict[str, Any],
    protected: _ProtectedEcho,
    llm: _ScriptedLLM,
) -> list[dict[str, Any]]:
    event_types = [event["type"] for event in events]
    assertions: list[dict[str, Any]] = []
    ordered = [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assertions.append({"name": "ordered_events", "passed": ordered})
    assertions.append(
        {
            "name": "terminal_completed",
            "passed": bool(events and event_types[-1] == "run.completed" and result["status"] == "completed"),
        }
    )
    if case_id == "tool_permission":
        requested = event_types.index("permission.requested") if "permission.requested" in event_types else -1
        completed = event_types.index("tool.call.completed") if "tool.call.completed" in event_types else -1
        assertions.append(
            {
                "name": "permission-before-executor",
                "passed": requested >= 0 and completed > requested and protected.executions == 0,
                "details": {"executor_calls": protected.executions},
            }
        )
    if case_id == "context_memory":
        context_events = [event for event in events if event["type"] == "context.assembled"]
        assertions.append(
            {
                "name": "context-manifest",
                "passed": bool(context_events and context_events[0]["payload"].get("manifest", {}).get("content_hash")),
            }
        )
        assertions.append(
            {
                "name": "memory-recalled",
                "passed": "memory.recalled" in event_types and any(
                    any("remembered memory" in str(message.content) for message in request.messages)
                    for request in llm.requests
                ),
            }
        )
    if case_id == "workflow_continuation":
        assertions.append(
            {
                "name": "continuation-budget",
                "passed": event_types.count("workflow.continuation.requested") == 1 and llm.calls == 2,
            }
        )
    return assertions


async def _run_all() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="box-agent-acp-e2e-") as directory:
        root = Path(directory)
        cases = [await _run_case(case_id, root) for case_id in CASE_IDS]
        return {
            "schema_version": 1,
            "generated_by": "tests/e2e/run_acp_cases.py",
            "cases": cases,
        }


def run_cases() -> dict[str, Any]:
    """Run all cases synchronously for pytest and external callers."""

    return asyncio.run(_run_all())


def write_report(path: str | Path, report: dict[str, Any]) -> Path:
    target = Path(path)
    if target.suffix.casefold() != ".json":
        raise ValueError("ACP E2E report path must use the .json extension")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic ACP E2E cases")
    parser.add_argument("--report", type=Path, default=Path("tests/e2e/report.json"))
    args = parser.parse_args()
    report = run_cases()
    write_report(args.report, report)
    # Keep stdout useful for CI/ACP host logs; the full event payload remains in
    # the JSON report consumed by the visual page.
    summary = {
        "report": str(args.report),
        "cases": [
            {
                "id": case["id"],
                "status": case["status"],
                "duration_ms": case["duration_ms"],
                "failed_assertions": [
                    assertion["name"]
                    for assertion in case.get("assertions", [])
                    if not assertion.get("passed")
                ],
            }
            for case in report["cases"]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if all(case["status"] == "passed" for case in report["cases"]) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["CASE_IDS", "main", "run_cases", "write_report"]
