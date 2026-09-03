from __future__ import annotations

import asyncio

import pytest
from dataclasses import replace

from box_agent.api import Message, ModelChunk, RunOptions, RunRequest
from box_agent.plugins import PluginHost
from box_agent.services.kernel import KernelAgentService
from box_agent.tools.base import ToolResult
from box_agent.tools.skill_loader import Skill, SkillLoader
from box_agent.workflows.controlled_presentation import ControlledPresentationPolicy
from box_agent.workflows.external_skill import ExternalSkillRunPolicy


def _request(*, workflow_options: dict[str, object] | None = None) -> RunRequest:
    return RunRequest(
        request_id="native-workflow-request",
        session_id="native-workflow-session",
        turn_id="native-workflow-turn",
        user_input=Message.user("continue"),
        options=RunOptions(
            workflow_id="external_skill",
            workflow_options=workflow_options or {},
        ),
        metadata={"workspace_dir": "."},
    )


def _loader() -> SkillLoader:
    loader = SkillLoader(".")
    loader.loaded_skills["vendor-skill"] = Skill(
        name="vendor-skill",
        description="Produce a deterministic artifact.",
        content="Use the approved artifact pipeline and publish the result.",
        source="user",
    )
    loader.loaded_skills["pptx"] = Skill(
        name="pptx",
        description="Author controlled presentations.",
        content="Follow the controlled deck contract and deterministic finalizer.",
        source="builtin",
    )
    return loader


def test_native_external_skill_injects_selected_skill_instructions() -> None:
    policy = ExternalSkillRunPolicy(
        workspace_dir=".",
        artifact_root_dir="./output",
        skill_loader=_loader(),
    )
    bound = policy.for_run(
        _request(
            workflow_options={
                "skill_name": "vendor-skill",
                "skill_source": "user",
                "task_text": "make the artifact",
            }
        )
    )

    items = bound.context_items({"phase": "before_model"})

    assert any(item.metadata.get("skill_name") == "vendor-skill" for item in items)
    assert any(
        "Use the approved artifact pipeline" in item.content for item in items
    )


def test_native_external_skill_checkpoint_keeps_instruction_identity_only() -> None:
    policy = ExternalSkillRunPolicy(
        workspace_dir=".",
        artifact_root_dir="./output",
        skill_loader=_loader(),
    ).for_run(
        _request(
            workflow_options={
                "skill_name": "vendor-skill",
                "skill_source": "user",
                "task_text": "make the artifact",
            }
        )
    )

    checkpoint = policy.build_checkpoint_payload()["external_skill"]

    assert checkpoint["options"]["skill_name"] == "vendor-skill"
    assert checkpoint["skill_content_hash"]
    assert "Use the approved artifact pipeline" not in str(checkpoint)


def test_native_external_skill_marks_instruction_drift_after_resume() -> None:
    loader = _loader()
    policy = ExternalSkillRunPolicy(
        workspace_dir=".",
        artifact_root_dir="./output",
        skill_loader=loader,
    ).for_run(
        _request(
            workflow_options={
                "skill_name": "vendor-skill",
                "skill_source": "user",
            }
        )
    )
    checkpoint = policy.build_checkpoint_payload()
    loader.loaded_skills["vendor-skill"].content = "Changed instructions."

    restored = ExternalSkillRunPolicy(
        workspace_dir=".",
        artifact_root_dir="./output",
        skill_loader=loader,
    ).for_run(
        _request(
            workflow_options={
                "skill_name": "vendor-skill",
                "skill_source": "user",
            }
        ),
        type("Bundle", (), {
            "events": (
                {
                    "type": "workflow.checkpoint",
                    "payload": {"workflow_state": checkpoint},
                },
            )
        })(),
    )

    skill_item = next(
        item for item in restored.context_items({}) if item.kind == "skill"
    )
    assert skill_item.metadata["skill_content_hash_mismatch"] is True


