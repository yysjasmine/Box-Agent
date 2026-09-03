"""The native ACP façade only translates service events and controls."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from box_agent.adapters import KernelACPAgent
from box_agent.adapters.acp_kernel import _native_terminal_metadata_text
from box_agent.adapters.acp_metadata import user_decision_response
from box_agent.api import AgentEvent, RunResult, SessionInfo, Usage
from box_agent.workflows import GoalStore


class _Handle:
    run_id = "run-1"

    async def events(self, after_sequence=0):
        yield AgentEvent(
            event_id="content",
            sequence=1,
            session_id="session-1",
            run_id=self.run_id,
            type="model.content.delta",
            payload={"content": "hello"},
        )
        yield AgentEvent(
            event_id="done",
            sequence=2,
            session_id="session-1",
            run_id=self.run_id,
            type="run.completed",
            payload={"final_content": "hello", "stop_reason": "stop"},
        )

    async def wait(self):
        return RunResult(status="completed", stop_reason="stop", final_message="hello")

    async def cancel(self, reason=None):
        return None


class _Service:
    def __init__(self) -> None:
        self.started_request = None
        self.updated_metadata = None

    async def open_session(self, request):
        return SessionInfo("session-1", "now", request.metadata)

    async def start(self, request):
        self.started_request = request
        return _Handle()

    async def update_session_metadata(self, session_id, metadata):
        self.updated_metadata = (session_id, dict(metadata))
        return SessionInfo(session_id, "now", metadata)


class _Connection:
    def __init__(self):
        self.updates = []

    async def sessionUpdate(self, update):
        self.updates.append(update)


def test_acp_metadata_normalizes_structured_user_decision_response() -> None:
    assert user_decision_response(
        {
            "userDecision": {
                "request_id": "decision-1",
                "decision_kind": "delivery_scope",
                "selected_option_id": "keep_full",
                "selected_option_label": "保持完整版本",
                "trigger": "timeout",
            }
        }
    ) == {
        "request_id": "decision-1",
        "decision_kind": "delivery_scope",
        "selected_option_id": "keep_full",
        "selected_option_label": "保持完整版本",
        "custom_text": "",
        "trigger": "timeout",
    }


@pytest.mark.asyncio
async def test_kernel_acp_inserts_decision_as_typed_user_message() -> None:
    service = _Service()
    agent = KernelACPAgent(_Connection(), service)
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))
    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="继续")],
            field_meta={
                "userDecision": {
                    "requestId": "decision-1",
                    "decisionKind": "delivery_scope",
                    "customText": "突出案例",
                }
            },
        )
    )

    content = service.started_request.user_input.content
    assert "[HOST_USER_DECISION_RESPONSE]" in content
    assert '"custom_text": "突出案例"' in content
    assert content.endswith("继续")


@pytest.mark.asyncio
async def test_kernel_acp_negotiates_and_seeds_bounded_continuation_once() -> None:
    service = _Service()
    agent = KernelACPAgent(_Connection(), service)
    session = await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))
    assert session.field_meta["capabilities"]["session_continuation_versions"] == [1]
    continuation = {
        "schema_version": "officev3-session-continuation/v1",
        "product_session_id": "product-session",
        "reason": "artifact_binding_changed",
        "messages": [
            {"role": "user", "content": "制作融资 BP"},
            {"role": "assistant", "content": "已生成 index.html"},
        ],
    }

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="修改标题")],
            field_meta={"session_continuation": continuation},
        )
    )
    first_content = service.started_request.user_input.content
    assert "[HOST_SESSION_CONTINUATION]" in first_content
    assert "制作融资 BP" in first_content
    assert first_content.endswith("修改标题")

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="继续")],
            field_meta={"session_continuation": continuation},
        )
    )
    assert service.started_request.user_input.content == "继续"


def test_native_autopilot_terminal_text_counts_no_progress_turns() -> None:
    """Native host rendering must match legacy CLI's no-progress count."""

    text = _native_terminal_metadata_text(
        {
            "goalAutopilot": {
                "enabled": True,
                "continuations": 3,
                "noProgressTurns": 2,
                "stopCause": "no_progress",
            }
        }
    )

    assert "after 2 continuation(s) without recorded goal progress" in text
    assert "after 3 continuation(s) without recorded goal progress" not in text


def test_kernel_acp_opens_native_session_without_shadow_runtime():
    async def scenario() -> None:
        agent = KernelACPAgent(_Connection(), _Service())
        await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))
        assert "session-1" in agent._sessions

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_kernel_acp_persists_expert_profile_as_session_metadata() -> None:
    service = _Service()
    agent = KernelACPAgent(_Connection(), service)
    await agent.newSession(
        SimpleNamespace(
            cwd=".",
            field_meta={
                "expert": {
                    "id": "researcher",
                    "name": "行业研究员",
                    "defaultSkills": ["web-research"],
                }
            },
        )
    )

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="continue")],
            field_meta={},
        )
    )

    assert service.started_request.metadata["expert"]["id"] == "researcher"
    assert service.started_request.metadata["expert"]["defaultSkills"] == [
        "web-research"
    ]


@pytest.mark.asyncio
async def test_kernel_acp_applies_session_metadata_plugin_defaults_before_persistence() -> None:
    class WorkspaceDefaults:
        async def contribute(self, request):
            assert request.metadata["workspace_dir"]
            return {"session_mode": "code_agent", "artifact_mode": "project"}

    agent = KernelACPAgent(
        _Connection(),
        _Service(),
        session_metadata_contributors=(WorkspaceDefaults(),),
    )
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    metadata = agent._sessions["session-1"]["metadata"]
    assert metadata["session_mode"] == "code_agent"
    assert metadata["artifact_mode"] == "project"


@pytest.mark.asyncio
async def test_kernel_acp_explicit_metadata_overrides_session_plugin_defaults() -> None:
    class WorkspaceDefaults:
        def contribute(self, request):
            return {"session_mode": "code_agent", "artifact_mode": "project"}

    agent = KernelACPAgent(
        _Connection(),
        _Service(),
        session_metadata_contributors=(WorkspaceDefaults(),),
    )
    await agent.newSession(
        SimpleNamespace(
            cwd=".",
            field_meta={"session_mode": "data_analysis", "artifact_mode": "output"},
        )
    )

    metadata = agent._sessions["session-1"]["metadata"]
    assert metadata["session_mode"] == "data_analysis"
    assert metadata["artifact_mode"] == "output"


