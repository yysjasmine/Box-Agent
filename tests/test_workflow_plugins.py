from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from box_agent.api import (
    ControlCommand,
    Message,
    ModelChunk,
    RunOptions,
    SessionOpenRequest,
    RunRequest,
    ToolCallRequest,
    ToolCallResult,
    WorkflowContinuation,
)
from box_agent.kernel import AgentLoopKernel
from box_agent.kernel import PluginKernelComposer
from box_agent.persistence import SQLiteEventLog
from box_agent.services.kernel import KernelAgentService
from box_agent.plugins import PluginHost, TypedRegistry
from box_agent.tools.engine import RegistryToolEngine
from box_agent.workflows import (
    CompositeWorkflowPolicy,
    CompletionGateWorkflowPolicy,
    ControlledPresentationPolicy,
    GoalStore,
    GoalWorkflowPolicy,
    ExternalSkillRunPolicy,
    PlanWorkflowPolicy,
    SessionPlanStore,
    WorkflowEventHook,
    apply_goal_action,
    build_goal_tools,
    build_plan_tools,
    goal_autopilot_progress_signature,
    goal_autopilot_prompt,
    goal_autopilot_terminal_message,
    should_continue_goal_autopilot,
    text_requests_native_plan,
)
from box_agent.loop_guards import CompletionGate


def test_goal_autopilot_terminal_message_uses_no_progress_turn_count() -> None:
    message = goal_autopilot_terminal_message(
        enabled=True,
        stop_cause="no_progress",
        continuations=3,
        no_progress_turns=2,
    )

    assert "after 2 continuation(s) without recorded goal progress" in message
    assert "after 3 continuation(s) without recorded goal progress" not in message


def test_goal_control_actions_share_one_host_neutral_contract() -> None:
    store = GoalStore()

    created = apply_goal_action(
        store,
        "session-a",
        {
            "action": "set",
            "objective": "Ship the native Goal flow",
            "progress": ["implemented"],
        },
    )
    assert created["ok"] is True
    assert created["goal"]["status"] == "active"

    blocked = apply_goal_action(
        store,
        "session-a",
        {
            "action": "block",
            "blockedReason": "waiting for review",
            "evidence": ["tests passed"],
        },
    )
    assert blocked["goal"]["status"] == "blocked"
    assert blocked["goal"]["blockedReason"] == "waiting for review"

    resumed = apply_goal_action(store, "session-a", {"action": "resume"})
    assert resumed["goal"]["blockedReason"] is None

    completed = apply_goal_action(
        store,
        "session-a",
        {"action": "complete", "completedBy": "acp-host"},
    )
    assert completed["goal"]["status"] == "complete"
    assert completed["goal"]["completedBy"] == "acp-host"

    assert apply_goal_action(
        GoalStore(), "missing", {"action": "pause"}
    ) == {"error": "goal_not_found"}
    assert apply_goal_action(
        store, "session-a", {"action": "unknown"}
    ) == {"error": "unknown_action: unknown"}


async def _execute(
    engine: RegistryToolEngine,
    call_id: str,
    tool_name: str,
    arguments: dict,
    session_id: str,
):
    return await engine.execute(
        ToolCallRequest(
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            metadata={"session_id": session_id, "run_id": f"run-{session_id}"},
        )
    )


@pytest.mark.asyncio
async def test_goal_tools_are_isolated_by_runtime_session() -> None:
    store = GoalStore()
    registry = TypedRegistry("tools")
    for tool in build_goal_tools(store):
        registry.register(tool.name, tool, source="test")
    engine = RegistryToolEngine(registry)

    first = await _execute(
        engine,
        "set-a",
        "goal_write",
        {"action": "set", "objective": "Ship A"},
        "session-a",
    )
    second = await _execute(
        engine,
        "set-b",
        "goal_write",
        {"action": "set", "objective": "Ship B"},
        "session-b",
    )
    assert first.status == second.status == "succeeded"

    read_a = await _execute(engine, "read-a", "goal_read", {}, "session-a")
    read_b = await _execute(engine, "read-b", "goal_read", {}, "session-b")
    assert read_a.output["goal"]["objective"] == "Ship A"
    assert read_b.output["goal"]["objective"] == "Ship B"

    missing_evidence = await _execute(
        engine,
        "complete-a",
        "goal_write",
        {"action": "complete"},
        "session-a",
    )
    assert missing_evidence.status == "failed"
    assert "evidence" in missing_evidence.error.message


def test_goal_and_plan_policies_compose_and_bind_to_run_session() -> None:
    goals = GoalStore()
    plans = SessionPlanStore()
    goals.set("session-a", "Ship A", progress=["implemented"])
    plans.set("session-a", title="Release A", steps=[{"title": "Verify"}])

    policy = CompositeWorkflowPolicy(
        [GoalWorkflowPolicy(goals), PlanWorkflowPolicy(plans)]
    )
    bound = policy.for_run(type("Request", (), {"session_id": "session-a"})())
    checkpoint = bound.build_checkpoint()
    assert checkpoint is not None
    assert "[goal]" in checkpoint
    assert "Ship A" in checkpoint
    assert "[plan]" in checkpoint
    assert "Release A" in checkpoint
    payload = bound.build_checkpoint_payload()
    assert payload["goal"]["goal"]["objective"] == "Ship A"
    assert payload["plan"]["plan"]["title"] == "Release A"

    other = policy.for_run(type("Request", (), {"session_id": "session-b"})())
    assert other.build_checkpoint() is None


def test_composite_policy_injects_checkpoint_for_policy_without_context_items() -> None:
    class CheckpointOnlyPolicy:
        kind = "external_skill"

        def build_checkpoint(self):
            return "Resume the pending Skill delivery."

    items = CompositeWorkflowPolicy([CheckpointOnlyPolicy()]).context_items(
        {"session_id": "session-a", "step": 1}
    )

    assert len(items) == 1
    assert items[0].item_id == "workflow:external_skill"
    assert items[0].content == "Resume the pending Skill delivery."
    assert items[0].metadata == {
        "role": "system",
        "workflow_context": True,
        "workflow": "external_skill",
    }


def test_composite_policy_composes_catalog_and_visible_result_hooks() -> None:
    class Policy:
        kind = "catalog"
        restrict_tools_until_required_succeed = True
        max_tool_calls = 7

        def hidden_tool_names(self):
            return {"hidden"}

        def filter_tool_schemas(self, schemas):
            return tuple(schema for schema in schemas if schema.get("name") != "filtered")

        def required_tool_names(self):
            return {"required"}

        def begin_tool_decision(self, decision_id):
            self.decision_id = decision_id

        def record_visible_tool_result(self, tool_name, arguments, result, content):
            self.visible = (tool_name, arguments, result.success, content)

        def evidence_urls(self, tool_name, arguments, result):
            return ("https://example.test/source",)

    policy = Policy()
    composite = CompositeWorkflowPolicy([policy])
    schemas = (
        {"name": "required"},
        {"name": "filtered"},
        {"name": "hidden"},
    )
    assert composite.hidden_tool_names() == frozenset({"hidden"})
    assert composite.filter_tool_schemas(schemas) == (
        {"name": "required"},
        {"name": "hidden"},
    )
    assert composite.required_tool_names() == frozenset({"required"})
    assert composite.restrict_tools_until_required_succeed is True
    assert composite.max_tool_calls == 7
    composite.begin_tool_decision(3)
    assert policy.decision_id == 3
    result = ToolCallResult(
        call_id="plan-required",
        status="succeeded",
        output={"type": "plan_snapshot", "plan": {"id": "plan-1"}},
    )
    composite.record_visible_tool_result("required", {}, result, "ok")
    assert policy.visible == ("required", {}, True, "ok")
    assert composite.evidence_urls("required", {}, result) == (
        "https://example.test/source",
    )