def test_native_external_skill_enforces_bounded_completion_and_terminal_facts() -> None:
    loader = SkillLoader(".")
    loader.loaded_skills["artifact-skill"] = Skill(
        name="artifact-skill",
        description="Create a pptx artifact.",
        content="Publish the finished pptx under output.",
        source="user",
    )
    policy = ExternalSkillRunPolicy(
        workspace_dir=".",
        artifact_root_dir="./output",
        skill_loader=loader,
    ).for_run(
        _request(
            workflow_options={
                "skill_name": "artifact-skill",
                "skill_source": "user",
            }
        )
    )

    for _ in range(3):
        continuation = policy.next_continuation(
            stop_reason="stop",
            final_content="done",
            step=1,
        )
        assert continuation is not None
    assert policy.next_continuation(
        stop_reason="stop", final_content="done", step=1
    ) is None
    terminal = policy.terminal_metadata("stop", "done")["externalSkill"]
    assert terminal["continuations"] == 3
    assert terminal["gaps"]
    assert policy.terminal_message("stop", "done")


def test_native_external_skill_tool_budget_and_user_pause_are_plugin_owned() -> None:
    policy = ExternalSkillRunPolicy(
        workspace_dir=".",
        artifact_root_dir="./output",
        skill_loader=_loader(),
    ).for_run(_request())
    assert policy.tool_call_error(
        "bash", {}, verified_evidence_urls=set()
    ) is None
    policy._completion_gate = replace(policy._completion_gate, max_tool_calls=1)
    assert "budget exhausted" in policy.tool_call_error(
        "bash", {}, verified_evidence_urls=set()
    )
    result = ToolResult(success=True, content="decision requested")
    policy.record_tool_result(
        "request_user_decision", {}, result
    )
    assert policy.pause_after_tool("request_user_decision", result)


def test_native_external_skill_reuses_artifact_baseline_after_resume(tmp_path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    baseline = output / "before.pptx"
    baseline.write_bytes(b"before")
    loader = SkillLoader(".")
    loader.loaded_skills["artifact-skill"] = Skill(
        name="artifact-skill",
        description="Create a pptx artifact.",
        content="Publish the finished pptx under output.",
        source="user",
    )
    request = RunRequest(
        request_id="artifact-baseline",
        session_id="artifact-baseline-session",
        turn_id="artifact-baseline-turn",
        user_input=Message.user("continue"),
        options=RunOptions(
            workflow_id="external_skill",
            workflow_options={"skill_name": "artifact-skill", "skill_source": "user"},
        ),
        metadata={"workspace_dir": str(tmp_path)},
    )
    policy = ExternalSkillRunPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        skill_loader=loader,
    ).for_run(request)
    checkpoint = policy.build_checkpoint_payload()
    baseline.write_bytes(b"after")
    restored = ExternalSkillRunPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=output,
        skill_loader=loader,
    ).for_run(
        request,
        {"events": ({"type": "workflow.checkpoint", "payload": {"workflow_state": checkpoint}},)},
    )
    assert str(baseline.resolve()) in restored._completion_gate.baseline_artifact_signatures


@pytest.mark.asyncio
async def test_service_kernel_sends_selected_skill_context_to_model() -> None:
    class LLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ModelChunk(content="done", finish_reason="stop")

    llm = LLM()
    policy = ExternalSkillRunPolicy(
        workspace_dir=".",
        artifact_root_dir="./output",
        skill_loader=_loader(),
    )
    host = PluginHost()
    host.registries["llm"].register("default", llm, source="test")
    host.registries["workflows"].register(
        "external_skill", policy, source="test"
    )
    service = KernelAgentService.from_plugin_host(host)
    await service.open_session(
        type("Session", (), {"session_id": "native-workflow-session", "metadata": {}})()
    )
    handle = await service.start(
        _request(
            workflow_options={
                "skill_name": "vendor-skill",
                "skill_source": "user",
            }
        )
    )
    result = await handle.wait()

    assert result.status == "completed"
    assert any(
        "Use the approved artifact pipeline" in str(message.content)
        for message in llm.requests[0].messages
    )


