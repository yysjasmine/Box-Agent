"""Regression tests for capability-oriented import paths.

The identity checks prove that stable root imports do not create a second
implementation beside the canonical capability modules.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
from pathlib import Path

import pytest


_ROOT_CAPABILITY_FACADES = {
    "artifacts",
    "cache_fingerprint",
    "cli_memory_proposal",
    "cli_permissions",
    "completion",
    "context_resources",
    "delivery",
    "evidence",
    "events",
    "execution_profile",
    "experts",
    "hooks",
    "kernel_service",
    "logger",
    "loop_guards",
    "memory",
    "memory_maintainer",
    "model_history",
    "retry",
    "roadmap_artifacts",
    "session_continuation",
    "session_trace",
    "task_context",
    "task_registry",
    "tool_result_storage",
    "turn_policy",
    "workflow_checkpoint_store",
    "workflow_owner_store",
    "workflow_policy",
    "workspace_registry",
}


def _module_name(package: Path, path: Path) -> str:
    relative = path.relative_to(package).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("box_agent", *parts))


def _resolved_imports(package: Path, path: Path) -> list[tuple[str, int]]:
    """Resolve absolute and relative imports to their canonical module name."""

    module_name = _module_name(package, path)
    import_package = (
        module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((alias.name, node.lineno) for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if node.level:
            module = importlib.util.resolve_name(
                f"{'.' * node.level}{module}", import_package
            )
        if module:
            imports.append((module, node.lineno))
        else:
            imports.extend(
                (f"{import_package}.{alias.name}", node.lineno)
                for alias in node.names
            )
    return imports


@pytest.mark.parametrize(
    ("stable_name", "canonical_name"),
    (
        ("experts", "context.experts"),
        ("evidence", "context.evidence"),
        ("events", "compat.events"),
        ("hooks", "compat.hooks"),
        ("retry", "llm.retry"),
        ("logger", "observability.logger"),
        ("session_trace", "observability.session_trace"),
        ("cache_fingerprint", "observability.cache_fingerprint"),
        ("cli_memory_proposal", "adapters.cli.memory_proposal_impl"),
        ("cli_permissions", "adapters.cli.permission_broker_impl"),
        ("context_resources", "context.resource_ledger"),
        ("artifacts", "persistence.artifacts"),
        ("completion", "workflows.completion"),
        ("delivery", "workflows.delivery"),
        ("execution_profile", "workflows.execution_profile"),
        ("loop_guards", "workflows.guards"),
        ("roadmap_artifacts", "persistence.roadmap_artifacts"),
        ("turn_policy", "workflows.turn_policy"),
        ("kernel_service", "services.kernel"),
        ("memory", "memory_engine.store"),
        ("memory_maintainer", "memory_engine.maintenance"),
        ("model_history", "context.model_history"),
        ("session_continuation", "persistence.session_continuation"),
        ("task_context", "context.task"),
        ("task_registry", "persistence.task_registry"),
        ("tool_result_storage", "tools.result_storage"),
        ("workflow_checkpoint_store", "persistence.workflow_checkpoint_store"),
        ("workflow_owner_store", "persistence.workflow_owner_store"),
        ("workflow_policy", "workflows.contract"),
        ("workspace_registry", "persistence.workspace_registry"),
    ),
)
def test_root_capability_modules_alias_their_canonical_owners(
    stable_name: str,
    canonical_name: str,
) -> None:
    stable = importlib.import_module(f"box_agent.{stable_name}")
    canonical = importlib.import_module(f"box_agent.{canonical_name}")
    assert stable is canonical


def test_internal_modules_do_not_depend_on_root_capability_facades() -> None:
    package = Path(__file__).resolve().parents[1] / "box_agent"
    violations: list[str] = []
    for path in package.rglob("*.py"):
        if path.parent == package and path.stem in _ROOT_CAPABILITY_FACADES:
            continue
        for module, lineno in _resolved_imports(package, path):
            if not module.startswith("box_agent."):
                continue
            name = module.removeprefix("box_agent.").split(".", 1)[0]
            if name in _ROOT_CAPABILITY_FACADES:
                violations.append(f"{path.relative_to(package)}:{lineno}:{module}")
    assert violations == []


def test_workflow_spi_has_one_protocol_identity() -> None:
    public_api = importlib.import_module("box_agent.api")
    workflow_contract = importlib.import_module("box_agent.workflows.contract")

    assert public_api.WorkflowPolicy is workflow_contract.WorkflowPolicy


def test_workflow_compositor_has_one_implementation_identity() -> None:
    compatibility_module = importlib.import_module("box_agent.workflows.composite")
    kernel_module = importlib.import_module("box_agent.kernel.workflow_composite")

    assert compatibility_module is kernel_module
    assert (
        compatibility_module.CompositeWorkflowPolicy
        is kernel_module.CompositeWorkflowPolicy
    )


def test_context_provider_spi_has_one_protocol_identity() -> None:
    public_api = importlib.import_module("box_agent.api")
    context_api = importlib.import_module("box_agent.context.api")

    assert public_api.ContextProvider is context_api.ContextProvider


def test_compatibility_facades_forward_legacy_objects() -> None:
    legacy_events = importlib.import_module("box_agent.events")
    compat_events = importlib.import_module("box_agent.compat.events")
    assert compat_events.AgentEvent is legacy_events.AgentEvent

    legacy_policy = importlib.import_module("box_agent.workflow_policy")
    compat_policy = importlib.import_module("box_agent.compat.workflow_policy")
    canonical_policy = importlib.import_module("box_agent.workflows.contract")
    assert compat_policy.WorkflowPolicy is legacy_policy.WorkflowPolicy
    assert legacy_policy.WorkflowPolicy is canonical_policy.WorkflowPolicy
    assert legacy_policy.WorkflowAction is canonical_policy.WorkflowAction

    legacy_agent = importlib.import_module("box_agent.agent")
    compat_agent = importlib.import_module("box_agent.compat.agent")
    goal_workflow = importlib.import_module("box_agent.workflows.goal")
    assert compat_agent.Agent is legacy_agent.Agent
    assert legacy_agent.GoalState is goal_workflow.GoalState
    assert legacy_agent.goal_payload is goal_workflow.goal_payload
    assert (
        legacy_agent.goal_state_from_payload
        is goal_workflow.goal_state_from_payload
    )
    assert (
        legacy_agent.goal_autopilot_prompt
        is goal_workflow.goal_autopilot_prompt
    )
    assert (
        legacy_agent.goal_autopilot_progress_signature
        is goal_workflow.goal_autopilot_progress_signature
    )

    legacy_runtime = importlib.import_module("box_agent.runtime")
    compat_runtime = importlib.import_module("box_agent.compat.runtime")
    assert compat_runtime.run_agent_loop is legacy_runtime.run_agent_loop

    legacy_core = importlib.import_module("box_agent.core")
    compat_core = importlib.import_module("box_agent.compat.core")
    assert compat_core.run_agent_loop is legacy_core.run_agent_loop

    legacy_task = importlib.import_module("box_agent.task_context")
    canonical_task = importlib.import_module("box_agent.context.task")
    assert legacy_task.TaskContext is canonical_task.TaskContext
    assert legacy_task.normalize_task_id is canonical_task.normalize_task_id

    legacy_history = importlib.import_module("box_agent.model_history")
    canonical_history = importlib.import_module("box_agent.context.model_history")
    assert (
        legacy_history.is_model_history_placeholder
        is canonical_history.is_model_history_placeholder
    )

    memory = importlib.import_module("box_agent.memory")
    memory_store = importlib.import_module("box_agent.memory_engine.store")
    assert memory is memory_store
    maintenance = importlib.import_module("box_agent.memory_maintainer")
    canonical_maintenance = importlib.import_module(
        "box_agent.memory_engine.maintenance"
    )
    assert maintenance is canonical_maintenance

    migrated_modules = (
        ("session_continuation", "persistence.session_continuation", "SessionContinuation"),
        ("task_registry", "persistence.task_registry", "ArtifactLineage"),
        ("workspace_registry", "persistence.workspace_registry", "WorkspaceRegistry"),
        (
            "workflow_checkpoint_store",
            "persistence.workflow_checkpoint_store",
            "WorkflowPauseCheckpoint",
        ),
        ("workflow_owner_store", "persistence.workflow_owner_store", "WorkflowOwner"),
    )
    for legacy_name, canonical_name, symbol in migrated_modules:
        legacy_module = importlib.import_module(f"box_agent.{legacy_name}")
        canonical_module = importlib.import_module(f"box_agent.{canonical_name}")
        legacy_value = getattr(legacy_module, symbol)
        canonical_value = getattr(canonical_module, symbol)
        assert legacy_value is canonical_value
        assert canonical_value.__module__ == canonical_module.__name__


def test_host_adapters_forward_existing_implementations() -> None:
    root_cli = importlib.import_module("box_agent.cli")
    adapter_cli = importlib.import_module("box_agent.adapters.cli.app")
    assert root_cli is adapter_cli

    permission_impl = importlib.import_module(
        "box_agent.adapters.cli.permission_broker_impl"
    )
    memory_impl = importlib.import_module(
        "box_agent.adapters.cli.memory_proposal_impl"
    )
    legacy_permissions = importlib.import_module("box_agent.cli_permissions")
    cli_permissions = importlib.import_module(
        "box_agent.adapters.cli.permission_broker"
    )
    assert cli_permissions.CLIPermissionNegotiator is legacy_permissions.CLIPermissionNegotiator
    assert cli_permissions.CLIPermissionNegotiator is permission_impl.CLIPermissionNegotiator
    assert cli_permissions.CLIPermissionNegotiator.__module__ == permission_impl.__name__

    legacy_memory = importlib.import_module("box_agent.cli_memory_proposal")
    cli_memory = importlib.import_module("box_agent.adapters.cli.memory_proposal")
    assert cli_memory.CLIMemoryProposalNegotiator is legacy_memory.CLIMemoryProposalNegotiator
    assert cli_memory.CLIMemoryProposalNegotiator is memory_impl.CLIMemoryProposalNegotiator
    assert cli_memory.CLIMemoryProposalNegotiator.__module__ == memory_impl.__name__

    acp = importlib.import_module("box_agent.adapters.acp_kernel")
    acp_package = importlib.import_module("box_agent.adapters.acp")
    assert acp_package.KernelACPAgent is acp.KernelACPAgent
    assert acp_package.ACPPermissionGateway is acp.ACPPermissionGateway

    compat_cli = importlib.import_module("box_agent.compat.cli")
    assert compat_cli.__all__ == ["main"]
    assert compat_cli._TARGET == "box_agent.adapters.cli.app"
    compat_acp = importlib.import_module("box_agent.compat.acp")
    assert compat_acp._TARGET == "box_agent.acp"

    compat_policy = importlib.import_module("box_agent.compat.workflow_policy")
    assert compat_policy._TARGET == "box_agent.workflows.contract"


def test_scriptable_cli_goal_commands_use_the_canonical_store() -> None:
    """The CLI adapter must not own a second Goal state machine."""

    cli = importlib.import_module("box_agent.cli")
    goal_workflow = importlib.import_module("box_agent.workflows.goal")

    assert cli.GoalStore is goal_workflow.GoalStore
    source = inspect.getsource(cli.cmd_goal)
    assert "GoalStore()" in source
    assert "GoalState(" not in source
    for direct_mutation in (
        "goal.status =",
        "goal.blocked_reason =",
        "goal.completed_by =",
        "goal.updated_at =",
    ):
        assert direct_mutation not in source