def test_composite_policy_keeps_legacy_preflight_signatures_compatible() -> None:
    class LegacyPolicy:
        def tool_call_error(self, tool_name, arguments, *, verified_evidence_urls):
            del tool_name, arguments, verified_evidence_urls
            return "blocked"

    composite = CompositeWorkflowPolicy([LegacyPolicy()])
    assert (
        composite.tool_call_error(
            "write_file",
            {},
            verified_evidence_urls=set(),
            parallel=True,
        )
        == "blocked"
    )


def test_composite_policy_adapts_tool_result_for_legacy_evidence_hooks() -> None:
    """Evidence hooks must see the legacy success/content view as well."""

    class LegacyEvidencePolicy:
        def evidence_urls(self, tool_name, arguments, result):
            assert tool_name == "web_fetch"
            assert arguments == {"url": "https://example.test"}
            assert result.success is True
            assert result.content == "source text"
            return ("https://example.test",)

    composite = CompositeWorkflowPolicy([LegacyEvidencePolicy()])
    result = ToolCallResult(
        call_id="fetch-1",
        status="succeeded",
        content="source text",
    )

    assert composite.evidence_urls(
        "web_fetch",
        {"url": "https://example.test"},
        result,
    ) == ("https://example.test",)


def test_composite_policy_returns_direct_evidence_url() -> None:
    class DirectEvidencePolicy:
        def direct_evidence_url(self, tool_name, arguments, result):
            assert tool_name == "read_url"
            assert arguments == {"url": "https://example.test/direct"}
            assert result.success is True
            return arguments["url"]

    composite = CompositeWorkflowPolicy([DirectEvidencePolicy()])
    result = ToolCallResult(
        call_id="direct-1",
        status="succeeded",
        content="verified",
    )

    assert composite.direct_evidence_url(
        "read_url",
        {"url": "https://example.test/direct"},
        result,
    ) == "https://example.test/direct"


def test_composite_policy_forwards_terminal_boundary_message() -> None:
    class BoundaryPolicy:
        def terminal_message(self, stop_reason, final_content):
            assert stop_reason == "end_turn"
            assert final_content == "done"
            return "recoverable pause"

    assert (
        CompositeWorkflowPolicy([BoundaryPolicy()]).terminal_message(
            "end_turn", "done"
        )
        == "recoverable pause"
    )


def test_plan_plugin_preserves_legacy_tool_schemas_and_snapshot_shape() -> None:
    store = SessionPlanStore()
    write_tool, read_tool = build_plan_tools(store)
    assert write_tool.name == "plan_write"
    assert read_tool.name == "plan_read"
    assert write_tool.parameters["required"] == ["action"]
    assert read_tool.parameters == {"type": "object", "properties": {}}
    restored = store.restore(
        "session-a",
        {
            "id": "persisted-1",
            "title": "Recovered",
            "objective": "Resume",
            "steps": [{"title": "Continue"}],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:01:00",
        },
    )
    assert restored is not None
    assert restored["id"] == "persisted-1"
    assert store.get("session-a")["created_at"] == "2026-01-01T00:00:00"


def test_plan_policy_enforces_approval_at_workflow_preflight() -> None:
    store = SessionPlanStore()
    store.set("session-a", title="Release", steps=[{"title": "Verify"}])
    policy = PlanWorkflowPolicy(
        store,
        require_plan_approval=True,
    )
    request = RunRequest(
        request_id="request-plan-pending",
        session_id="session-a",
        turn_id="turn-1",
        user_input=Message.user("prepare"),
        options=RunOptions(workflow_options={"require_plan_approval": True}),
    )
    policy = policy.for_run(request)

    blocked = policy.plan_scope_error("bash", {})
    assert blocked is not None and "approval" in blocked.lower()
    assert policy.plan_scope_error("plan_read", {}) is None
    assert policy.context_items({})[0].metadata["approval_request_id"]
    assert policy.build_checkpoint_payload()["plan"]["approval"]["state"] == "pending"

    approved_request = RunRequest(
        request_id="request-plan-approved",
        session_id="session-a",
        turn_id="turn-2",
        user_input=Message.user("execute"),
        options=RunOptions(
            workflow_options={
                "require_plan_approval": True,
                "plan_approval": {"decision": "approved"},
            }
        ),
    )
    approved = policy.for_run(approved_request)
    assert approved.plan_scope_error("bash", {}) is None
    assert approved.context_items({}) == ()


def test_plan_policy_accepts_scoped_operator_approval_control() -> None:
    store = SessionPlanStore()
    policy = PlanWorkflowPolicy(
        store,
        session_id="session-a",
        require_plan_approval=True,
    ).for_run(
        RunRequest(
            request_id="request-plan-control",
            session_id="session-a",
            turn_id="turn-1",
            user_input=Message.user("prepare"),
            options=RunOptions(workflow_options={"require_plan_approval": True}),
        )
    )
    request_id = policy.build_checkpoint_payload()["plan"]["approval"]["request_id"]
    assert policy.handle_control(
        ControlCommand(
            command_id="control-1",
            session_id="session-a",
            run_id="run-1",
            kind="workflow.plan.approve",
            payload={"request_id": request_id, "decision": "approved"},
            source="test",
        )
    )["state"] == "approved"
    assert policy.plan_scope_error("bash", {}) is None
    assert policy.handle_control(
        ControlCommand(
            command_id="control-2",
            session_id="session-a",
            run_id="run-1",
            kind="workflow.plan.approve",
            payload={"request_id": "other", "decision": "approved"},
            source="test",
        )
    ) is None


def test_plan_approval_state_is_restored_from_checkpoint_payload() -> None:
    store = SessionPlanStore()
    initial = PlanWorkflowPolicy(
        store,
        session_id="session-a",
        require_plan_approval=True,
    ).for_run(
        RunRequest(
            request_id="request-plan-recovery",
            session_id="session-a",
            turn_id="turn-1",
            user_input=Message.user("prepare"),
            options=RunOptions(workflow_options={"require_plan_approval": True}),
        )
    )
    request_id = initial.build_checkpoint_payload()["plan"]["approval"]["request_id"]
    initial.handle_control(
        ControlCommand(
            command_id="control-recovery",
            session_id="session-a",
            run_id="run-1",
            kind="workflow.plan.approve",
            payload={"request_id": request_id, "decision": "approved"},
            source="test",
        )
    )
    checkpoint = {
        "type": "workflow.checkpoint",
        "payload": {"workflow_state": initial.build_checkpoint_payload()},
    }
    restored = PlanWorkflowPolicy(
        store,
        session_id="session-a",
        require_plan_approval=True,
    ).for_run(
        RunRequest(
            request_id="request-plan-recovery",
            session_id="session-a",
            turn_id="turn-1",
            user_input=Message.user("resume"),
            options=RunOptions(workflow_options={"require_plan_approval": True}),
        ),
        SimpleNamespace(events=(checkpoint,)),
    )
    assert restored.context_items({}) == ()


def test_plan_policy_emits_native_start_snapshot_for_explicit_request() -> None:
    policy = PlanWorkflowPolicy(
        SessionPlanStore(),
        session_id="session-plan-start",
    ).for_run(
        RunRequest(
            request_id="request-plan-start",
            session_id="session-plan-start",
            turn_id="turn-1",
            user_input=Message.user("先做一个计划再执行"),
        )
    )

    events = policy.initial_events(
        {
            "tool_names": ("plan_write",),
            "latest_user_text": "先做一个计划再执行",
            "recovery_events": (),
        }
    )

    assert len(events) == 1
    assert events[0]["type"] == "workflow.plan.snapshot"
    assert events[0]["payload"]["type"] == "plan_snapshot"
    assert events[0]["payload"]["action"] == "start"
    assert events[0]["payload"]["plan"]["status"] == "draft"


