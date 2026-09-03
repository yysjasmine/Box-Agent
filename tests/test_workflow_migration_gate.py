"""Guard the explicit workflow parity/deletion gate."""

from __future__ import annotations

import json
import ast
from pathlib import Path


def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }

def test_kernel_promotion_evidence_covers_all_complex_workflows() -> None:
    root = Path(__file__).parent / "parity"
    status = json.loads((root / "migration_status.json").read_text(encoding="utf-8"))
    gap_matrix = json.loads(
        (root / "workflow_gap_matrix.json").read_text(encoding="utf-8")
    )
    required = set(status["kernel_promotion_requires"])
    workflows = status["workflows"]
    assert required == set(gap_matrix["workflows"])

    assert required == set(workflows)
    for workflow_id in required:
        entry = workflows[workflow_id]
        gaps = gap_matrix["workflows"][workflow_id]["remaining_gaps"]
        assert isinstance(gaps, list)
        fixture = entry.get("fixture")
        if entry["status"] == "parity_passed":
            assert isinstance(fixture, str) and (root / fixture).is_file()
            assert not gaps
        else:
            assert entry["status"] in {"legacy", "parity_candidate"}
            assert gaps

    assert all(
        workflows[workflow_id]["status"] == "parity_passed"
        for workflow_id in required
    )
    assert status["legacy_loop_retired"] is True
    assert status.get("runtime_removal_blockers") == []


def test_sub_agent_execution_no_longer_imports_the_legacy_loop() -> None:
    source = (
        Path(__file__).parents[1] / "box_agent" / "tools" / "sub_agent_tool.py"
    ).read_text(encoding="utf-8")

    assert "from ..runtime import run_agent_loop" not in source
    assert "from box_agent.runtime import run_agent_loop" not in source


def test_acp_monolith_replacement_matrix_points_to_real_tests() -> None:
    repository = Path(__file__).parents[1]
    matrix = json.loads(
        (repository / "tests" / "parity" / "acp_host_gap_matrix.json").read_text(
            encoding="utf-8"
        )
    )
    assert matrix["schema_version"] == 1
    assert not (repository / matrix["retired_suite"]).exists()
    assert matrix["categories"]
    for category, entry in matrix["categories"].items():
        assert entry["status"] in {"covered", "covered_with_intentional_change"}
        assert entry["behaviors"], category
        if entry["status"] == "covered_with_intentional_change":
            assert entry.get("intentional_change", "").strip(), category
        for node_id in entry["replacement_tests"]:
            relative_path, separator, function_name = node_id.partition("::")
            assert separator and function_name.startswith("test_"), node_id
            test_path = repository / relative_path
            assert test_path.is_file(), node_id
            assert function_name in _test_functions(test_path), node_id


def test_retired_core_loop_matrix_points_to_real_tests() -> None:
    repository = Path(__file__).parents[1]
    matrix = json.loads(
        (repository / "tests" / "parity" / "core_loop_gap_matrix.json").read_text(
            encoding="utf-8"
        )
    )
    assert matrix["schema_version"] == 1
    assert not (repository / matrix["retired_suite"]).exists()
    for category, entry in matrix["categories"].items():
        assert entry["status"] in {"covered", "covered_with_intentional_change"}
        if entry["status"] == "covered_with_intentional_change":
            assert entry.get("intentional_change", "").strip(), category
        for node_id in entry["replacement_tests"]:
            relative_path, separator, function_name = node_id.partition("::")
            assert separator and function_name.startswith("test_"), node_id
            test_path = repository / relative_path
            assert test_path.is_file(), node_id
            assert function_name in _test_functions(test_path), node_id