@pytest.mark.asyncio
async def test_service_kernel_sends_registered_pptx_context_to_model() -> None:
    class LLM:
        def __init__(self) -> None:
            self.requests = []

        async def stream(self, request):
            self.requests.append(request)
            yield ModelChunk(content="done", finish_reason="stop")

    llm = LLM()
    policy = ControlledPresentationPolicy(
        workspace_dir=".",
        artifact_root_dir="./output",
        skill_loader=_loader(),
    )
    host = PluginHost()
    host.registries["llm"].register("default", llm, source="test")
    host.registries["workflows"].register(
        "controlled_presentation", policy, source="test"
    )
    service = KernelAgentService.from_plugin_host(host)
    await service.open_session(
        type("Session", (), {"session_id": "ppt-context-session", "metadata": {}})()
    )
    request = RunRequest(
        request_id="ppt-context-request",
        session_id="ppt-context-session",
        turn_id="ppt-context-turn",
        user_input=Message.user("continue"),
        options=RunOptions(
            workflow_id="controlled_presentation",
            workflow_options={"research_mode": "deep"},
        ),
        metadata={"workspace_dir": "."},
    )
    result = await (await service.start(request)).wait()

    assert result.status == "completed"
    assert any(
        "controlled deck contract" in str(message.content)
        for message in llm.requests[0].messages
    )


@pytest.mark.asyncio
async def test_service_kernel_persists_external_skill_terminal_facts(tmp_path) -> None:
    class LLM:
        async def stream(self, request):
            del request
            yield ModelChunk(content="done", finish_reason="stop")

    loader = SkillLoader(".")
    loader.loaded_skills["artifact-skill"] = Skill(
        name="artifact-skill",
        description="Create a pptx artifact.",
        content="Publish the finished pptx under output.",
        source="user",
    )
    host = PluginHost()
    host.registries["llm"].register("default", LLM(), source="test")
    host.registries["workflows"].register(
        "external_skill",
        ExternalSkillRunPolicy(
            workspace_dir=str(tmp_path),
            artifact_root_dir=tmp_path / "output",
            skill_loader=loader,
        ),
        source="test",
    )
    service = KernelAgentService.from_plugin_host(host)
    await service.open_session(
        type("Session", (), {"session_id": "skill-terminal-session", "metadata": {}})()
    )
    request = RunRequest(
        request_id="skill-terminal-request",
        session_id="skill-terminal-session",
        turn_id="skill-terminal-turn",
        user_input=Message.user("continue"),
        options=RunOptions(
            workflow_id="external_skill",
            max_steps=8,
            workflow_options={"skill_name": "artifact-skill", "skill_source": "user"},
        ),
        metadata={"workspace_dir": str(tmp_path)},
    )
    result = await (await service.start(request)).wait()

    assert result.status == "completed"
    assert result.metadata["externalSkill"]["continuations"] == 3
    assert result.final_message.startswith("The Skill reached")


def test_native_presentation_applies_run_workflow_options() -> None:
    policy = ControlledPresentationPolicy(
        workspace_dir=".",
        artifact_root_dir="./output",
    )
    bound = policy.for_run(
        RunRequest(
            request_id="presentation-request",
            session_id="presentation-session",
            turn_id="presentation-turn",
            user_input=Message.user("continue"),
            options=RunOptions(
                workflow_id="controlled_presentation",
                workflow_options={
                    "research_mode": "deep",
                    "research_round_limit": 2,
                    "image_generation_policy": "forbidden_by_user",
                },
            ),
        )
    )

    assert bound.research_mode == "deep"
    assert bound.research_round_limit == 2
    assert bound.image_generation_policy == "forbidden_by_user"