def test_plan_policy_does_not_repeat_start_snapshot_after_recovery() -> None:
    policy = PlanWorkflowPolicy(
        SessionPlanStore(),
        session_id="session-plan-recovery",
    ).for_run(
        RunRequest(
            request_id="request-plan-recovery",
            session_id="session-plan-recovery",
            turn_id="turn-2",
            user_input=Message.user("继续按计划执行"),
        )
    )

    events = policy.initial_events(
        {
            "tool_names": ("plan_write",),
            "latest_user_text": "请先做一个计划",
            "recovery_events": (
                {
                    "type": "workflow.plan.snapshot",
                    "payload": {"type": "plan_snapshot", "action": "start"},
                },
            ),
        }
    )

    assert events == ()


def test_plan_policy_restores_draft_from_replayed_start_snapshot() -> None:
    snapshot = {
        "type": "workflow.plan.snapshot",
        "session_id": "session-plan-snapshot",
        "payload": {
            "type": "plan_snapshot",
            "action": "start",
            "plan": {"id": "pending", "title": "Preparing", "status": "draft"},
        },
    }
    request = RunRequest(
        request_id="request-plan-snapshot",
        session_id="session-plan-snapshot",
        turn_id="turn-2",
        user_input=Message.user("继续"),
    )

    restored = PlanWorkflowPolicy(SessionPlanStore()).for_run(
        request,
        SimpleNamespace(events=(snapshot,)),
    )

    plan = restored.store.get("session-plan-snapshot")
    assert plan is not None
    assert plan["id"] == "pending"
    assert plan["title"] == "Preparing"
    assert plan["status"] == "draft"


def test_plan_policy_checkpoints_forced_guidance_before_plan_exists() -> None:
    policy = PlanWorkflowPolicy(
        SessionPlanStore(),
        session_id="session-plan-guidance-checkpoint",
        force_plan_start=True,
    )

    policy.context_items({"phase": "before_model"})

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    assert "plan_write" in checkpoint
    assert policy.build_checkpoint_payload()["plan"]["force_guidance_emitted"] is True


def test_plan_policy_on_event_rehydrates_forced_guidance_state() -> None:
    policy = PlanWorkflowPolicy(
        SessionPlanStore(),
        session_id="session-plan-guidance-replay",
        force_plan_start=True,
    )

    policy.on_event(
        {
            "type": "workflow.checkpoint",
            "session_id": "session-plan-guidance-replay",
            "payload": {
                "workflow_state": {
                    "plan": {
                        "plan": None,
                        "force_guidance_emitted": True,
                    }
                }
            },
        }
    )

    items = policy.context_items({})
    assert len(items) == 1
    assert "still waiting for the structured plan card" in items[0].content


@pytest.mark.asyncio
async def test_service_persists_force_plan_guidance_checkpoint(tmp_path) -> None:
    class LLM:
        async def stream(self, request):
            del request
            yield ModelChunk(content="先说明，再补计划", finish_reason="stop")

    host = PluginHost()
    host.registries["llm"].register("default", LLM(), source="test")
    host.registries["workflows"].register(
        "default",
        PlanWorkflowPolicy(SessionPlanStore(), force_plan_start=True),
        source="test",
    )
    for tool in build_plan_tools(SessionPlanStore()):
        host.registries["tools"].register(tool.name, tool, source="test")

    event_log = SQLiteEventLog(tmp_path / "plan-guidance.sqlite3")
    service = KernelAgentService.from_plugin_host(host, event_log=event_log)
    session_id = "plan-guidance-checkpoint-session"
    await service.open_session(SessionOpenRequest(session_id=session_id))
    handle = await service.start(
        RunRequest(
            request_id="plan-guidance-checkpoint-run",
            session_id=session_id,
            turn_id="turn-1",
            user_input=Message.user("完成这个任务"),
            options=RunOptions(workflow_options={"force_plan_start": True}),
        )
    )
    result = await handle.wait()
    bundle = await event_log.load_recovery_bundle(handle.run_id)

    assert result.status == "completed"
    assert bundle.checkpoint is not None
    workflow_state = bundle.checkpoint.state["workflow_state"]
    assert workflow_state["plan"]["plan"] is None
    assert workflow_state["plan"]["force_guidance_emitted"] is True


def test_plan_policy_decorates_native_result_with_pending_approval() -> None:
    policy = PlanWorkflowPolicy(
        SessionPlanStore(),
        session_id="session-plan-approval",
        require_plan_approval=True,
    ).for_run(
        RunRequest(
            request_id="request-plan-approval-output",
            session_id="session-plan-approval",
            turn_id="turn-1",
            user_input=Message.user("准备执行"),
            options=RunOptions(
                workflow_options={"require_plan_approval": True}
            ),
        )
    )

    from box_agent.api import ToolCallResult

    result = ToolCallResult(
        call_id="plan-call",
        status="succeeded",
        output={
            "type": "plan_snapshot",
            "version": 1,
            "action": "set",
            "plan": {"id": "p-1", "title": "Release"},
        },
    )
    output = policy.result_output("plan_write", {}, result)

    assert output is not None
    assert output["approval"]["state"] == "pending"
    assert output["approval"]["request_id"]


def test_plan_policy_requests_terminal_pause_after_plan_write() -> None:
    policy = PlanWorkflowPolicy(
        SessionPlanStore(),
        session_id="session-plan-pause",
        pause_after_plan_write=True,
    )

    from box_agent.api import ToolCallResult

    result = ToolCallResult(call_id="plan-call", status="succeeded")

    assert policy.pause_after_tool("plan_write", result) == (
        "计划已生成，等待用户确认后再执行。"
    )


def test_plan_policy_pauses_for_required_approval_without_extra_option() -> None:
    policy = PlanWorkflowPolicy(
        SessionPlanStore(),
        require_plan_approval=True,
    )
    result = SimpleNamespace(success=True)
    policy.record_tool_result("plan_write", {}, result)

    assert policy.pause_after_tool("plan_write", result) == (
        "计划已生成，等待用户确认后再执行。"
    )


def test_goal_autopilot_terminal_metadata_reports_no_progress_guard() -> None:
    goals = GoalStore()
    goals.set("session-a", "Ship native")
    policy = GoalWorkflowPolicy(
        goals,
        session_id="session-a",
        autopilot_enabled=True,
        max_continuations=3,
        no_progress_turns=1,
    )

    assert policy.next_continuation(
        stop_reason="end_turn", final_content="same", step=1
    ) is not None
    assert policy.next_continuation(
        stop_reason="end_turn", final_content="same", step=2
    ) is None
    metadata = policy.terminal_metadata("end_turn", "same")

    assert metadata["goalAutopilot"]["noProgressExhausted"] is True
    assert metadata["goalAutopilot"]["noProgressTurns"] == 1
    assert metadata["goalAutopilot"]["stopCause"] == "no_progress"


def test_goal_terminal_metadata_includes_detached_goal_snapshot() -> None:
    goals = GoalStore()
    goals.set("session-a", "Ship native", progress=["implemented"])
    policy = GoalWorkflowPolicy(goals, session_id="session-a")

    metadata = policy.terminal_metadata("end_turn", "done")

    assert metadata["goal"]["objective"] == "Ship native"
    assert metadata["goal"]["progress"] == ["implemented"]
    # The terminal contract must not expose a mutable GoalState instance.
    goals.progress("session-a", ["verified"])
    assert metadata["goal"]["progress"] == ["implemented"]


def test_completion_gate_policy_continues_only_for_unmet_evidence() -> None:
    policy = CompletionGateWorkflowPolicy(
        CompletionGate(
            required_tools=frozenset({"verify"}),
            max_continuations=2,
            max_tool_calls=4,
        )
    )
    assert policy.max_tool_calls == 4
    first = policy.next_continuation(
        stop_reason="end_turn", final_content="done", step=1
    )
    assert first is not None
    assert first.reason == "completion_gate"
    assert "verify" in str(first.message.content)
    assert policy.next_continuation(
        stop_reason="end_turn", final_content="still done", step=2
    ) is not None

    class Result:
        success = True
        content = "verified"

    policy.record_tool_result("verify", {}, Result())
    assert policy.next_continuation(
        stop_reason="end_turn", final_content="done", step=3
    ) is None