@pytest.mark.asyncio
async def test_kernel_acp_keeps_security_and_tool_graph_metadata_session_immutable(
    tmp_path: Path,
) -> None:
    service = _Service()
    agent = KernelACPAgent(_Connection(), service)
    await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "permission_mode": "default",
                "filesystem_policy": {"filesystem_scope": "session_workspace"},
                "artifact_mode": "project",
                "env_context": {"platform": "win32"},
            },
        )
    )

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="continue")],
            field_meta={
                "permission_mode": "full_access",
                "filesystem_policy": {"filesystem_scope": "user_home"},
                "artifact_mode": "output",
                "env_context": {"platform": "darwin"},
            },
        )
    )

    metadata = service.started_request.metadata
    assert metadata["permission_mode"] == "default"
    assert metadata["filesystem_policy"]["filesystem_scope"] == "session_workspace"
    assert metadata["artifact_mode"] == "project"
    assert metadata["env_context"]["platform"] == "win32"


@pytest.mark.asyncio
async def test_kernel_acp_goal_extension_uses_native_session_store() -> None:
    goals = GoalStore()
    service = _Service()
    agent = KernelACPAgent(
        _Connection(),
        service,
        goal_store=goals,
        native_workflow_ids={"goal", "autopilot"},
    )
    await agent.newSession(
        SimpleNamespace(
            cwd=".",
            field_meta={
                "workflow_id": "goal",
                "goal": {
                    "objective": "Resume the native Goal",
                    "status": "paused",
                    "progress": ["checkpoint restored"],
                },
            },
        )
    )

    restored = goals.get("session-1")
    assert restored is not None
    assert restored.status == "paused"
    result = await agent.extMethod(
        "goal",
        {"sessionId": "session-1", "action": "resume"},
    )

    assert result["ok"] is True
    assert result["goal"]["status"] == "active"
    assert service.updated_metadata[0] == "session-1"
    assert service.updated_metadata[1]["goal"]["status"] == "active"


@pytest.mark.asyncio
async def test_kernel_acp_load_session_reuses_durable_native_session() -> None:
    conn = _Connection()
    agent = KernelACPAgent(
        conn,
        _Service(),
        workspace_dir=".",
    )

    response = await agent.loadSession(
        SimpleNamespace(
            sessionId="session-1",
            cwd=".",
            field_meta={"source": "reconnect"},
        )
    )

    assert response is not None
    assert agent._sessions["session-1"]["metadata"]["source"] == "reconnect"


@pytest.mark.asyncio
async def test_kernel_acp_keeps_session_workspace_and_system_prompt_host_immutable(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()

    class DurableService(_Service):
        def __init__(self) -> None:
            super().__init__()
            self.session = None

        async def open_session(self, request):
            self.session = SessionInfo("session-1", "now", request.metadata)
            return self.session

        async def load_session(self, request):
            assert self.session is not None
            return self.session

        async def update_session_metadata(self, session_id, metadata):
            assert self.session is not None
            self.session = SessionInfo(session_id, self.session.created_at, metadata)
            return self.session

    service = DurableService()
    first = KernelACPAgent(_Connection(), service, system_prompt="trusted system")
    await first.newSession(
        SimpleNamespace(
            cwd=str(workspace),
            field_meta={
                "cwd": str(other_workspace),
                "workspace_dir": str(other_workspace),
                "system_prompt": "untrusted override",
            },
        )
    )
    await first.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="hello")],
            field_meta={
                "workspace_dir": str(other_workspace),
                "system_prompt": "untrusted turn override",
            },
        )
    )

    assert service.started_request.metadata["workspace_dir"] == str(
        workspace.resolve()
    )
    assert service.started_request.metadata["system_prompt"] == "trusted system"
    assert service.session.metadata["workspace_dir"] == str(workspace.resolve())

    restarted = KernelACPAgent(_Connection(), service, system_prompt="trusted system")
    with pytest.raises(ValueError, match="workspace"):
        await restarted.loadSession(
            SimpleNamespace(
                sessionId="session-1",
                cwd=str(other_workspace),
                field_meta={},
            )
        )


@pytest.mark.asyncio
async def test_kernel_acp_recovers_from_malformed_internal_turn_counter(tmp_path) -> None:
    class PersistedService(_Service):
        async def load_session(self, request):
            return SessionInfo(
                request.session_id,
                "now",
                {
                    "workspace_dir": str(tmp_path.resolve()),
                    "_acp_turn_counter": "not-an-integer",
                },
            )

    service = PersistedService()
    agent = KernelACPAgent(_Connection(), service)
    await agent.loadSession(
        SimpleNamespace(sessionId="session-1", cwd=str(tmp_path), field_meta={})
    )
    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="hello")],
            field_meta={},
        )
    )

    assert service.started_request.turn_id == "session-1-turn-1"


@pytest.mark.asyncio
async def test_kernel_acp_validates_and_canonicalizes_session_llm_binding() -> None:
    agent = KernelACPAgent(_Connection(), _Service())

    with pytest.raises(ValueError, match="maxTokens"):
        await agent.newSession(
            SimpleNamespace(
                cwd=".",
                field_meta={
                    "llmBinding": {
                        "source": "builtin",
                        "model": "demo",
                        "maxTokens": 0,
                    }
                },
            )
        )

    await agent.newSession(
        SimpleNamespace(
            cwd=".",
            field_meta={
                "llmBinding": {
                    "source": "builtin",
                    "model": "demo",
                    "contextWindow": 128000,
                    "maxTokens": 16000,
                }
            },
        )
    )

    assert agent._sessions["session-1"]["metadata"]["llm_binding"] == {
        "source": "builtin",
        "model": "demo",
        "contextWindow": 128000,
        "maxTokens": 16000,
    }


@pytest.mark.asyncio
async def test_kernel_acp_advertises_session_load_capability() -> None:
    agent = KernelACPAgent(_Connection(), _Service())

    response = await agent.initialize(SimpleNamespace())

    assert response.agentCapabilities.loadSession is True


@pytest.mark.asyncio
async def test_kernel_acp_agent_renders_normalized_service_events() -> None:
    conn = _Connection()
    agent = KernelACPAgent(conn, _Service(), system_prompt="system")
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))
    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="hello")],
            field_meta={},
        )
    )

    assert response.stopReason == "end_turn"
    assert any(
        getattr(update.update, "sessionUpdate", "") == "agent_message_chunk"
        for update in conn.updates
    )
    assert any(
        isinstance(getattr(update.update, "rawOutput", None), dict)
        and update.update.rawOutput.get("type") == "turn_usage"
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_kernel_acp_normalizes_split_action_hint_at_rendering_boundary() -> None:
    class HintHandle(_Handle):
        async def events(self, after_sequence=0):
            del after_sequence
            for sequence, content in enumerate(
                (
                    "answer```action",
                    '_hint {"action":"open_settings","params":{"tab":"onboarding"},'
                    '"display_text":"完善偏好"}```',
                ),
                start=1,
            ):
                yield AgentEvent(
                    event_id=f"content-{sequence}",
                    sequence=sequence,
                    session_id="session-1",
                    run_id=self.run_id,
                    type="model.content.delta",
                    payload={"content": content},
                )

    class HintService(_Service):
        async def start(self, request):
            self.started_request = request
            return HintHandle()

    conn = _Connection()
    agent = KernelACPAgent(conn, HintService())
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))
    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="hello")],
            field_meta={},
        )
    )

    rendered = "".join(str(update) for update in conn.updates)
    assert "```action_hint\\n" in rendered
    assert "```action_hint {" not in rendered


