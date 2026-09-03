"""Executable dependency rules for the three-layer architecture."""

from __future__ import annotations

import ast
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "box_agent"
APPLICATION_ADAPTER_MODULES = ("box_agent.acp", "box_agent.cli", "acp", "cli")
STABLE_KERNEL_MODULES = (
    Path("kernel/loop.py"),
    Path("loop_guards.py"),
    Path("workflow_policy.py"),
)
PRESENTATION_WORKFLOW_TOKENS = (
    "controlled_presentation",
    "presentation_research_mode",
    "controlled_presentation_stage",
    "pptx",
    "powerpoint",
    "slide deck",
    "演示文稿",
    "幻灯片",
)


def _direct_core_imports(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "box_agent.core"
                or alias.name.startswith("box_agent.core.")
                for alias in node.names
            ):
                violations.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if (
                module == "box_agent.core"
                or module.startswith("box_agent.core.")
                or (node.level > 0 and (module == "core" or module.startswith("core.")))
                or (
                    module == "box_agent"
                    and any(alias.name == "core" for alias in node.names)
                )
                or (
                    node.level > 0
                    and not module
                    and any(alias.name == "core" for alias in node.names)
                )
            ):
                violations.append(node.lineno)
    return violations


def _is_application_adapter_module(name: str) -> bool:
    return any(
        name == module or name.startswith(f"{module}.")
        for module in APPLICATION_ADAPTER_MODULES
    )


def _application_adapter_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [module] if module else []
            if module in {"", "box_agent"}:
                names.extend(alias.name for alias in node.names)
        else:
            continue
        for name in names:
            if _is_application_adapter_module(name):
                violations.append(f"{name}:{node.lineno}")
    return violations


def test_no_production_module_imports_retired_core_implementation() -> None:
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        relative_path = path.relative_to(PACKAGE_ROOT)
        if relative_path == Path("core.py"):
            continue
        for lineno in _direct_core_imports(path):
            violations.append(f"{relative_path}:{lineno}")

    assert violations == [], (
        "All runtime paths must use api/kernel/services/capability modules; "
        f"the retired box_agent.core implementation cannot be imported: {violations}"
    )


def test_runtime_has_no_competing_session_log_recovery_owner() -> None:
    assert not (PACKAGE_ROOT / "session_log.py").exists()
    violations: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            if any(
                module == "box_agent.session_log"
                or module.endswith(".session_log")
                or module == "session_log"
                for module in modules
            ):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}")

    assert violations == [], (
        "SQLite event/checkpoint/effect stores are the sole recovery source; "
        f"a second append-only Session Log must not be imported: {violations}"
    )


def test_core_does_not_depend_on_application_adapters() -> None:
    core_path = PACKAGE_ROOT / "core.py"
    forbidden = _application_adapter_imports(core_path)
    assert forbidden == [], f"Core must not import application adapters: {forbidden}"


def test_host_adapters_do_not_import_other_host_packages() -> None:
    violations: list[str] = []
    for path in (PACKAGE_ROOT / "adapters").rglob("*.py"):
        relative = path.relative_to(PACKAGE_ROOT / "adapters")
        if relative.parts[0] == "acp" or relative == Path("acp_kernel.py"):
            continue
        for value in _application_adapter_imports(path):
            if value.startswith("box_agent.acp"):
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}:{value}")

    assert violations == [], (
        "Shared/CLI/SDK adapters must use capability modules rather than "
        f"depending on the ACP host package: {violations}"
    )


def test_core_is_a_compatibility_facade_not_an_execution_owner() -> None:
    core_path = PACKAGE_ROOT / "core.py"
    tree = ast.parse(core_path.read_text(encoding="utf-8"), filename=str(core_path))
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_modules.append(module)
            if module in {"", "box_agent"}:
                imported_modules.extend(alias.name for alias in node.names)

    source = core_path.read_text(encoding="utf-8")
    assert imported_modules == ["box_agent.compat.core"]
    assert "async def run_agent_loop" not in source
    assert "class AgentLoop" not in source


def test_stable_kernel_contains_no_concrete_presentation_workflow() -> None:
    violations: list[str] = []
    for relative_path in STABLE_KERNEL_MODULES:
        path = PACKAGE_ROOT / relative_path
        source = path.read_text(encoding="utf-8").lower()
        for token in PRESENTATION_WORKFLOW_TOKENS:
            if token in source:
                violations.append(f"{relative_path}:{token}")

    assert violations == [], (
        "Concrete PPT routing and checkpoint state belong under "
        f"box_agent.workflows, not the stable kernel: {violations}"
    )


def test_kernel_imports_no_concrete_workflow_implementation() -> None:
    violations: list[str] = []
    for path in (PACKAGE_ROOT / "kernel").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                if module == "box_agent.workflows" or module.startswith(
                    "box_agent.workflows."
                ):
                    violations.append(
                        f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}:{module}"
                    )

    assert violations == [], (
        "Kernel composition may depend on the Workflow SPI, not concrete "
        f"workflow implementations: {violations}"
    )


def test_host_adapters_share_builtin_workflow_composition() -> None:
    constructors = {
        "AttachmentInspectionPolicy",
        "BrowserIntentWorkflowPolicy",
        "CompositeWorkflowPolicy",
        "GoalWorkflowPolicy",
        "PlanWorkflowPolicy",
        "ResponseContinuationWorkflowPolicy",
    }
    violations: list[str] = []
    for relative_path in (
        Path("acp/kernel_runtime.py"),
        Path("adapters/cli/kernel_runtime.py"),
    ):
        path = PACKAGE_ROOT / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        duplicated = sorted(calls & constructors)
        if duplicated:
            violations.append(f"{relative_path}:{','.join(duplicated)}")

    assert violations == [], (
        "Host adapters must translate host concerns and delegate shared "
        f"workflow composition to the plugin bootstrap: {violations}"
    )


def test_acp_depends_on_generic_workflow_lifecycle_only() -> None:
    acp_path = PACKAGE_ROOT / "acp" / "__init__.py"
    tree = ast.parse(acp_path.read_text(encoding="utf-8"), filename=str(acp_path))
    concrete_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        concrete_imports.extend(
            module
            for module in modules
            if module.startswith("box_agent.workflows.presentation_")
        )

    source = acp_path.read_text(encoding="utf-8")
    assert concrete_imports == []
    assert '"controlled_presentation"' not in source


def test_application_adapter_detector_rejects_submodule_imports(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "forbidden_imports.py"
    sample.write_text(
        "import box_agent.acp.session\n"
        "from box_agent.acp.protocol import Request\n"
        "from .acp.transport import Connection\n"
        "from box_agent import cli\n"
        "from . import acp\n",
        encoding="utf-8",
    )

    assert _application_adapter_imports(sample) == [
        "box_agent.acp.session:1",
        "box_agent.acp.protocol:2",
        "acp.transport:3",
        "cli:4",
        "acp:5",
    ]
