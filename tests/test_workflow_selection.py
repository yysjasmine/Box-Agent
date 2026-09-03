from __future__ import annotations

from box_agent.plugins import PluginHost
from box_agent.tools.skill_loader import Skill, SkillLoader
from box_agent.workflows import (
    CompletionGateWorkflowSelector,
    PresentationWorkflowSelector,
    WorkflowSelectorChain,
    workflow_selector_from_registry,
)


def _loader() -> SkillLoader:
    loader = SkillLoader(".")
    loader.loaded_skills["pptx"] = Skill(
        name="pptx",
        description="Create editable PowerPoint presentations.",
        content="Follow the controlled presentation workflow.",
        source="builtin",
        workflow="controlled_presentation",
        capabilities=["presentation.authoring"],
    )
    return loader


def test_selector_chain_uses_registered_priority_and_falls_through() -> None:
    class Never:
        def select(self, prompt, metadata):
            del prompt, metadata
            return None

    class Vendor:
        def select(self, prompt, metadata):
            del prompt, metadata
            return {
                "workflow_id": "vendor.workflow",
                "workflow_options": {"mode": "safe"},
            }

    host = PluginHost()
    host.registries["workflow.selectors"].register(
        "vendor", Vendor(), source="vendor", priority=10
    )
    host.registries["workflow.selectors"].register(
        "never", Never(), source="builtin", priority=100
    )

    selector = workflow_selector_from_registry(host)

    assert isinstance(selector, WorkflowSelectorChain)
    assert selector("do the work", {}) == {
        "workflow_id": "vendor.workflow",
        "workflow_options": {"mode": "safe"},
    }


def test_presentation_selector_returns_data_only_native_workflow_options(tmp_path) -> None:
    selector = PresentationWorkflowSelector(
        workspace_dir=tmp_path,
        skill_loader=_loader(),
    )

    selection = selector.select("Create a six-slide presentation", {})

    assert selection is not None
    assert selection["workflow_id"] == "controlled_presentation"
    assert selection["workflow_options"]["presentation_skill_name"] == "pptx"
    assert selection["workflow_options"]["research_mode"] in {
        "auto",
        "targeted",
        "deep",
    }


def test_explicit_host_workflow_is_not_overridden_by_implicit_selector(tmp_path) -> None:
    selector = PresentationWorkflowSelector(
        workspace_dir=tmp_path,
        skill_loader=_loader(),
    )

    assert selector.select(
        "Create a presentation",
        {"workflow_id": "vendor.workflow"},
    ) is None


def test_generic_artifact_request_selects_native_completion_gate(tmp_path) -> None:
    from box_agent.completion import build_auto_completion_gate

    selection = CompletionGateWorkflowSelector(
        tmp_path,
        build_auto_completion_gate,
    ).select(
        "Create a Markdown research report artifact",
        {},
    )

    assert selection is not None
    assert selection["workflow_id"] == "completion_gate"
    assert selection["workflow_options"]["completion_gate"][
        "required_changed_artifact_globs"
    ]