@pytest.mark.asyncio
async def test_kernel_acp_exposes_native_terminal_metadata_and_stop_reason() -> None:
    class MetadataHandle(_Handle):
        async def wait(self):
            return RunResult(
                status="completed",
                stop_reason="checkpoint_paused",
                final_message="paused",
                metadata={
                    "runStatus": "paused",
                    "planApproval": {"state": "pending"},
                },
            )

    class MetadataService(_Service):
        async def start(self, request):
            self.started_request = request
            return MetadataHandle()

    conn = _Connection()
    agent = KernelACPAgent(conn, MetadataService())
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="prepare a plan")],
            field_meta={},
        )
    )

    assert response.stopReason == "end_turn"
    assert response.field_meta["runStatus"] == "paused"
    assert response.field_meta["planApproval"]["state"] == "pending"


@pytest.mark.asyncio
async def test_kernel_acp_renders_native_terminal_message_when_no_content_delta() -> None:
    class PauseHandle:
        run_id = "pause-run"

        async def events(self, after_sequence=0):
            if False:
                yield None

        async def wait(self):
            return RunResult(
                status="completed",
                stop_reason="checkpoint_paused",
                final_message="计划已生成，等待用户确认后再执行。",
                metadata={"runStatus": "paused", "recoverable": True},
            )

    class PauseService(_Service):
        async def start(self, request):
            self.started_request = request
            return PauseHandle()

    conn = _Connection()
    agent = KernelACPAgent(conn, PauseService())
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="prepare a plan")],
            field_meta={},
        )
    )

    assert response.stopReason == "end_turn"
    assert any(
        getattr(update.update, "sessionUpdate", None) == "agent_message_chunk"
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_kernel_acp_renders_native_autopilot_stop_boundary() -> None:
    class AutopilotHandle:
        run_id = "autopilot-run"

        async def events(self, after_sequence=0):
            if False:
                yield None

        async def wait(self):
            return RunResult(
                status="completed",
                stop_reason="end_turn",
                final_message="done",
                metadata={
                    "goalAutopilot": {
                        "enabled": True,
                        "continuations": 1,
                        "stopCause": "no_progress",
                        "noProgressTurns": 1,
                    }
                },
            )

    class AutopilotService(_Service):
        async def start(self, request):
            self.started_request = request
            return AutopilotHandle()

    conn = _Connection()
    agent = KernelACPAgent(conn, AutopilotService())
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="continue goal")],
            field_meta={},
        )
    )

    assert response.stopReason == "end_turn"
    assert any(
        "without recorded goal progress" in str(update)
        for update in conn.updates
    )


@pytest.mark.asyncio
async def test_kernel_acp_renders_native_completion_gate_pause_boundary() -> None:
    class CompletionHandle:
        run_id = "completion-run"

        async def events(self, after_sequence=0):
            del after_sequence
            yield AgentEvent(
                event_id="content",
                sequence=1,
                session_id="session-1",
                run_id=self.run_id,
                type="model.content.delta",
                payload={"content": "done"},
            )

        async def wait(self):
            return RunResult(
                status="completed",
                stop_reason="checkpoint_paused",
                final_message="The recoverable workflow reached its bounded continuation boundary.",
                metadata={
                    "completionGate": {
                        "continuations": 1,
                        "maxContinuations": 1,
                        "budgetExhausted": True,
                        "gaps": ["tool `verify` is missing"],
                    }
                },
            )

    class CompletionService(_Service):
        async def start(self, request):
            self.started_request = request
            return CompletionHandle()

    conn = _Connection()
    agent = KernelACPAgent(conn, CompletionService())
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="finish")],
            field_meta={},
        )
    )

    assert any("completion gate stopped" in str(update).lower() for update in conn.updates)


@pytest.mark.asyncio
async def test_kernel_acp_renders_native_plan_snapshot_as_tool_card() -> None:
    conn = _Connection()
    agent = KernelACPAgent(conn, _Service())

    await agent._render(
        AgentEvent(
            event_id="plan-event",
            sequence=1,
            session_id="session-1",
            run_id="run-1",
            type="workflow.plan.snapshot",
            payload={
                "type": "plan_snapshot",
                "action": "start",
                "plan": {"title": "Preparing execution plan"},
            },
        )
    )

    assert len(conn.updates) == 2


@pytest.mark.asyncio
async def test_kernel_acp_renders_plan_tool_output_as_legacy_raw_snapshot() -> None:
    conn = _Connection()
    agent = KernelACPAgent(conn, _Service())
    snapshot = {
        "type": "plan_snapshot",
        "action": "set",
        "plan": {"title": "Release", "status": "active"},
        "summary": {"steps": 1},
    }

    await agent._render(
        AgentEvent(
            event_id="plan-tool-event",
            sequence=2,
            session_id="session-1",
            run_id="run-1",
            type="tool.call.completed",
            payload={
                "call_id": "plan-write-1",
                "tool_name": "plan_write",
                "status": "succeeded",
                "content": "Plan updated",
                "output": snapshot,
            },
        )
    )

    assert len(conn.updates) == 1
    assert conn.updates[0].update.rawOutput == snapshot


@pytest.mark.asyncio
async def test_kernel_acp_maps_deep_think_metadata_to_kernel_run_options() -> None:
    """ACP's deep_think hint must reach the provider-neutral Kernel contract."""

    service = _Service()
    agent = KernelACPAgent(_Connection(), service, system_prompt="system")
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="explain carefully")],
            field_meta={"deep_think": True},
        )
    )

    assert response.stopReason == "end_turn"
    assert service.started_request is not None
    assert service.started_request.options.thinking_enabled is True