def test_completion_gate_policy_binds_host_data_options_per_run() -> None:
    prototype = CompletionGateWorkflowPolicy(CompletionGate())
    bound = prototype.for_run(
        RunRequest(
            request_id="completion-options",
            session_id="session-a",
            turn_id="turn-a",
            user_input=Message.user("finish"),
            options=RunOptions(
                workflow_id="completion_gate",
                workflow_options={
                    "completion_gate": {
                        "required_tools": ["verify"],
                        "required_artifacts": ["output/report.md"],
                        "max_continuations": 2,
                        "max_tool_calls": 5,
                        "budget_exempt_tools": ["goal_write"],
                    }
                },
            ),
        )
    )

    assert bound.gate.required_tools == frozenset({"verify"})
    assert bound.gate.required_artifacts == ("output/report.md",)
    assert bound.gate.max_continuations == 2
    assert bound.max_tool_calls == 5
    assert bound.gate.budget_exempt_tools == frozenset({"goal_write"})


def test_completion_gate_treats_openai_stop_as_natural_end() -> None:
    policy = CompletionGateWorkflowPolicy(
        CompletionGate(required_tools=frozenset({"verify"}), max_continuations=1)
    )

    continuation = policy.next_continuation(
        stop_reason="stop", final_content="done", step=1
    )

    assert continuation is not None


def test_completion_gate_terminal_metadata_reports_budget_and_remaining_gaps() -> None:
    gate = CompletionGate(
        required_tools=frozenset({"verify"}),
        max_continuations=1,
    )
    policy = CompletionGateWorkflowPolicy(gate)

    continuation = policy.next_continuation(
        stop_reason="end_turn",
        final_content="",
        step=1,
    )
    assert continuation is not None
    metadata = policy.terminal_metadata("end_turn", "")

    assert metadata["completionGate"]["continuations"] == 1
    assert metadata["completionGate"]["budgetExhausted"] is True
    assert metadata["completionGate"]["gaps"]


def test_completion_gate_terminal_boundary_exposes_recoverable_pause() -> None:
    policy = CompletionGateWorkflowPolicy(
        CompletionGate(required_tools=frozenset({"verify"}), max_continuations=0)
    )

    message = policy.terminal_message("end_turn", "done")

    assert message is not None
    assert "recoverable workflow" in message
    assert "durable workspace checkpoint" in message


def test_completion_gate_terminal_boundary_stays_silent_before_budget_exhaustion() -> None:
    policy = CompletionGateWorkflowPolicy(
        CompletionGate(required_tools=frozenset({"verify"}), max_continuations=2)
    )

    assert policy.terminal_message("end_turn", "done") is None