def test_native_presentation_injects_registered_pptx_skill() -> None:
    policy = ControlledPresentationPolicy(
        workspace_dir=".",
        artifact_root_dir="./output",
        skill_loader=_loader(),
    )

    items = policy.context_items({"phase": "before_model"})

    assert any(item.metadata.get("skill_name") == "pptx" for item in items)
    assert any("controlled deck contract" in item.content for item in items)
    checkpoint = policy.build_checkpoint_payload()["controlled_presentation"]
    assert checkpoint["skill_content_hash"]


def test_native_presentation_exposes_budget_pause_and_terminal_facts() -> None:
    policy = ControlledPresentationPolicy(
        workspace_dir=".",
        artifact_root_dir="./output",
    )
    assert policy.max_tool_calls > 0
    result = ToolResult(success=True, content="decision requested")
    assert policy.pause_after_tool("request_user_decision", result)
    policy.repair_stalled = True
    metadata = policy.terminal_metadata("failed", "")
    assert metadata["controlledPresentation"]["repairStalled"] is True
    assert policy.terminal_message("failed", "")


@pytest.mark.asyncio
async def test_native_kernel_drives_presentation_repair_fuse_to_durable_terminal(
    tmp_path,
) -> None:
    from box_agent.api import ErrorCode, ErrorInfo, ToolCallRequest, ToolCallResult
    from box_agent.kernel import AgentLoopKernel
    from box_agent.workflows.presentation_checkpoint import (
        CONTROLLED_PRESENTATION_CHECKPOINT_MARKER,
    )

    class Policy(ControlledPresentationPolicy):
        def build_checkpoint(self):
            return (
                f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}deck_spec_repair\n"
                "REPAIR_INPUT={}\n"
                "NEXT_ACTION=write deck.patch.json"
            )

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
                            call_id=f"repair-{self.calls}",
                            tool_name="write_file",
                            arguments={
                                "path": "deck.patch.json",
                                "content": "{}",
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                )
                return
            yield ModelChunk(content="repair stopped", finish_reason="stop")

    class Tools:
        def schemas(self):
            return (
                {
                    "name": "write_file",
                    "description": "write",
                    "input_schema": {},
                },
            )

        async def execute(self, call, *, context=None):
            del context
            return ToolCallResult(
                call_id=call.call_id,
                status="failed",
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL_ERROR,
                    category="tool",
                    message="same repair failure",
                ),
            )

    request = RunRequest(
        request_id="presentation-repair-request",
        session_id="presentation-repair-session",
        turn_id="presentation-repair-turn",
        user_input=Message.user("repair the deck"),
        options=RunOptions(max_steps=3, workflow_id="controlled_presentation"),
        metadata={"workspace_dir": str(tmp_path)},
    )
    events = []
    result = await AgentLoopKernel(
        llm=LLM(),
        tool_engine=Tools(),
        workflow_policy=Policy(
            workspace_dir=str(tmp_path),
            artifact_root_dir=tmp_path / "output",
        ),
    ).run(
        request,
        emit=events.append,
        cancel_event=asyncio.Event(),
    )

    assert result.status == "completed"
    assert result.stop_reason == "checkpoint_paused"
    assert result.metadata["controlledPresentation"]["repairStalled"] is True
    assert result.final_message.startswith("The presentation workflow stopped")
    checkpoint = next(
        event.payload
        for event in reversed(events)
        if event.type == "workflow.checkpoint"
    )
    assert checkpoint["workflow_state"]["controlled_presentation"]["flags"][
        "repair_stalled"
    ] is True


def test_public_runtime_consumes_skill_loader_at_composition_boundary(monkeypatch) -> None:
    """A Skill loader belongs to policy composition at the compatibility edge."""

    import box_agent.compat.runtime as runtime

    composed: dict[str, object] = {}

    def fake_policy(**kwargs):
        composed.update(kwargs)
        return None

    loader = _loader()
    monkeypatch.setattr(runtime, "create_workflow_policy", fake_policy)
    runtime._workflow_from_kwargs({"tools": {}, "skill_loader": loader})

    assert composed["skill_loader"] is loader