@pytest.mark.asyncio
async def test_kernel_acp_normalizes_host_identity_and_typed_attachments() -> None:
    service = _Service()
    agent = KernelACPAgent(_Connection(), service, workspace_dir="D:/workspace")
    await agent.newSession(
        SimpleNamespace(
            cwd="D:/workspace",
            field_meta={"session_id": "office-session-1", "title": "Quarterly review"},
        )
    )

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="What is visible?")],
            field_meta={
                "turnId": "office-turn-1",
                "image_attachment_paths": ["slide.png", "slide.png", 3],
            },
        )
    )

    request = service.started_request
    assert request.turn_id == "office-turn-1"
    assert request.metadata["correlation_session_id"] == "office-session-1"
    assert request.metadata["correlation_turn_id"] == "office-turn-1"
    assert request.metadata["title"] == "Quarterly review"
    assert len(request.attachments) == 1
    assert request.attachments[0].kind == "image"
    assert request.attachments[0].uri == "file:///D:/workspace/slide.png"


@pytest.mark.asyncio
async def test_kernel_acp_response_reports_turn_usage_and_fallback_turn_identity() -> None:
    class UsageHandle(_Handle):
        async def wait(self):
            return RunResult(
                status="completed",
                stop_reason="stop",
                final_message="hello",
                usage=Usage(input_tokens=2, output_tokens=3, total_tokens=5),
            )

    class UsageService(_Service):
        async def start(self, request):
            self.started_request = request
            return UsageHandle()

    service = UsageService()
    agent = KernelACPAgent(_Connection(), service)
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    first = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="one")],
            field_meta={},
        )
    )
    second = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="two")],
            field_meta={},
        )
    )

    assert first.field_meta["usage"]["totalTokens"] == 5
    assert first.field_meta["usage"]["turnId"] == "session-1-turn-1"
    assert second.field_meta["usage"]["turnId"] == "session-1-turn-2"


@pytest.mark.asyncio
async def test_kernel_acp_projects_durable_events_to_office_turn_usage() -> None:
    class UsageEventHandle(_Handle):
        async def events(self, after_sequence=0):
            del after_sequence
            yield AgentEvent(
                event_id="tool-request",
                sequence=1,
                session_id="session-1",
                run_id=self.run_id,
                turn_id="office-turn",
                type="tool.call.requested",
                payload={
                    "call_id": "search-1",
                    "tool_name": "search",
                    "arguments": {"q": "plugins"},
                    "metadata": {
                        "registration_source": "mcp.server:research",
                        "mcp_server": "research",
                    },
                },
            )
            yield AgentEvent(
                event_id="usage",
                sequence=2,
                session_id="session-1",
                run_id=self.run_id,
                turn_id="office-turn",
                type="model.usage",
                payload={
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "total_tokens": 5,
                },
            )
            yield AgentEvent(
                event_id="done",
                sequence=3,
                session_id="session-1",
                run_id=self.run_id,
                turn_id="office-turn",
                type="run.completed",
                payload={"stop_reason": "stop", "final_content": "done"},
            )

        async def wait(self):
            return RunResult(
                status="completed",
                stop_reason="stop",
                final_message="done",
                usage=Usage(input_tokens=2, output_tokens=3, total_tokens=5),
            )

    class UsageEventService(_Service):
        async def start(self, request):
            self.started_request = request
            return UsageEventHandle()

    conn = _Connection()
    agent = KernelACPAgent(conn, UsageEventService())
    await agent.newSession(
        SimpleNamespace(
            cwd=".",
            field_meta={"session_id": "billing-session"},
        )
    )

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="search")],
            field_meta={"turnId": "office-turn", "taskId": "task-1"},
        )
    )

    usage_payloads = [
        update.update.rawOutput
        for update in conn.updates
        if isinstance(getattr(update.update, "rawOutput", None), dict)
        and update.update.rawOutput.get("type") == "turn_usage"
    ]
    assert usage_payloads
    assert usage_payloads[-1]["sessionId"] == "billing-session"
    assert usage_payloads[-1]["taskId"] == "task-1"
    assert usage_payloads[-1]["turnId"] == "office-turn"
    assert usage_payloads[-1]["tokenUsage"]["totalTokens"] == 5
    assert usage_payloads[-1]["mcp"][0]["name"] == "research.search"


@pytest.mark.asyncio
async def test_kernel_acp_renders_subagent_progress_and_artifacts_from_stable_events() -> None:
    conn = _Connection()
    agent = KernelACPAgent(conn, _Service())

    await agent._render(
        AgentEvent(
            event_id="sub-progress",
            sequence=3,
            session_id="session-1",
            run_id="run-1",
            turn_id="turn-1",
            type="tool.progress",
            payload={
                "call_id": "sub-call-1",
                "tool_name": "sub_agent",
                "kind": "subagent.event",
                "data": {
                    "sub_agent_id": "child-1",
                    "title": "Inspect files",
                    "event": {"type": "model.content.delta", "payload": {"content": "working"}},
                },
            },
        )
    )
    await agent._render(
        AgentEvent(
            event_id="artifact",
            sequence=4,
            session_id="session-1",
            run_id="run-1",
            turn_id="turn-1",
            type="artifact.created",
            payload={
                "call_id": "write-1",
                "filename": "report.html",
                "uri": "file:///D:/workspace/output/report.html",
                "mime": "text/html",
            },
        )
    )

    outputs = [update.update.rawOutput for update in conn.updates]
    assert outputs[0]["type"] == "sub_agent_progress"
    assert outputs[0]["parentToolCallId"] == "sub-call-1"
    assert outputs[1]["type"] == "artifact"
    assert outputs[1]["filename"] == "report.html"


@pytest.mark.asyncio
async def test_kernel_acp_renders_context_plugin_host_projections() -> None:
    conn = _Connection()
    agent = KernelACPAgent(conn, _Service())

    await agent._render(
        AgentEvent(
            event_id="context-event",
            sequence=1,
            session_id="session-1",
            run_id="run-1",
            type="context.assembled",
            payload={
                "host_projections": [
                    {
                        "projection_id": "expert-progress",
                        "surface": "raw_output",
                        "schema_version": 1,
                        "payload": {
                            "type": "expert_team_progress",
                            "teamId": "research",
                        },
                    }
                ]
            },
        )
    )

    outputs = [
        update.update.rawOutput
        for update in conn.updates
        if isinstance(getattr(update.update, "rawOutput", None), dict)
    ]
    assert outputs[0] == {
        "type": "expert_team_progress",
        "teamId": "research",
    }
    assert outputs[-1]["type"] == "context.assembled"


@pytest.mark.asyncio
async def test_kernel_acp_maps_host_chat_template_thinking_to_run_options() -> None:
    """Host model config naming must stop at the ACP adapter boundary."""

    service = _Service()
    agent = KernelACPAgent(_Connection(), service, system_prompt="system")
    await agent.newSession(
        SimpleNamespace(
            cwd=".",
            field_meta={
                "chatTemplateKwargs": {
                    "thinking": True,
                    "reasoningEffort": "high",
                }
            },
        )
    )

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="explain carefully")],
            field_meta={},
        )
    )

    assert service.started_request is not None
    assert service.started_request.options.thinking_enabled is True