@pytest.mark.asyncio
async def test_kernel_completion_gate_publishes_recoverable_pause_boundary() -> None:
    class LLM:
        async def stream(self, request):
            del request
            yield ModelChunk(content="done", finish_reason="end_turn")

    events = []
    result = await AgentLoopKernel(
        llm=LLM(),
        tool_engine=None,
        workflow_policy=CompletionGateWorkflowPolicy(
            CompletionGate(
                required_tools=frozenset({"verify"}),
                max_continuations=0,
            )
        ),
    ).run(
        RunRequest(
            request_id="completion-boundary",
            session_id="session",
            turn_id="turn",
            user_input=Message.user("finish"),
            options=RunOptions(max_steps=2),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert result.stop_reason == "checkpoint_paused"
    assert "durable workspace checkpoint" in result.final_message
    completed = next(event for event in events if event.type == "run.completed")
    assert completed.payload["stop_reason"] == "checkpoint_paused"


@pytest.mark.asyncio
async def test_kernel_maps_openai_stop_to_workflow_natural_end() -> None:
    class LLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            del request
            self.calls += 1
            yield ModelChunk(content=f"turn-{self.calls}", finish_reason="stop")

    events = []
    llm = LLM()
    result = await AgentLoopKernel(
        llm=llm,
        tool_engine=None,
        workflow_policy=CompletionGateWorkflowPolicy(
            CompletionGate(required_tools=frozenset({"verify"}), max_continuations=1)
        ),
    ).run(
        RunRequest(
            request_id="openai-stop-workflow",
            session_id="session",
            turn_id="turn",
            user_input=Message.user("finish"),
            options=RunOptions(max_steps=3),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.stop_reason == "checkpoint_paused"
    assert llm.calls == 2
    assert any(event.type == "workflow.continuation.requested" for event in events)


def test_completion_gate_policy_owns_legacy_budget_rules() -> None:
    """Budget exemptions and workflow-specific caps stay inside the plugin."""

    policy = CompletionGateWorkflowPolicy(
        CompletionGate(
            required_tools=frozenset(),
            max_delegated_tool_calls=2,
            web_search_total_limit=1,
            budget_exempt_tools=frozenset({"free"}),
        )
    )

    assert policy.exempts_tool_budget("free") is True
    assert policy.exempts_tool_budget("paid") is False
    assert policy.tool_call_error("sub_agent", {}) is None
    assert policy.tool_call_error("web_search", {}) is None
    assert policy.tool_call_error("web_search", {}) is not None

    class DelegationResult:
        success = True
        content = "delegated"
        raw_output = {"type": "sub_agent_delegation", "tool_calls": 2}

    policy.record_tool_result("sub_agent", {}, DelegationResult())
    assert policy.tool_call_error("sub_agent", {}) is not None


def test_completion_gate_policy_replays_budget_counters_from_events() -> None:
    policy = CompletionGateWorkflowPolicy(
        CompletionGate(
            max_delegated_tool_calls=1,
            web_search_total_limit=1,
        )
    )
    restored = policy.for_run(
        RunRequest(
            request_id="budget-replay",
            session_id="session-a",
            turn_id="turn-a",
            user_input=Message.user("resume"),
        ),
        SimpleNamespace(
            events=(
                {
                    "type": "tool.call.requested",
                    "payload": {
                        "call_id": "delegated-1",
                        "tool_name": "sub_agent",
                        "arguments": {},
                    },
                },
                {
                    "type": "tool.call.completed",
                    "payload": {
                        "call_id": "delegated-1",
                        "tool_name": "sub_agent",
                        "arguments": {},
                        "status": "succeeded",
                        "content": "delegated",
                        "output": {
                            "type": "sub_agent_delegation",
                            "tool_calls": 1,
                        },
                    },
                },
                {
                    "type": "tool.call.requested",
                    "payload": {
                        "call_id": "search-1",
                        "tool_name": "web_search",
                        "arguments": {},
                    },
                },
                {
                    "type": "tool.call.completed",
                    "payload": {
                        "call_id": "search-1",
                        "tool_name": "web_search",
                        "arguments": {},
                        "status": "succeeded",
                        "content": "result",
                    },
                },
            )
        ),
    )

    assert restored.tool_call_error("sub_agent", {}) is not None
    assert restored.tool_call_error("web_search", {}) is not None


def test_completion_gate_policy_emits_completion_reserve_guidance() -> None:
    policy = CompletionGateWorkflowPolicy(
        CompletionGate(
            required_tools=frozenset({"verify"}),
            max_tool_calls=3,
            completion_reserve_tool_calls=1,
        )
    )

    assert policy.context_items({})
    assert policy.tool_call_error("inspect", {}) is None
    assert all(
        "交付收尾预算" not in item.content
        for item in policy.context_items({})
    )
    assert policy.tool_call_error("patch", {}) is None
    guidance = policy.context_items({})
    assert any("交付收尾预算" in item.content for item in guidance)


def test_completion_gate_policy_emits_native_near_limit_guidance() -> None:
    policy = CompletionGateWorkflowPolicy(
        CompletionGate(required_tools=frozenset({"verify"}))
    ).for_run(
        RunRequest(
            request_id="near-limit",
            session_id="session",
            turn_id="turn",
            user_input=Message.user("finish"),
            options=RunOptions(max_steps=4),
        )
    )

    guidance = policy.context_items({"step": 3})

    assert any("步数预算即将用尽" in item.content for item in guidance)
    assert all(item.metadata.get("workflow_context") for item in guidance)


def test_completion_gate_policy_emits_native_budget_boundary_guidance() -> None:
    policy = CompletionGateWorkflowPolicy(
        CompletionGate(
            required_tools=frozenset({"verify"}),
            max_tool_calls=2,
            web_search_total_limit=1,
            max_delegated_tool_calls=1,
        )
    ).for_run(
        RunRequest(
            request_id="budget-boundary",
            session_id="session",
            turn_id="turn",
            user_input=Message.user("finish"),
            options=RunOptions(max_steps=8),
        )
    )

    assert policy.tool_call_error("inspect", {}) is None
    assert policy.tool_call_error("patch", {}) is None
    guidance = policy.context_items({"step": 3})

    assert any("工具调用总预算已达到上限" in item.content for item in guidance)
    assert not any("本轮 web_search 调用已达到预算上限" in item.content for item in guidance)


def test_completion_gate_terminal_boundary_reflects_run_tool_budget() -> None:
    """A host run cap is a recoverable gate boundary, even without a gate cap."""

    policy = CompletionGateWorkflowPolicy(
        CompletionGate(required_tools=frozenset({"verify"}), max_continuations=2)
    ).for_run(
        RunRequest(
            request_id="run-tool-budget",
            session_id="session",
            turn_id="turn",
            user_input=Message.user("finish"),
            options=RunOptions(max_tool_calls=1),
        )
    )
    assert policy.tool_call_error("inspect", {}) is None

    metadata = policy.terminal_metadata("end_turn", "done")["completionGate"]
    assert metadata["budgetExhausted"] is True
    assert policy.terminal_message("end_turn", "done") is not None


@pytest.mark.asyncio
async def test_kernel_does_not_continue_completion_gate_after_tool_budget_exhaustion() -> None:
    """A gate must not inject an impossible continuation after max_tool_calls."""

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
                            call_id="unrelated-call",
                            tool_name="unrelated",
                            arguments={},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield ModelChunk(content="done", finish_reason="end_turn")

    class Tools:
        async def execute(self, call, *, context=None):
            del context
            from box_agent.api import ToolCallResult

            return ToolCallResult(
                call_id=call.call_id,
                status="succeeded",
                content="unrelated result",
            )

    llm = LLM()
    events = []
    policy = CompletionGateWorkflowPolicy(
        CompletionGate(required_tools=frozenset({"verify"}), max_continuations=2)
    )
    result = await AgentLoopKernel(
        llm=llm,
        tool_engine=Tools(),
        workflow_policy=policy,
    ).run(
        RunRequest(
            request_id="completion-budget-request",
            session_id="completion-budget-session",
            turn_id="completion-budget-turn",
            user_input=Message.user("finish"),
            options=RunOptions(max_steps=4, max_tool_calls=1),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert llm.calls == 2
    assert not any(event.type == "workflow.continuation.requested" for event in events)


@pytest.mark.asyncio
async def test_kernel_enforces_completion_gate_delegated_budget_via_plugin() -> None:
    class LLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            del request
            self.calls += 1
            if self.calls <= 2:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id=f"delegate-{self.calls}",
                            tool_name="sub_agent",
                            arguments={},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield ModelChunk(content="done", finish_reason="end_turn")

    class Tools:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, call, *, context=None):
            del context
            self.calls += 1
            return ToolCallResult(
                call_id=call.call_id,
                status="succeeded",
                content="delegated",
                output={"type": "sub_agent_delegation", "tool_calls": 1},
            )

    llm = LLM()
    tools = Tools()
    events = []
    result = await AgentLoopKernel(
        llm=llm,
        tool_engine=tools,
        workflow_policy=CompletionGateWorkflowPolicy(
            CompletionGate(max_delegated_tool_calls=1)
        ),
    ).run(
        RunRequest(
            request_id="delegated-budget-request",
            session_id="delegated-budget-session",
            turn_id="delegated-budget-turn",
            user_input=Message.user("delegate"),
            options=RunOptions(max_steps=4),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert tools.calls == 1
    blocked = [
        event
        for event in events
        if event.type == "tool.call.completed"
        and event.payload.get("call_id") == "delegate-2"
    ]
    assert blocked and blocked[0].payload["status"] == "failed"
    assert "Delegated tool call budget reached" in blocked[0].payload["error"]["message"]


@pytest.mark.asyncio
async def test_native_completion_gate_checkpoint_replays_tool_identity(tmp_path) -> None:
    class VerifyTool:
        name = "verify"
        description = "verify"
        parameters = {"type": "object", "properties": {}}

        async def invoke(self, arguments, *, context=None):
            del arguments, context
            from box_agent.tools.base import ToolResult

            return ToolResult(success=True, content="verified")

    class LLM:
        def __init__(self):
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id="verify-call",
                            tool_name="verify",
                            arguments={},
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield ModelChunk(content="done", finish_reason="end_turn")

    gate = CompletionGate(
        required_tools=frozenset({"verify"}),
        max_continuations=1,
    )
    host = PluginHost()
    host.registries["llm"].register("default", LLM(), source="test")
    host.registries["tools"].register("verify", VerifyTool(), source="test")
    host.registries["workflows"].register(
        "default",
        CompletionGateWorkflowPolicy(gate, workspace_dir=tmp_path),
        source="test",
    )
    event_log = SQLiteEventLog(tmp_path / "completion-gate-replay.sqlite3")
    service = KernelAgentService.from_plugin_host(host, event_log=event_log)
    await service.open_session(SessionOpenRequest(session_id="completion-session"))
    handle = await service.start(
        RunRequest(
            request_id="completion-request",
            session_id="completion-session",
            turn_id="completion-turn",
            user_input=Message.user("verify"),
            options=RunOptions(workflow_options={"max_continuations": 1}),
        )
    )
    result = await handle.wait()
    assert result.status == "completed"
    events = [event async for event in handle.events()]
    completed = next(event for event in events if event.type == "tool.call.completed")
    assert completed.payload["tool_name"] == "verify"
    assert completed.payload["arguments"] == {}
    # A fresh policy can now reconstruct the satisfied requirement from the
    # durable event, rather than relying on process-local mutable state.
    bundle = await event_log.load_recovery_bundle(handle.run_id)
    restored = CompletionGateWorkflowPolicy(
        gate,
        workspace_dir=tmp_path,
    ).for_run(
        RunRequest(
            request_id="completion-request",
            session_id="completion-session",
            turn_id="completion-turn",
            user_input=Message.user("resume"),
        ),
        bundle,
    )
    assert restored.next_continuation(
        stop_reason="end_turn", final_content="done", step=1
    ) is None
    event_log.close()


def test_ppt_and_skill_policies_restore_native_checkpoint_state(tmp_path) -> None:
    request = RunRequest(
        request_id="request-policy-recovery",
        session_id="session-a",
        turn_id="turn-1",
        user_input=Message.user("resume"),
        metadata={"workspace_dir": str(tmp_path)},
    )
    skill = ExternalSkillRunPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=tmp_path / "output",
        skill_name="vendor-skill",
        skill_source="vendor",
        task_text="make an artifact",
    )
    skill.observed_paths.append(str(tmp_path / "work.txt"))
    skill.stage = "skill_active"
    skill_state = skill.build_checkpoint_payload()
    restored_skill = skill.for_run(
        request,
        SimpleNamespace(
            events=(
                {
                    "type": "workflow.checkpoint",
                    "payload": {"workflow_state": skill_state},
                },
            )
        ),
    )
    assert restored_skill.skill_name == "vendor-skill"
    assert restored_skill.stage == "skill_active"
    assert restored_skill.observed_paths == skill.observed_paths
    skill_context = restored_skill.context_items({"phase": "before_model"})
    assert skill_context and skill_context[0].metadata["workflow"] == skill.kind

    presentation = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=tmp_path / "output",
    )
    presentation.stage = "outline"
    presentation._research_tool_attempts = 2
    presentation_state = presentation.build_checkpoint_payload()
    restored_presentation = presentation.for_run(
        request,
        SimpleNamespace(
            events=(
                {
                    "type": "workflow.checkpoint",
                    "payload": {
                        "workflow_state": presentation_state
                    },
                },
            )
        ),
    )
    assert restored_presentation.stage == "outline"
    assert restored_presentation._research_tool_attempts == 2
    presentation_context = restored_presentation.context_items(
        {"phase": "before_model"}
    )
    assert presentation_context
    assert presentation_context[0].metadata["workflow"] == presentation.kind


def test_composer_binds_run_local_workflow_view() -> None:
    goals = GoalStore()
    goals.set("session-a", "Ship A")

    class LLM:
        async def stream(self, request):
            if False:
                yield request

    host = PluginHost()
    host.registries["llm"].register("default", LLM(), source="test")
    host.registries["workflows"].register(
        "default", GoalWorkflowPolicy(goals), source="test"
    )
    kernel = PluginKernelComposer(host).build(
        RunRequest(
            request_id="request-1",
            session_id="session-a",
            turn_id="turn-1",
            user_input=Message.user("continue"),
        )
    )
    assert kernel._workflow.session_id == "session-a"
    assert "Ship A" in kernel._workflow.build_checkpoint()


def test_goal_policy_contributes_active_instruction_through_context_spi() -> None:
    goals = GoalStore()
    goals.set("session-a", "Ship A")
    policy = GoalWorkflowPolicy(goals, session_id="session-a")

    items = policy.context_items({"phase": "before_model"})

    assert len(items) == 1
    assert items[0].kind == "workflow"
    assert items[0].metadata["workflow_context"] is True
    assert "Objective: Ship A" in items[0].content
    goals.pause("session-a")
    assert policy.context_items({}) == ()


def test_goal_policy_exposes_typed_autopilot_continuation() -> None:
    goals = GoalStore()
    goals.set("session-a", "Ship native", progress=["implemented"])
    policy = GoalWorkflowPolicy(
        goals,
        session_id="session-a",
        autopilot_enabled=True,
        max_continuations=1,
    )

    continuation = policy.next_continuation(
        stop_reason="end_turn",
        final_content="still working",
        step=1,
    )

    assert isinstance(continuation, WorkflowContinuation)
    assert continuation.reason == "goal_autopilot"
    assert continuation.metadata["continuation"] == 1
    assert "Goal autopilot continuation 1/1" in str(continuation.message.content)

    goals.complete("session-a", evidence=["verified"])
    assert (
        policy.next_continuation(
            stop_reason="end_turn", final_content="done", step=2
        )
        is None
    )


def test_goal_autopilot_treats_openai_stop_as_natural_end() -> None:
    goals = GoalStore()
    goals.set("session-a", "Ship native", progress=["implemented"])
    policy = GoalWorkflowPolicy(
        goals,
        session_id="session-a",
        autopilot_enabled=True,
        max_continuations=1,
    )

    continuation = policy.next_continuation(
        stop_reason="stop", final_content="still working", step=1
    )

    assert continuation is not None
    assert continuation.reason == "goal_autopilot"


def test_goal_continuation_budget_is_scoped_to_one_run() -> None:
    goals = GoalStore()
    goals.set("session-a", "Ship native")
    policy = GoalWorkflowPolicy(
        goals,
        autopilot_enabled=True,
        max_continuations=1,
    )
    request = RunRequest(
        request_id="request-new-turn",
        session_id="session-a",
        turn_id="turn-2",
        user_input=Message.user("continue"),
        metadata={
            "_session_events": {
                "events": [
                    {"type": "workflow.continuation.requested"},
                ]
            }
        },
    )

    bound = policy.for_run(request)
    assert bound.next_continuation(
        stop_reason="end_turn", final_content="more", step=1
    ) is not None


def test_goal_continuation_stops_after_configured_no_progress_turns() -> None:
    goals = GoalStore()
    goals.set("session-a", "Ship native")
    policy = GoalWorkflowPolicy(
        goals,
        session_id="session-a",
        autopilot_enabled=True,
        max_continuations=3,
        no_progress_turns=1,
    )

    assert policy.next_continuation(
        stop_reason="end_turn", final_content="same", step=1
    ) is not None
    assert policy.next_continuation(
        stop_reason="end_turn", final_content="same", step=2
    ) is None


def test_goal_autopilot_stops_after_native_wall_clock_budget() -> None:
    goals = GoalStore()
    goals.set("session-a", "Ship native")
    policy = GoalWorkflowPolicy(
        goals,
        session_id="session-a",
        autopilot_enabled=True,
        max_continuations=3,
        max_seconds=0.5,
    )

    policy._started_at = time.monotonic() - 1.0

    assert policy.should_continue_autopilot("end_turn") is False


def test_goal_autopilot_checkpoint_restores_progress_guard_state() -> None:
    goals = GoalStore()
    goals.set("session-a", "Ship native")
    policy = GoalWorkflowPolicy(
        goals,
        session_id="session-a",
        autopilot_enabled=True,
        max_continuations=3,
        no_progress_turns=2,
    )
    signature = goal_autopilot_progress_signature(goals.get("session-a"))
    policy.record_autopilot_turn(
        stop_reason="end_turn",
        before_signature=signature,
    )
    policy._continuation_count = 1
    payload = policy.build_checkpoint_payload()

    restored = GoalWorkflowPolicy(
        goals,
        autopilot_enabled=True,
        max_continuations=3,
        no_progress_turns=2,
    ).for_run(
        RunRequest(
            request_id="goal-recovery",
            session_id="session-a",
            turn_id="turn-a",
            user_input=Message.user("resume"),
        ),
        SimpleNamespace(
            events=(
                {
                    "type": "workflow.checkpoint",
                    "payload": {"workflow_state": payload},
                },
            )
        ),
    )

    assert restored._continuation_count == 1
    assert restored._no_progress_count == 1
    assert restored._last_stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_kernel_runs_plugin_continuation_before_terminal_event() -> None:
    class LLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ModelChunk(content="continue", finish_reason="end_turn")
            else:
                yield ModelChunk(content="finished", finish_reason="end_turn")

    class Policy:
        def __init__(self) -> None:
            self.calls = 0

        def next_continuation(self, *, stop_reason, final_content, step):
            self.calls += 1
            if self.calls > 1 or stop_reason != "end_turn":
                return None
            return WorkflowContinuation(
                continuation_id="test:1",
                message=Message.user("continue without another user turn"),
                reason="test",
            )

    llm = LLM()
    events = []
    result = await AgentLoopKernel(llm=llm, workflow_policy=Policy()).run(
        RunRequest(
            request_id="request-continuation",
            session_id="session-a",
            turn_id="turn-1",
            user_input=Message.user("start"),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert result.final_message == "finished"
    assert len(llm.requests) == 2
    assert "continue without another user turn" in str(llm.requests[1].messages)
    event_types = [event.type for event in events]
    assert event_types.count("workflow.continuation.requested") == 1
    assert event_types[-1] == "run.completed"


@pytest.mark.asyncio
async def test_goal_autopilot_checkpoints_updated_guard_before_continuation_event() -> None:
    class LLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            del request
            self.calls += 1
            yield ModelChunk(content="working", finish_reason="end_turn")

    goals = GoalStore()
    goals.set("session-a", "Ship native")
    events = []
    result = await AgentLoopKernel(
        llm=LLM(),
        workflow_policy=GoalWorkflowPolicy(
            goals,
            session_id="session-a",
            autopilot_enabled=True,
            max_continuations=1,
            no_progress_turns=2,
        ),
    ).run(
        RunRequest(
            request_id="autopilot-checkpoint-order",
            session_id="session-a",
            turn_id="turn-1",
            user_input=Message.user("continue"),
            options=RunOptions(max_steps=2),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    continuation_index = next(
        index
        for index, event in enumerate(events)
        if event.type == "workflow.continuation.requested"
    )
    checkpoint = next(
        event
        for event in reversed(events[:continuation_index])
        if event.type == "workflow.checkpoint"
    )
    assert checkpoint.payload["workflow_state"]["goal"]["autopilot"] == {
        "continuation_count": 1,
        "no_progress_count": 0,
        "last_stop_reason": "end_turn",
        "stop_cause": None,
        "last_signature": [
            "Ship native",
            "active",
            [],
            [],
            None,
            None,
        ],
    }


@pytest.mark.asyncio
async def test_kernel_publishes_native_plan_start_before_model_request() -> None:
    class LLM:
        async def stream(self, request):
            del request
            yield ModelChunk(content="规划完成", finish_reason="stop")

    plans = SessionPlanStore()
    registry = TypedRegistry("tools")
    for tool in build_plan_tools(plans):
        registry.register(tool.name, tool, source="test")
    events = []
    llm = LLM()
    result = await AgentLoopKernel(
        llm=llm,
        tool_engine=RegistryToolEngine(registry),
        workflow_policy=PlanWorkflowPolicy(plans),
    ).run(
        RunRequest(
            request_id="plan-start-kernel",
            session_id="plan-start-session",
            turn_id="plan-start-turn",
            user_input=Message.user("请先做一个计划"),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    plan_index = next(
        index
        for index, event in enumerate(events)
        if event.type == "workflow.plan.snapshot"
    )
    model_index = next(
        index
        for index, event in enumerate(events)
        if event.type == "model.requested"
    )
    assert plan_index < model_index


@pytest.mark.asyncio
async def test_kernel_injects_native_force_plan_guidance_before_model_request() -> None:
    class LLM:
        def __init__(self) -> None:
            self.request = None

        async def stream(self, request):
            self.request = request
            yield ModelChunk(content="done", finish_reason="stop")

    plans = SessionPlanStore()
    registry = TypedRegistry("tools")
    for tool in build_plan_tools(plans):
        registry.register(tool.name, tool, source="test")
    llm = LLM()
    result = await AgentLoopKernel(
        llm=llm,
        tool_engine=RegistryToolEngine(registry),
        workflow_policy=PlanWorkflowPolicy(plans, force_plan_start=True),
    ).run(
        RunRequest(
            request_id="plan-guidance-kernel",
            session_id="plan-guidance-session",
            turn_id="plan-guidance-turn",
            user_input=Message.user("完成这个任务"),
            options=RunOptions(workflow_options={"force_plan_start": True}),
        ),
        emit=lambda _event: None,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert llm.request is not None
    rendered = "\n".join(str(message.content) for message in llm.request.messages)
    assert "Host UI requires a structured execution plan" in rendered
    assert "plan_write" in rendered


@pytest.mark.asyncio
async def test_kernel_persists_native_plan_approval_output() -> None:
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
                            call_id="plan-write-native",
                            tool_name="plan_write",
                            arguments={
                                "action": "set",
                                "title": "Release",
                                "steps": [{"title": "Verify"}],
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield ModelChunk(content="等待审批", finish_reason="stop")

    plans = SessionPlanStore()
    registry = TypedRegistry("tools")
    for tool in build_plan_tools(plans):
        registry.register(tool.name, tool, source="test")
    events = []
    llm = LLM()
    result = await AgentLoopKernel(
        llm=llm,
        tool_engine=RegistryToolEngine(registry),
        workflow_policy=PlanWorkflowPolicy(
            plans,
            require_plan_approval=True,
        ),
    ).run(
        RunRequest(
            request_id="plan-approval-kernel",
            session_id="plan-approval-session",
            turn_id="plan-approval-turn",
            user_input=Message.user("准备一个执行计划"),
            options=RunOptions(
                workflow_options={"require_plan_approval": True}
            ),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert result.stop_reason == "checkpoint_paused"
    assert result.final_message == "计划已生成，等待用户确认后再执行。"
    assert llm.calls == 1
    completed = next(
        event
        for event in events
        if event.type == "tool.call.completed"
        and event.payload.get("call_id") == "plan-write-native"
    )
    assert completed.payload["output"]["approval"]["state"] == "pending"


@pytest.mark.asyncio
async def test_kernel_pauses_native_plan_after_write_before_next_model_turn() -> None:
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
                            call_id="plan-pause-native",
                            tool_name="plan_write",
                            arguments={
                                "action": "set",
                                "title": "Release",
                                "steps": [{"title": "Verify"}],
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                )
                return
            yield ModelChunk(content="unexpected second turn", finish_reason="stop")

    plans = SessionPlanStore()
    registry = TypedRegistry("tools")
    for tool in build_plan_tools(plans):
        registry.register(tool.name, tool, source="test")
    events = []
    llm = LLM()
    result = await AgentLoopKernel(
        llm=llm,
        tool_engine=RegistryToolEngine(registry),
        workflow_policy=PlanWorkflowPolicy(
            plans,
            pause_after_plan_write=True,
        ),
    ).run(
        RunRequest(
            request_id="plan-pause-kernel",
            session_id="plan-pause-session",
            turn_id="plan-pause-turn",
            user_input=Message.user("准备一个执行计划"),
            options=RunOptions(
                workflow_options={"pause_after_plan_write": True}
            ),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert result.stop_reason == "checkpoint_paused"
    assert result.final_message == "计划已生成，等待用户确认后再执行。"
    assert result.metadata["runStatus"] == "paused"
    assert result.metadata["planApproval"]["state"] == "pending"
    assert llm.calls == 1
    completed = next(
        event
        for event in events
        if event.type == "run.completed"
    )
    assert completed.payload["stop_reason"] == "checkpoint_paused"
    assert completed.payload["metadata"]["runStatus"] == "paused"


@pytest.mark.asyncio
async def test_native_plan_pause_rehydrates_across_service_restart_and_approval(tmp_path) -> None:
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
                            call_id="plan-pause-recovery",
                            tool_name="plan_write",
                            arguments={
                                "action": "set",
                                "title": "Release",
                                "steps": [{"title": "Verify"}],
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                )
                return
            yield ModelChunk(content="approved and continued", finish_reason="stop")

    plans = SessionPlanStore()
    host = PluginHost()
    llm = LLM()
    host.registries["llm"].register("default", llm, source="test")
    host.registries["workflows"].register(
        "default",
        PlanWorkflowPolicy(plans),
        source="test",
    )
    for tool in build_plan_tools(plans):
        host.registries["tools"].register(tool.name, tool, source="test")

    db_path = tmp_path / "plan-pause-recovery.sqlite3"
    first = KernelAgentService.from_plugin_host(host, event_log=SQLiteEventLog(db_path))
    session_id = "plan-pause-recovery-session"
    await first.open_session(SessionOpenRequest(session_id=session_id))
    paused = await first.start(
        RunRequest(
            request_id="plan-pause-recovery-1",
            session_id=session_id,
            turn_id="turn-1",
            user_input=Message.user("请准备并暂停计划"),
            options=RunOptions(
                workflow_options={"pause_after_plan_write": True}
            ),
        )
    )
    paused_result = await paused.wait()
    assert paused_result.stop_reason == "checkpoint_paused"

    second = KernelAgentService.from_plugin_host(
        host,
        event_log=SQLiteEventLog(db_path),
    )
    await second.open_session(SessionOpenRequest(session_id=session_id))
    continued = await second.start(
        RunRequest(
            request_id="plan-pause-recovery-2",
            session_id=session_id,
            turn_id="turn-2",
            user_input=Message.user("确认执行计划"),
            options=RunOptions(
                workflow_options={
                    "pause_after_plan_write": True,
                    "plan_approval": {"approved": True},
                }
            ),
        )
    )
    result = await continued.wait()

    assert result.status == "completed"
    assert result.final_message == "approved and continued"
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_service_checkpoints_plugin_continuation_for_restart(tmp_path) -> None:
    class LLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            yield ModelChunk(
                content="continue" if self.calls == 1 else "finished",
                finish_reason="end_turn",
            )

    goals = GoalStore()
    goals.set("session-a", "Ship native")
    host = PluginHost()
    host.registries["llm"].register("default", LLM(), source="test")
    host.registries["workflows"].register(
        "default",
        GoalWorkflowPolicy(goals, autopilot_enabled=False),
        source="test",
    )
    event_log = SQLiteEventLog(tmp_path / "continuation.sqlite3")
    service = KernelAgentService.from_plugin_host(host, event_log=event_log)
    await service.open_session(SessionOpenRequest(session_id="session-a"))
    handle = await service.start(
        RunRequest(
            request_id="request-service-continuation",
            session_id="session-a",
            turn_id="turn-1",
            user_input=Message.user("start"),
            options=RunOptions(
                workflow_options={
                    "autopilot_enabled": True,
                    "max_continuations": 1,
                }
            ),
        )
    )
    result = await handle.wait()
    assert result.final_message == "finished"
    bundle = await event_log.load_recovery_bundle(handle.run_id)
    assert any(
        event.type == "workflow.continuation.requested" for event in bundle.events
    )
    assert bundle.checkpoint is not None
    assert bundle.checkpoint.state["last_event_type"] == "run.completed"


@pytest.mark.asyncio
async def test_kernel_refreshes_workflow_context_after_goal_tool_update() -> None:
    class LLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id="goal-1",
                            tool_name="goal_write",
                            arguments={
                                "action": "set",
                                "objective": "Ship native",
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                )
            else:
                yield ModelChunk(content="done", finish_reason="stop")

    llm = LLM()
    goals = GoalStore()
    tools = TypedRegistry("tools")
    for tool in build_goal_tools(goals):
        tools.register(tool.name, tool, source="test")
    result = await AgentLoopKernel(
        llm=llm,
        workflow_policy=GoalWorkflowPolicy(goals, session_id="session-a"),
        tool_engine=RegistryToolEngine(tools),
    ).run(
        RunRequest(
            request_id="request-goal-context",
            session_id="session-a",
            turn_id="turn-1",
            user_input=Message.user("start"),
        ),
        emit=lambda event: None,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert len(llm.requests) == 2
    assert "Objective: Ship native" in str(llm.requests[1].messages)


@pytest.mark.asyncio
async def test_workflow_event_hook_forwards_events_without_kernel_knowledge() -> None:
    seen = []

    class Policy:
        async def on_event(self, event):
            seen.append(event)

    hook = WorkflowEventHook([Policy()])
    await hook.on_event({"type": "workflow.checkpoint"})
    assert seen == [{"type": "workflow.checkpoint"}]


@pytest.mark.asyncio
async def test_workflow_hook_rehydrates_goal_and_plan_state() -> None:
    goals = GoalStore()
    plans = SessionPlanStore()
    policy = CompositeWorkflowPolicy(
        [GoalWorkflowPolicy(goals, session_id="session-a"), PlanWorkflowPolicy(plans, session_id="session-a")]
    )
    hook = WorkflowEventHook([policy])
    await hook.on_event(
        {
            "type": "workflow.checkpoint",
            "session_id": "session-a",
            "payload": {
                "workflow_state": {
                    "goal": {"goal": {"objective": "Goal from event", "status": "active"}},
                    "plan": {"plan": {"id": "p-2", "title": "Plan from event"}},
                }
            },
        }
    )
    assert goals.get("session-a").objective == "Goal from event"
    assert plans.get("session-a")["title"] == "Plan from event"


@pytest.mark.asyncio
async def test_kernel_emits_structured_workflow_checkpoint_state() -> None:
    class LLM:
        async def stream(self, request):
            yield ModelChunk(content="done", finish_reason="stop")

    goals = GoalStore()
    goals.set("session-a", "Ship A")
    events = []
    result = await AgentLoopKernel(
        llm=LLM(),
        workflow_policy=GoalWorkflowPolicy(goals, session_id="session-a"),
    ).run(
        RunRequest(
            request_id="request-1",
            session_id="session-a",
            turn_id="turn-1",
            user_input=Message.user("continue"),
        ),
        emit=events.append,
        cancel_event=asyncio.Event(),
    )
    assert result.status == "completed"
    checkpoint = next(event for event in events if event.type == "workflow.checkpoint")
    assert checkpoint.payload["workflow_state"]["goal"]["goal"]["objective"] == "Ship A"
    assert result.metadata["goal"]["objective"] == "Ship A"


def test_goal_autopilot_policy_matches_legacy_prompt_and_progress_facts() -> None:
    from box_agent.agent import (
        GoalState as LegacyGoalState,
        goal_autopilot_progress_signature as legacy_signature,
        goal_autopilot_prompt as legacy_prompt,
        should_continue_goal_autopilot as legacy_should_continue,
    )

    legacy_goal = LegacyGoalState(
        objective="Ship it",
        status="active",
        created_at="2026-01-01",
        updated_at="2026-01-02",
        progress=["implemented"],
        evidence=["pytest passed"],
    )
    native_goal = GoalStore()
    native_goal.restore("session-a", legacy_goal.__dict__ | {"createdAt": "2026-01-01", "updatedAt": "2026-01-02"})
    restored = native_goal.get("session-a")
    assert restored is not None
    assert goal_autopilot_progress_signature(restored) == legacy_signature(legacy_goal)
    assert goal_autopilot_prompt(restored, 1, 3) == legacy_prompt(legacy_goal, 1, 3)
    assert should_continue_goal_autopilot(restored, "end_turn") == legacy_should_continue(type("A", (), {"goal": legacy_goal})(), "end_turn")


def test_workflow_policy_rehydrates_structured_checkpoint_state() -> None:
    request = type(
        "Request",
        (),
        {
            "session_id": "session-recovered",
            "metadata": {
                "_session_events": {
                    "events": [
                        {
                            "payload": {
                                "workflow_state": {
                                    "goal": {
                                        "goal": {
                                            "objective": "Resume goal",
                                            "status": "paused",
                                        }
                                    },
                                    "plan": {
                                        "plan": {
                                            "id": "p-1",
                                            "title": "Resume plan",
                                            "steps": [{"title": "Continue"}],
                                        }
                                    },
                                }
                            }
                        }
                    ]
                }
            },
        },
    )()
    goals = GoalStore()
    plans = SessionPlanStore()
    bound = CompositeWorkflowPolicy(
        [GoalWorkflowPolicy(goals), PlanWorkflowPolicy(plans)]
    ).for_run(request)
    assert goals.get("session-recovered").objective == "Resume goal"
    assert plans.get("session-recovered")["title"] == "Resume plan"


def test_native_plan_classifier_preserves_rich_workflow_fallbacks() -> None:
    assert text_requests_native_plan("请先制定计划")
    assert text_requests_native_plan("make a plan")
    assert not text_requests_native_plan("请制作一个 PPT 计划")
    assert not text_requests_native_plan("/goal ship it")
