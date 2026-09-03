"""Deterministic fixtures preserving promoted workflow behavior."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.api import Message, RunOptions, RunRequest
from box_agent.workflows.goal import (
    GoalWorkflowPolicy,
    GoalStore,
    goal_autopilot_progress_signature,
    goal_autopilot_prompt,
    goal_state_from_payload,
    should_continue_goal_autopilot,
)
from box_agent.workflows.completion_gate import CompletionGateWorkflowPolicy
from box_agent.loop_guards import CompletionGate, completion_gate_gaps
from box_agent.tools.plan_tool import PlanStore, PlanWriteTool
from box_agent.workflows.plan import (
    PlanWorkflowPolicy,
    SessionPlanStore,
    SessionPlanWriteTool,
)
from box_agent.tools.skill_loader import Skill, SkillLoader
from box_agent.workflows.controlled_presentation import ControlledPresentationPolicy
from box_agent.workflows.external_skill import ExternalSkillRunPolicy


FIXTURE_PATH = Path(__file__).parent / "parity" / "goal_plan_cases.json"
PLAN_FIXTURE_PATH = Path(__file__).parent / "parity" / "plan_cases.json"
PLAN_EVENT_FIXTURE_PATH = Path(__file__).parent / "parity" / "plan_event_cases.json"
COMPLETION_GATE_FIXTURE_PATH = (
    Path(__file__).parent / "parity" / "completion_gate_cases.json"
)
COMPLETION_GATE_EVENT_FIXTURE_PATH = (
    Path(__file__).parent / "parity" / "completion_gate_event_cases.json"
)
AUTOPILOT_FIXTURE_PATH = Path(__file__).parent / "parity" / "autopilot_cases.json"
AUTOPILOT_EVENT_FIXTURE_PATH = (
    Path(__file__).parent / "parity" / "autopilot_event_cases.json"
)
SKILL_FIXTURE_PATH = Path(__file__).parent / "parity" / "skill_cases.json"
SKILL_EVENT_FIXTURE_PATH = Path(__file__).parent / "parity" / "skill_event_cases.json"
PRESENTATION_FIXTURE_PATH = Path(__file__).parent / "parity" / "presentation_cases.json"
PRESENTATION_EVENT_FIXTURE_PATH = (
    Path(__file__).parent / "parity" / "presentation_event_cases.json"
)


def test_goal_fixture_matches_legacy_prompt_and_progress_contract() -> None:
    # The mature Agent imports optional ACP/provider SDKs at module import time.
    # Keep this gate explicit: a missing optional runtime skips the oracle
    # comparison rather than turning a source-only test run into a false red.
    pytest.importorskip("acp")
    from box_agent.agent import (
        GoalState as LegacyGoalState,
        goal_autopilot_progress_signature as legacy_signature,
        goal_autopilot_prompt as legacy_prompt,
        should_continue_goal_autopilot as legacy_should_continue,
    )

    cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(cases, list) and cases
    for case in cases:
        legacy_goal = LegacyGoalState(
            objective=case["objective"],
            status=case["status"],
            created_at=case["created_at"],
            updated_at=case["updated_at"],
            progress=list(case["progress"]),
            evidence=list(case["evidence"]),
            blocked_reason=case["blocked_reason"],
            completed_by=case["completed_by"],
        )
        native_goal = goal_state_from_payload(
            {
                **case,
                "createdAt": case["created_at"],
                "updatedAt": case["updated_at"],
                "blockedReason": case["blocked_reason"],
                "completedBy": case["completed_by"],
            }
        )
        assert native_goal is not None
        assert goal_autopilot_progress_signature(native_goal) == legacy_signature(
            legacy_goal
        )
        assert goal_autopilot_prompt(
            native_goal,
            case["continuation"],
            case["max_continuations"],
        ) == legacy_prompt(
            legacy_goal,
            case["continuation"],
            case["max_continuations"],
        )
        assert should_continue_goal_autopilot(
            native_goal, case["stop_reason"]
        ) == legacy_should_continue(
            SimpleNamespace(goal=legacy_goal), case["stop_reason"]
        )


def test_goal_fixture_round_trips_through_session_store() -> None:
    cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    store = GoalStore()
    for index, case in enumerate(cases):
        restored = store.restore(f"fixture-{index}", case)
        assert restored is not None
        assert restored.objective == case["objective"]
        assert restored.status == case["status"]
        assert restored.progress == case["progress"]
        assert restored.evidence == case["evidence"]


def test_plan_fixture_matches_legacy_tool_payloads() -> None:
    cases = json.loads(PLAN_FIXTURE_PATH.read_text(encoding="utf-8"))
    legacy_store = PlanStore()
    native_store = SessionPlanStore()
    legacy_tool = PlanWriteTool(legacy_store)
    native_tool = SessionPlanWriteTool(native_store, session_id="fixture")

    async def scenario() -> None:
        for case in cases:
            legacy_result = await legacy_tool.execute(**case)
            native_result = await native_tool.execute(**case)
            assert native_result.success == legacy_result.success
            assert native_result.content == legacy_result.content
            assert native_result.raw_output is not None
            assert legacy_result.raw_output is not None
            native_payload = dict(native_result.raw_output)
            legacy_payload = dict(legacy_result.raw_output)
            # IDs and normalized fields are part of the contract. Timestamps
            # are intentionally generated at execution time and are compared
            # for shape rather than wall-clock equality.
            native_plan = native_payload.get("plan") or {}
            legacy_plan = legacy_payload.get("plan") or {}
            for plan in (native_plan, legacy_plan):
                plan.pop("created_at", None)
                plan.pop("updated_at", None)
            assert native_payload == legacy_payload

    import asyncio

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_plan_event_fixture_matches_native_pause_and_checkpoint_stream() -> None:
    from box_agent.api import ModelChunk, ToolCallRequest
    from box_agent.kernel import AgentLoopKernel
    from box_agent.plugins import TypedRegistry
    from box_agent.tools.engine import RegistryToolEngine

    fixture = json.loads(PLAN_EVENT_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == 1
    for index, case in enumerate(fixture["cases"]):
        class LLM:
            async def stream(self, request):
                del request
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id=case["tool_call_id"],
                            tool_name="plan_write",
                            arguments=case["plan"],
                        ),
                    ),
                    finish_reason="tool_calls",
                )

        session_id = f"plan-event-parity-{index}"
        plans = SessionPlanStore()
        registry = TypedRegistry("tools")
        for tool in (
            SessionPlanWriteTool(plans),
        ):
            registry.register(tool.name, tool, source="parity-fixture")
        events = []
        result = await AgentLoopKernel(
            llm=LLM(),
            tool_engine=RegistryToolEngine(registry),
            workflow_policy=PlanWorkflowPolicy(
                plans,
                session_id=session_id,
                require_plan_approval=True,
                force_plan_start=True,
                pause_after_plan_write=True,
            ),
        ).run(
            RunRequest(
                request_id=f"plan-event-request-{index}",
                session_id=session_id,
                turn_id=f"plan-event-turn-{index}",
                user_input=Message.user(case["prompt"]),
                options=RunOptions(
                    workflow_options={
                        "force_plan_start": True,
                        "require_plan_approval": True,
                        "pause_after_plan_write": True,
                        "plan_start_text": case["prompt"],
                    }
                ),
            ),
            emit=events.append,
            cancel_event=asyncio.Event(),
        )

        assert [event.type for event in events] == case["expected_event_types"]
        initial = next(
            event.payload
            for event in events
            if event.type == "workflow.plan.snapshot"
        )
        completed = next(
            event.payload
            for event in events
            if event.type == "tool.call.completed"
        )
        assert [initial["action"], completed["output"]["action"]] == case[
            "expected_plan_actions"
        ]
        assert completed["output"]["type"] == "plan_snapshot"
        assert result.stop_reason == case["expected_stop_reason"]
        assert result.metadata["runStatus"] == case["expected_run_status"]
        assert result.metadata["planApproval"]["state"] == case[
            "expected_approval_state"
        ]
        checkpoint = next(
            event.payload
            for event in events
            if event.type == "workflow.checkpoint"
        )
        assert checkpoint["workflow_state"]["plan"]["plan"]["title"] == "Release"


def test_completion_gate_fixture_matches_legacy_evidence_boundary() -> None:
    cases = json.loads(COMPLETION_GATE_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(cases, list) and cases
    for case in cases:
        gate = CompletionGate(
            required_tools=frozenset(case["required_tools"]),
            max_continuations=case["max_continuations"],
        )
        policy = CompletionGateWorkflowPolicy(gate)
        policy._succeeded_tools.update(case["succeeded_tools"])
        gaps = completion_gate_gaps(gate, set(case["succeeded_tools"]), None)
        continuation = policy.next_continuation(
            stop_reason=case["stop_reason"],
            final_content="done",
            step=1,
        )

        # ``completion_gate_gaps`` and the continuation rules are the mature
        # loop's evidence oracle.  The native policy must make the same
        # decision for every deterministic fixture row.
        expected = bool(
            case["expects_continuation"]
            and case["stop_reason"] == "end_turn"
            and gaps
            and case["max_continuations"] > 0
        )
        assert (continuation is not None) is expected
        if continuation is not None:
            assert continuation.reason == "completion_gate"
            assert all(gap in continuation.message.content for gap in gaps)


@pytest.mark.asyncio
async def test_completion_gate_event_fixture_matches_recoverable_pause_stream() -> None:
    from box_agent.api import ModelChunk
    from box_agent.kernel import AgentLoopKernel

    fixture = json.loads(
        COMPLETION_GATE_EVENT_FIXTURE_PATH.read_text(encoding="utf-8")
    )
    assert fixture["schema_version"] == 1
    for index, case in enumerate(fixture["cases"]):
        class LLM:
            async def stream(self, request):
                del request
                yield ModelChunk(content="done", finish_reason="end_turn")

        events = []
        result = await AgentLoopKernel(
            llm=LLM(),
            workflow_policy=CompletionGateWorkflowPolicy(
                CompletionGate(
                    required_tools=frozenset(case["required_tools"]),
                    max_continuations=case["max_continuations"],
                )
            ),
        ).run(
            RunRequest(
                request_id=f"completion-event-request-{index}",
                session_id=f"completion-event-session-{index}",
                turn_id=f"completion-event-turn-{index}",
                user_input=Message.user("finish"),
                options=RunOptions(max_steps=2),
            ),
            emit=events.append,
            cancel_event=asyncio.Event(),
        )

        assert [event.type for event in events] == case["expected_event_types"]
        assert result.stop_reason == case["expected_stop_reason"]
        assert case["expected_recoverable_text"] in result.final_message
        assert result.metadata["completionGate"]["budgetExhausted"] is case[
            "expected_budget_exhausted"
        ]


def test_autopilot_fixture_matches_native_terminal_contract() -> None:
    """Keep native Autopilot stop facts aligned with the legacy host fields."""

    cases = json.loads(AUTOPILOT_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(cases, list) and cases
    for index, case in enumerate(cases):
        store = GoalStore()
        store.set(
            f"autopilot-{index}",
            case["objective"],
            progress=list(case.get("progress", [])),
            evidence=list(case.get("evidence", [])),
        )
        policy = GoalWorkflowPolicy(
            store,
            session_id=f"autopilot-{index}",
            autopilot_enabled=case["enabled"],
            max_continuations=case["max_continuations"],
            no_progress_turns=case["no_progress_turns"],
        )
        for turn in range(case["turns"]):
            policy.next_continuation(
                stop_reason=case["stop_reason"],
                final_content="still working",
                step=turn + 1,
            )
        facts = policy.terminal_metadata(case["stop_reason"], "still working")[
            "goalAutopilot"
        ]
        expected = case["expected"]
        for key, value in expected.items():
            assert facts[key] == value


@pytest.mark.asyncio
async def test_autopilot_event_fixture_preserves_checkpointed_continuation_stream() -> None:
    from box_agent.api import ModelChunk
    from box_agent.kernel import AgentLoopKernel

    fixture = json.loads(
        AUTOPILOT_EVENT_FIXTURE_PATH.read_text(encoding="utf-8")
    )
    assert fixture["schema_version"] == 1
    for index, case in enumerate(fixture["cases"]):
        class LLM:
            def __init__(self) -> None:
                self.calls = 0

            async def stream(self, request):
                del request
                content = case["responses"][self.calls]
                self.calls += 1
                yield ModelChunk(content=content, finish_reason="end_turn")

        session_id = f"autopilot-event-parity-{index}"
        goals = GoalStore()
        goals.set(session_id, case["objective"])
        events = []
        result = await AgentLoopKernel(
            llm=LLM(),
            workflow_policy=GoalWorkflowPolicy(
                goals,
                session_id=session_id,
                autopilot_enabled=True,
                max_continuations=case["max_continuations"],
                no_progress_turns=case["no_progress_turns"],
            ),
        ).run(
            RunRequest(
                request_id=f"autopilot-event-request-{index}",
                session_id=session_id,
                turn_id=f"autopilot-event-turn-{index}",
                user_input=Message.user("continue"),
                options=RunOptions(max_steps=len(case["responses"])),
            ),
            emit=events.append,
            cancel_event=asyncio.Event(),
        )

        assert [event.type for event in events] == case["expected_event_types"]
        facts = result.metadata["goalAutopilot"]
        assert facts["stopCause"] == case["expected_stop_cause"]
        assert facts["continuations"] == case["expected_continuations"]
        assert facts["noProgressTurns"] == case["expected_no_progress_turns"]
        continuation_index = next(
            index
            for index, event in enumerate(events)
            if event.type == "workflow.continuation.requested"
        )
        checkpoint = events[continuation_index - 1]
        assert checkpoint.type == "workflow.checkpoint"
        assert checkpoint.payload["workflow_state"]["goal"]["autopilot"][
            "continuation_count"
        ] == 1


def test_skill_fixture_matches_native_context_and_bounded_checkpoint() -> None:
    """The Skill fixture fences prompt injection from durable state."""

    cases = json.loads(SKILL_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(cases, list) and cases
    for case in cases:
        loader = SkillLoader(".")
        loader.loaded_skills[case["skill_name"]] = Skill(
            name=case["skill_name"],
            description=case["description"],
            content=case["content"],
            source=case["skill_source"],  # type: ignore[arg-type]
        )
        policy = ExternalSkillRunPolicy(
            workspace_dir=".",
            artifact_root_dir="./output",
            skill_loader=loader,
        ).for_run(
            RunRequest(
                request_id=f"skill-fixture-{case['skill_name']}",
                session_id="skill-fixture",
                turn_id="turn-1",
                user_input=Message.user("continue"),
                options=RunOptions(
                    workflow_id="external_skill",
                    workflow_options={
                        "skill_name": case["skill_name"],
                        "skill_source": case["skill_source"],
                    },
                ),
            )
        )
        items = policy.context_items({"phase": "before_model"})
        assert any(
            item.kind == "skill" and case["context_fragment"] in item.content
            for item in items
        )
        checkpoint = policy.build_checkpoint_payload()["external_skill"]
        assert checkpoint["skill_content_hash"]
        if case["checkpoint_forbids_prompt"]:
            assert case["content"] not in str(checkpoint)


@pytest.mark.asyncio
async def test_skill_event_fixture_matches_native_pause_checkpoint_and_terminal() -> None:
    from box_agent.api import ModelChunk, ToolCallRequest
    from box_agent.kernel import AgentLoopKernel
    from box_agent.plugins import TypedRegistry
    from box_agent.tools.base import Tool, ToolResult
    from box_agent.tools.engine import RegistryToolEngine

    fixture = json.loads(SKILL_EVENT_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == 1
    for index, case in enumerate(fixture["cases"]):
        class LLM:
            async def stream(self, request):
                assert any(
                    "Ask for a finite user decision" in str(message.content)
                    for message in request.messages
                )
                yield ModelChunk(
                    tool_calls=(
                        ToolCallRequest(
                            call_id=case["tool_call_id"],
                            tool_name="request_user_decision",
                            arguments={"question": "Choose layout"},
                        ),
                    ),
                    finish_reason="tool_calls",
                )

        class DecisionTool(Tool):
            name = "request_user_decision"
            description = "Pause for one finite user decision."
            parameters = {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            }

            async def execute(self, **arguments):
                return ToolResult(
                    success=True,
                    content=f"requested: {arguments['question']}",
                )

        loader = SkillLoader(".")
        loader.loaded_skills[case["skill_name"]] = Skill(
            name=case["skill_name"],
            description="Ask for one finite user decision.",
            content="Ask for a finite user decision and resume after the answer.",
            source=case["skill_source"],  # type: ignore[arg-type]
        )
        request = RunRequest(
            request_id=f"skill-event-request-{index}",
            session_id=f"skill-event-session-{index}",
            turn_id=f"skill-event-turn-{index}",
            user_input=Message.user(case["prompt"]),
            options=RunOptions(
                workflow_id="external_skill",
                workflow_options={
                    "skill_name": case["skill_name"],
                    "skill_source": case["skill_source"],
                },
            ),
        )
        registry = TypedRegistry("tools")
        tool = DecisionTool()
        registry.register(tool.name, tool, source="skill-parity")
        events = []
        result = await AgentLoopKernel(
            llm=LLM(),
            tool_engine=RegistryToolEngine(registry),
            workflow_policy=ExternalSkillRunPolicy(
                workspace_dir=".",
                artifact_root_dir="./output",
                skill_loader=loader,
            ).for_run(request),
        ).run(
            request,
            emit=events.append,
            cancel_event=asyncio.Event(),
        )

        assert [event.type for event in events] == case["expected_event_types"]
        assert result.stop_reason == case["expected_stop_reason"]
        facts = result.metadata["externalSkill"]
        assert facts["paused"] is True
        assert facts["skillName"] == case["skill_name"]
        checkpoint = next(
            event.payload
            for event in events
            if event.type == "workflow.checkpoint"
        )
        state = checkpoint["workflow_state"]["external_skill"]
        assert state["paused"] is True
        assert state["stage"] == case["expected_stage"]


def test_presentation_fixture_matches_native_options_and_skill_context() -> None:
    cases = json.loads(PRESENTATION_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(cases, list) and cases
    for case in cases:
        loader = SkillLoader(".")
        loader.loaded_skills[case["skill_name"]] = Skill(
            name=case["skill_name"],
            description=case["description"],
            content=case["content"],
            source=case["skill_source"],  # type: ignore[arg-type]
        )
        policy = ControlledPresentationPolicy(
            workspace_dir=".",
            artifact_root_dir="./output",
            skill_loader=loader,
        ).for_run(
            RunRequest(
                request_id="presentation-fixture",
                session_id="presentation-fixture",
                turn_id="turn-1",
                user_input=Message.user("continue"),
                options=RunOptions(
                    workflow_id="controlled_presentation",
                    workflow_options={
                        "research_mode": case["research_mode"],
                        "research_round_limit": case["research_round_limit"],
                        "image_generation_policy": case["image_generation_policy"],
                    },
                ),
            )
        )
        assert policy.research_mode == case["research_mode"]
        assert policy.research_round_limit == case["research_round_limit"]
        assert policy.image_generation_policy == case["image_generation_policy"]
        assert any(
            item.kind == "skill" and case["context_fragment"] in item.content
            for item in policy.context_items({"phase": "before_model"})
        )


@pytest.mark.asyncio
async def test_presentation_event_fixture_runs_finalizer_before_model_and_publishes_artifact(
    tmp_path: Path,
) -> None:
    from box_agent.api import ModelChunk, ToolCallResult
    from box_agent.kernel import AgentLoopKernel
    from box_agent.workflows.presentation_checkpoint import (
        CONTROLLED_PRESENTATION_CHECKPOINT_MARKER,
    )

    fixture = json.loads(PRESENTATION_EVENT_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == 1
    for index, case in enumerate(fixture["cases"]):
        artifact_root = tmp_path / f"case-{index}" / "output"
        artifact_root.mkdir(parents=True)
        (artifact_root / "deck.json").write_text(
            json.dumps({"slides": []}),
            encoding="utf-8",
        )
        artifact_path = artifact_root / case["artifact_name"]

        class Policy(ControlledPresentationPolicy):
            def build_checkpoint(self):
                return (
                    f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}finalize\n"
                    "NEXT_ACTION=run the deterministic finalizer"
                )

        class Tools:
            def __init__(self) -> None:
                self.calls = []

            def schemas(self):
                return ({"name": "bash", "description": "run", "input_schema": {}},)

            def supports_workflow_action(self, tool_name, capability):
                return (
                    tool_name == "bash"
                    and capability == case["expected_capability"]
                )

            async def execute(self, call, *, context=None):
                del context
                self.calls.append(call)
                artifact_path.write_text("<html>deck</html>", encoding="utf-8")
                return ToolCallResult(
                    call_id=call.call_id,
                    status="succeeded",
                    content="finalized",
                    output={
                        "artifacts": [
                            {
                                "artifact_id": case["artifact_name"],
                                "kind": "html",
                                "uri": str(artifact_path),
                                "mime": "text/html",
                                "size": artifact_path.stat().st_size,
                            }
                        ]
                    },
                )

        class LLM:
            def __init__(self) -> None:
                self.requests = []

            async def stream(self, request):
                self.requests.append(request)
                yield ModelChunk(content="Presentation finalized.", finish_reason="stop")

        request = RunRequest(
            request_id=f"presentation-event-request-{index}",
            session_id=f"presentation-event-session-{index}",
            turn_id=f"presentation-event-turn-{index}",
            user_input=Message.user(case["prompt"]),
            options=RunOptions(
                max_steps=2,
                workflow_id="controlled_presentation",
            ),
            metadata={"workspace_dir": str(tmp_path / f"case-{index}")},
        )
        tools = Tools()
        llm = LLM()
        events = []
        result = await AgentLoopKernel(
            llm=llm,
            tool_engine=tools,
            workflow_policy=Policy(
                workspace_dir=str(tmp_path / f"case-{index}"),
                artifact_root_dir=artifact_root,
            ),
        ).run(
            request,
            emit=events.append,
            cancel_event=asyncio.Event(),
        )

        assert [event.type for event in events] == case["expected_event_types"]
        assert len(tools.calls) == 1
        assert tools.calls[0].metadata["workflow_capability"] == case[
            "expected_capability"
        ]
        assert len(llm.requests) == 1
        assert artifact_path.is_file()
        assert [artifact.uri for artifact in result.artifacts] == [str(artifact_path)]
        assert result.metadata["controlledPresentation"]["stage"] == case[
            "expected_stage"
        ]