@pytest.mark.asyncio
async def test_kernel_acp_explicit_thinking_option_overrides_chat_template_hint() -> None:
    service = _Service()
    agent = KernelACPAgent(_Connection(), service, system_prompt="system")
    await agent.newSession(
        SimpleNamespace(
            cwd=".",
            field_meta={"chatTemplateKwargs": {"thinking": True}},
        )
    )

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="answer briefly")],
            field_meta={"thinking_enabled": False},
        )
    )

    assert service.started_request is not None
    assert service.started_request.options.thinking_enabled is False


@pytest.mark.asyncio
async def test_kernel_acp_does_not_forward_provider_api_key_in_runtime_metadata() -> None:
    """Credentials supplied by a host must never enter durable run metadata."""

    service = _Service()
    agent = KernelACPAgent(_Connection(), service, system_prompt="system")
    await agent.newSession(
        SimpleNamespace(
            cwd=".",
            field_meta={
                "apiKey": "secret-token",
                "baseURL": "https://llm.example/v1",
                "chatTemplateKwargs": {"thinking": True},
            },
        )
    )

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="hello")],
            field_meta={},
        )
    )

    assert service.started_request is not None
    assert "apiKey" not in service.started_request.metadata
    assert "api_key" not in service.started_request.metadata
    assert service.started_request.metadata["baseURL"] == "https://llm.example/v1"


@pytest.mark.asyncio
async def test_kernel_acp_sanitizes_credentials_replayed_from_session_store() -> None:
    class PersistedService(_Service):
        async def load_session(self, request):
            return SessionInfo(
                "session-1",
                "now",
                {"apiKey": "old-secret", "workflow_id": "goal"},
            )

    agent = KernelACPAgent(_Connection(), PersistedService(), system_prompt="system")
    await agent.loadSession(
        SimpleNamespace(sessionId="session-1", cwd=".", field_meta={})
    )

    assert agent._sessions["session-1"]["metadata"]["workflow_id"] == "goal"
    assert agent._sessions["session-1"]["metadata"]["workspace_dir"] == str(
        Path(".").resolve()
    )
    assert "apiKey" not in agent._sessions["session-1"]["metadata"]


@pytest.mark.asyncio
async def test_kernel_acp_keeps_goal_prompt_on_native_kernel() -> None:
    conn = _Connection()
    service = _Service()
    agent = KernelACPAgent(conn, service)
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="/goal ship it")],
            field_meta={},
        )
    )

    assert response.stopReason == "end_turn"
    assert service.started_request is not None


@pytest.mark.asyncio
async def test_kernel_acp_keeps_native_plan_requests_on_kernel_path() -> None:
    conn = _Connection()
    service = _Service()
    agent = KernelACPAgent(conn, service)
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="请先制定计划")],
            field_meta={"requirePlanApproval": True},
        )
    )

    assert response.stopReason == "end_turn"
    assert service.started_request.options.workflow_options["require_plan_approval"] is True


@pytest.mark.asyncio
async def test_kernel_acp_plan_text_requests_native_pause_without_host_metadata() -> None:
    service = _Service()
    agent = KernelACPAgent(_Connection(), service)
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="请先制定计划")],
            field_meta={},
        )
    )

    options = service.started_request.options.workflow_options
    assert options["force_plan_start"] is True
    assert options["pause_after_plan_write"] is True
    assert options["plan_start_text"] == "请先制定计划"


@pytest.mark.asyncio
async def test_kernel_acp_maps_completion_gate_metadata_to_native_policy() -> None:
    service = _Service()
    agent = KernelACPAgent(
        _Connection(),
        service,
        native_workflow_ids={"completion_gate"},
    )
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="finish the report")],
            field_meta={
                "completionGate": {
                    "required_tools": ["verify"],
                    "max_continuations": 2,
                }
            },
        )
    )

    assert service.started_request.options.workflow_id == "completion_gate"
    assert service.started_request.options.workflow_options["completion_gate"] == {
        "required_tools": ["verify"],
        "max_continuations": 2,
    }


@pytest.mark.asyncio
async def test_kernel_acp_keeps_explicit_vendor_workflow_native() -> None:
    conn = _Connection()
    agent = KernelACPAgent(conn, _Service())
    await agent.newSession(
        SimpleNamespace(cwd=".", field_meta={"workflow_id": "vendor.research"})
    )

    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="research the vendor API")],
            field_meta={"workflow_id": "vendor.research"},
        )
    )

    assert response.stopReason == "end_turn"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workflow_id",
    ["controlled_presentation", "external_skill"],
)
async def test_kernel_acp_keeps_registered_builtin_workflow_native(
    workflow_id: str,
) -> None:
    """A built-in workflow registered by the Kernel host must not be promoted."""

    conn = _Connection()
    service = _Service()
    agent = KernelACPAgent(
        conn,
        service,
        native_workflow_ids={"controlled_presentation", "external_skill"},
    )
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="continue the workflow")],
            field_meta={"workflow_id": workflow_id},
        )
    )

    assert response.stopReason == "end_turn"
    assert service.started_request.options.workflow_id == workflow_id


@pytest.mark.asyncio
async def test_kernel_acp_uses_pluggable_workflow_selector_for_slash_skill() -> None:
    service = _Service()

    def select(prompt, metadata):
        assert prompt == "/vendor-skill make artifact"
        assert isinstance(metadata, dict)
        return {
            "workflow_id": "external_skill",
            "workflow_options": {
                "skill_name": "vendor-skill",
                "skill_source": "user",
            },
        }

    agent = KernelACPAgent(
        _Connection(),
        service,
        workflow_selector=select,
        native_workflow_ids={"external_skill"},
    )
    await agent.newSession(SimpleNamespace(cwd=".", field_meta={}))

    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[
                SimpleNamespace(type="text", text="/vendor-skill make artifact")
            ],
            field_meta={},
        )
    )

    assert response.stopReason == "end_turn"
    assert service.started_request.options.workflow_id == "external_skill"
    assert service.started_request.options.workflow_options["skill_name"] == (
        "vendor-skill"
    )


@pytest.mark.asyncio
async def test_kernel_acp_registered_selector_routes_freeform_presentation_native(
    tmp_path,
) -> None:
    from box_agent.acp.kernel_runtime import register_builtin_workflow_selectors
    from box_agent.plugins import PluginHost
    from box_agent.workflows import workflow_selector_from_registry

    host = PluginHost()
    register_builtin_workflow_selectors(
        host,
        workspace=tmp_path,
        skill_loader=None,
    )
    service = _Service()
    agent = KernelACPAgent(
        _Connection(),
        service,
        native_workflow_ids={"controlled_presentation"},
        workflow_selector=workflow_selector_from_registry(host),
    )
    await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))

    await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[
                SimpleNamespace(
                    type="text",
                    text="Create a six-slide investor presentation",
                )
            ],
            field_meta={},
        )
    )

    assert service.started_request.options.workflow_id == "controlled_presentation"
    assert service.started_request.options.workflow_options["research_mode"]


@pytest.mark.asyncio
async def test_kernel_acp_applies_session_workflow_default_to_native_prompt() -> None:
    """Session defaults must select a registered workflow on every turn."""

    conn = _Connection()
    service = _Service()
    agent = KernelACPAgent(
        conn,
        service,
        native_workflow_ids={"controlled_presentation"},
    )
    await agent.newSession(
        SimpleNamespace(
            cwd=".",
            field_meta={"workflow_id": "controlled_presentation"},
        )
    )

    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="continue the workflow")],
            field_meta={},
        )
    )

    assert response.stopReason == "end_turn"
    assert service.started_request.options.workflow_id == "controlled_presentation"


@pytest.mark.asyncio
async def test_kernel_acp_load_session_preserves_durable_workflow_default() -> None:
    """Reconnect must retain session metadata stored by the Service."""

    class DurableService(_Service):
        async def load_session(self, request):
            return SessionInfo(
                request.session_id or "",
                "now",
                {"workflow_id": "controlled_presentation"},
            )

    conn = _Connection()
    service = DurableService()
    agent = KernelACPAgent(
        conn,
        service,
        native_workflow_ids={"controlled_presentation"},
    )
    await agent.loadSession(
        SimpleNamespace(sessionId="session-1", cwd=".", field_meta={})
    )

    response = await agent.prompt(
        SimpleNamespace(
            sessionId="session-1",
            prompt=[SimpleNamespace(type="text", text="continue the workflow")],
            field_meta={},
        )
    )

    assert response.stopReason == "end_turn"
    assert service.started_request.options.workflow_id == "controlled_presentation"


@pytest.mark.asyncio
async def test_kernel_acp_presentation_pause_resumes_after_session_reload(
    tmp_path,
) -> None:
    from box_agent.api import Message, ModelChunk, ToolCallRequest
    from box_agent.persistence import SQLiteEventLog, SQLiteSessionStore
    from box_agent.plugins import PluginHost
    from box_agent.services.kernel import KernelAgentService
    from box_agent.tools.base import Tool, ToolResult
    from box_agent.workflows import ControlledPresentationPolicy

    class LLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            del request
            self.calls += 1
            if self.calls == 1:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id="presentation-decision",
                            tool_name="request_user_decision",
                            arguments={"question": "Choose layout"},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
                return
            yield ModelChunk(content="resumed presentation", finish_reason="stop")

    class DecisionTool(Tool):
        @property
        def name(self):
            return "request_user_decision"

        @property
        def description(self):
            return "Pause for a user decision."

        @property
        def parameters(self):
            return {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            }

        async def execute(self, *, question: str):
            return ToolResult(success=True, content=f"requested: {question}")

    host = PluginHost()
    llm = LLM()
    host.registries["llm"].register("default", llm, source="test")
    host.registries["tools"].register(
        "request_user_decision", DecisionTool(), source="test"
    )
    host.registries["workflows"].register(
        "controlled_presentation",
        ControlledPresentationPolicy(
            workspace_dir=str(tmp_path),
            artifact_root_dir=tmp_path / "output",
        ),
        source="test",
    )
    event_log = SQLiteEventLog(tmp_path / "events.sqlite3")
    session_store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    service = KernelAgentService.from_plugin_host(
        host,
        event_log=event_log,
        session_store=session_store,
    )
    first_agent = KernelACPAgent(
        _Connection(),
        service,
        native_workflow_ids={"controlled_presentation"},
    )
    created = await first_agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={"workflow_id": "controlled_presentation"},
        )
    )
    session_id = created.sessionId

    first = await first_agent.prompt(
        SimpleNamespace(
            sessionId=session_id,
            prompt=[SimpleNamespace(type="text", text="create slides")],
            field_meta={},
        )
    )

    assert first.stopReason == "end_turn"
    assert first.field_meta["controlledPresentation"]["lastStopReason"] == (
        "checkpoint_paused"
    )

    reloaded_agent = KernelACPAgent(
        _Connection(),
        service,
        native_workflow_ids={"controlled_presentation"},
    )
    await reloaded_agent.loadSession(
        SimpleNamespace(sessionId=session_id, cwd=str(tmp_path), field_meta={})
    )
    second = await reloaded_agent.prompt(
        SimpleNamespace(
            sessionId=session_id,
            prompt=[SimpleNamespace(type="text", text="use the first layout")],
            field_meta={},
        )
    )

    assert second.stopReason == "end_turn"
    assert llm.calls == 2
    events = await event_log.events_for_session(session_id)
    assert sum(event.type == "run.completed" for event in events) == 2
    event_log.shutdown()
    session_store.shutdown()


def test_kernel_runtime_registers_native_complex_workflow_policies(tmp_path) -> None:
    from box_agent.acp.kernel_runtime import register_builtin_workflow_policies
    from box_agent.plugins import PluginHost
    from box_agent.workflows import (
        CompletionGateWorkflowPolicy,
        ControlledPresentationPolicy,
        ExternalSkillRunPolicy,
    )

    host = PluginHost()
    skill_loader = object()

    registered = register_builtin_workflow_policies(
        host,
        workspace=tmp_path,
        tools=(),
        skill_loader=skill_loader,
    )

    assert registered == frozenset(
        {"completion_gate", "controlled_presentation", "external_skill", "skill"}
    )
    assert isinstance(
        host.registries["workflows"].resolve("completion_gate", scope="run"),
        CompletionGateWorkflowPolicy,
    )
    assert isinstance(
        host.registries["workflows"].resolve(
            "controlled_presentation", scope="run"
        ),
        ControlledPresentationPolicy,
    )
    assert isinstance(
        host.registries["workflows"].resolve("external_skill", scope="run"),
        ExternalSkillRunPolicy,
    )
    assert host.registries["workflows"].resolve(
        "skill", scope="run"
    ) is host.registries["workflows"].resolve("external_skill", scope="run")
    assert host.registries["workflows"].resolve(
        "controlled_presentation", scope="run"
    ).skill_loader is skill_loader
    assert host.registries["workflows"].resolve(
        "external_skill", scope="run"
    ).skill_loader is skill_loader


def test_kernel_runtime_registers_prompt_workflow_selectors(tmp_path) -> None:
    from box_agent.acp.kernel_runtime import register_builtin_workflow_selectors
    from box_agent.plugins import PluginHost
    from box_agent.workflows import (
        ExternalSkillWorkflowSelector,
        PresentationWorkflowSelector,
    )

    host = PluginHost()

    register_builtin_workflow_selectors(
        host,
        workspace=tmp_path,
        skill_loader=None,
    )

    selectors = host.registries["workflow.selectors"].resolve_all(scope="run")
    assert isinstance(selectors[0], PresentationWorkflowSelector)
    assert isinstance(selectors[1], ExternalSkillWorkflowSelector)


def test_kernel_runtime_registers_skill_catalog_context_contributor() -> None:
    from box_agent.acp.kernel_runtime import register_builtin_context_contributors
    from box_agent.context import ExpertContextContributor, SkillCatalogContextContributor
    from box_agent.plugins import PluginHost

    host = PluginHost()
    loader = object()

    register_builtin_context_contributors(host, skill_loader=loader)

    contributors = host.registries["context.contributors"].resolve_all(scope="run")
    assert any(
        isinstance(contributor, SkillCatalogContextContributor)
        for contributor in contributors
    )
    assert any(
        isinstance(contributor, ExpertContextContributor)
        for contributor in contributors
    )


def test_kernel_runtime_registers_builtin_host_extensions() -> None:
    from box_agent.acp.kernel_runtime import register_builtin_host_extensions
    from box_agent.plugins import PluginHost

    host = PluginHost()

    class Utility:
        async def prompt(self, params):
            return {"text": params.get("prompt", "")}

    class MemoryProposals:
        async def list(self, params):
            return {"candidates": []}

        async def apply(self, params):
            return {"skipped": 1, "core": ""}

    register_builtin_host_extensions(
        host,
        skill_loader=None,
        utility_prompt_service=Utility(),
        memory_proposal_service=MemoryProposals(),
    )

    assert {
        registration.key
        for registration in host.registries["host.extensions"].registrations()
    } == {
        "list_skills",
        "workspace/list",
        "workspace/get",
        "workspace/set",
        "mcp/status",
        "mcp/reconnect",
        "mcp/disconnect",
        "llm/prompt",
        "presentation/preflight",
        "memory_proposal_list",
        "memory_proposal_apply",
    }


@pytest.mark.asyncio
async def test_kernel_acp_routes_utility_prompt_without_legacy_agent() -> None:
    from box_agent.acp.kernel_runtime import register_builtin_host_extensions
    from box_agent.adapters.extensions import HostExtensionRouter
    from box_agent.plugins import PluginHost

    class Utility:
        async def prompt(self, params):
            return {"text": f"utility:{params['prompt']}"}

    host = PluginHost()
    register_builtin_host_extensions(
        host,
        skill_loader=None,
        utility_prompt_service=Utility(),
    )
    agent = KernelACPAgent(
        _Connection(),
        _Service(),
        extension_router=HostExtensionRouter(host.registries["host.extensions"]),
    )

    result = await agent.extMethod("llm/prompt", {"prompt": "title"})

    assert result == {"text": "utility:title"}


@pytest.mark.asyncio
async def test_kernel_acp_routes_memory_proposals_without_legacy_agent() -> None:
    from box_agent.acp.kernel_runtime import register_builtin_host_extensions
    from box_agent.adapters.extensions import HostExtensionRouter
    from box_agent.plugins import PluginHost

    class MemoryProposals:
        async def list(self, params):
            return {"candidates": [{"id": "memory-1"}]}

        async def apply(self, params):
            return {"pinned": 1, "core": "stable"}

    host = PluginHost()
    register_builtin_host_extensions(
        host,
        skill_loader=None,
        memory_proposal_service=MemoryProposals(),
    )
    agent = KernelACPAgent(
        _Connection(),
        _Service(),
        extension_router=HostExtensionRouter(host.registries["host.extensions"]),
    )

    listed = await agent.extMethod("memory_proposal_list", {"sessionId": ""})
    applied = await agent.extMethod(
        "memory_proposal_apply",
        {"sessionId": "", "decisions": {"memory-1": "pin"}},
    )

    assert listed == {"candidates": [{"id": "memory-1"}]}
    assert applied == {"pinned": 1, "core": "stable"}


@pytest.mark.asyncio
async def test_mcp_host_extensions_replace_kernel_tool_generation(monkeypatch) -> None:
    from box_agent.acp.kernel_runtime import register_builtin_host_extensions
    from box_agent.adapters import build_plugin_host
    from box_agent.adapters.extensions import HostExtensionContext, HostExtensionRouter

    class LLM:
        pass

    class MCPTool:
        aliases = ()

        def __init__(self, name: str, server_name: str) -> None:
            self.name = name
            self.server_name = server_name

    old_tool = MCPTool("search", "demo")
    new_tool = MCPTool("search_v2", "demo")
    host = build_plugin_host(llm=LLM(), tools=(old_tool,))
    register_builtin_host_extensions(host, skill_loader=None)
    reconnect_calls: list[str] = []
    disconnect_calls: list[str] = []

    async def reconnect(name: str):
        reconnect_calls.append(name)
        return {"success": True, "toolCount": 1, "tools": [new_tool.name]}

    async def disconnect(name: str):
        disconnect_calls.append(name)
        return {"success": True, "removedTools": [new_tool.name]}

    monkeypatch.setattr("box_agent.tools.mcp_loader.reconnect_mcp_server", reconnect)
    monkeypatch.setattr("box_agent.tools.mcp_loader.disconnect_mcp_server", disconnect)
    monkeypatch.setattr(
        "box_agent.tools.mcp_loader.get_mcp_tools_for_server",
        lambda name: [new_tool] if name == "demo" else [],
    )
    router = HostExtensionRouter(host.registries["host.extensions"])
    context = HostExtensionContext(service=type("Service", (), {"active_run_ids": lambda self: ()})())

    result = await router.handle("mcp/reconnect", {"name": "demo"}, context=context)

    assert result["success"] is True
    assert reconnect_calls == ["demo"]
    registry = host.registries["tools.executors"]
    assert registry.resolve("search", scope="run") is None
    assert registry.resolve("search_v2", scope="run") is new_tool

    result = await router.handle("mcp/disconnect", {"name": "demo"}, context=context)

    assert result["success"] is True
    assert disconnect_calls == ["demo"]
    assert registry.resolve("search_v2", scope="run") is None


@pytest.mark.asyncio
async def test_mcp_host_extension_rejects_graph_change_during_active_run(
    monkeypatch,
) -> None:
    from box_agent.acp.kernel_runtime import register_builtin_host_extensions
    from box_agent.adapters.extensions import HostExtensionContext, HostExtensionRouter
    from box_agent.plugins import PluginHost

    host = PluginHost()
    register_builtin_host_extensions(host, skill_loader=None)
    called = False

    async def reconnect(name: str):
        nonlocal called
        called = True
        return {"success": True}

    monkeypatch.setattr("box_agent.tools.mcp_loader.reconnect_mcp_server", reconnect)
    context = HostExtensionContext(
        service=type("Service", (), {"active_run_ids": lambda self: ("run-1",)})()
    )

    result = await HostExtensionRouter(host.registries["host.extensions"]).handle(
        "mcp/reconnect", {"name": "demo"}, context=context
    )

    assert result == {
        "success": False,
        "error": "agent runs are active; retry after they reach a terminal state",
        "activeRunIds": ["run-1"],
    }
    assert called is False


def test_kernel_runtime_registers_explicit_native_goal_plan_aliases() -> None:
    from box_agent.acp.kernel_runtime import register_native_state_workflow_aliases
    from box_agent.plugins import PluginHost

    host = PluginHost()
    goal = object()
    plan = object()

    registered = register_native_state_workflow_aliases(
        host,
        goal_policy=goal,
        plan_policy=plan,
    )

    assert registered == frozenset({"goal", "autopilot", "plan"})
    assert host.registries["workflows"].resolve("goal", scope="run") is goal
    assert host.registries["workflows"].resolve("autopilot", scope="run") is goal
    assert host.registries["workflows"].resolve("plan", scope="run") is plan


@pytest.mark.asyncio
async def test_kernel_acp_forwards_connection_scoped_extension_without_session() -> None:
    agent = KernelACPAgent(_Connection(), _Service())

    result = await agent.extMethod("workspace/list", {})

    assert result == {"error": "unsupported_extension"}


@pytest.mark.asyncio
async def test_kernel_acp_dispatches_registered_host_extension_without_legacy() -> None:
    from box_agent.adapters.extensions import HostExtensionRouter
    from box_agent.plugins import PluginHost

    class Handler:
        async def handle(self, params, context):
            assert params == {"value": 3}
            assert context.session_id == ""
            return {"result": 6}

    host = PluginHost()
    host.registries["host.extensions"].register(
        "vendor/double", Handler(), source="vendor"
    )
    agent = KernelACPAgent(
        _Connection(),
        _Service(),
        extension_router=HostExtensionRouter(host.registries["host.extensions"]),
    )

    result = await agent.extMethod("vendor/double", {"value": 3})

    assert result == {"result": 6}


@pytest.mark.asyncio
async def test_kernel_acp_maps_inject_extension_to_run_control() -> None:
    from box_agent.api import CommandAck

    class ActiveHandle:
        run_id = "run-active"

        def __init__(self) -> None:
            self.commands = []

        async def send(self, command):
            self.commands.append(command)
            return CommandAck(command.command_id, True, "accepted")

    handle = ActiveHandle()
    agent = KernelACPAgent(_Connection(), _Service())
    agent._sessions["session-1"] = {"cwd": ".", "metadata": {}}
    agent._active["session-1"] = handle

    result = await agent.extMethod(
        "inject",
        {
            "sessionId": "session-1",
            "text": "Use the approved source",
            "injectionId": "inject-1",
            "operationId": "operation-1",
        },
    )

    assert result == {
        "ok": True,
        "injectionId": "inject-1",
        "operationId": "operation-1",
    }
    assert handle.commands[0].command_id == "acp:operation-1"
    assert handle.commands[0].kind == "run.inject"
    assert handle.commands[0].payload == {
        "text": "Use the approved source",
        "injection_id": "inject-1",
    }


@pytest.mark.asyncio
async def test_kernel_acp_uses_operation_identity_independent_of_injection_identity() -> None:
    """Cancel/reinject is legal; only a repeated operation is idempotent."""

    from box_agent.api import CommandAck

    class ActiveHandle:
        run_id = "run-active"

        def __init__(self) -> None:
            self.commands = []

        async def send(self, command):
            self.commands.append(command)
            return CommandAck(command.command_id, True, "accepted")

    handle = ActiveHandle()
    agent = KernelACPAgent(_Connection(), _Service())
    agent._sessions["session-1"] = {"cwd": ".", "metadata": {}}
    agent._active["session-1"] = handle

    for method, operation_id in (
        ("inject", "operation-add-1"),
        ("cancel_inject", "operation-remove-1"),
        ("inject", "operation-add-2"),
    ):
        params = {
            "sessionId": "session-1",
            "injectionId": "logical-note-1",
            "operationId": operation_id,
        }
        if method == "inject":
            params["text"] = f"content from {operation_id}"
        result = await agent.extMethod(method, params)
        assert result["operationId"] == operation_id

    assert [command.command_id for command in handle.commands] == [
        "acp:operation-add-1",
        "acp:operation-remove-1",
        "acp:operation-add-2",
    ]
    assert [command.payload["injection_id"] for command in handle.commands] == [
        "logical-note-1",
        "logical-note-1",
        "logical-note-1",
    ]


def test_acp_entrypoint_runs_the_only_runtime(monkeypatch) -> None:
    import box_agent.acp as acp_module

    seen = {}

    async def fake_run_acp_server():
        seen["called"] = True

    monkeypatch.setattr(acp_module, "run_acp_server", fake_run_acp_server)
    monkeypatch.setattr(sys, "argv", ["box-agent-acp"])
    acp_module.main()

    assert seen["called"] is True


def test_acp_server_dispatches_protocol_bootstrap_without_legacy_setup(monkeypatch) -> None:
    import box_agent.acp as acp_module
    import box_agent.acp.bootstrap as bootstrap

    seen: dict[str, object] = {}

    async def fake_bootstrap(config):
        seen["config"] = config

    monkeypatch.setattr(bootstrap, "run_acp_bootstrap", fake_bootstrap)
    marker = object()

    asyncio.run(acp_module.run_acp_server(marker))

    assert seen["config"] is marker


def test_acp_server_defers_default_config_loading_to_bootstrap(monkeypatch) -> None:
    import box_agent.acp as acp_module
    import box_agent.acp.bootstrap as bootstrap

    seen: dict[str, object] = {}

    async def fake_bootstrap(config):
        seen["config"] = config

    monkeypatch.setattr(bootstrap, "run_acp_bootstrap", fake_bootstrap)
    monkeypatch.delenv("BOX_AGENT_RUNTIME", raising=False)

    asyncio.run(acp_module.run_acp_server())

    assert seen["config"] is None
