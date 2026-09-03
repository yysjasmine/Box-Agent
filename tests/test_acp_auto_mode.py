"""Explicit session modes are deterministic Context Engine plugins."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from box_agent.context import ContextBuildRequest, SessionModeContextContributor


def _request(tmp_path: Path, **metadata) -> ContextBuildRequest:
    return ContextBuildRequest(
        items=(), token_budget=100_000, session_id="session-1",
        metadata={"workspace_dir": str(tmp_path), **metadata},
    )


def _contributor() -> SessionModeContextContributor:
    return SessionModeContextContributor(
        {
            "data_analysis": Path("box_agent/config/analysis_prompt.md").read_text(encoding="utf-8"),
            "code_agent": Path("box_agent/config/code_prompt.md").read_text(encoding="utf-8"),
        }
    )


def test_missing_mode_keeps_general_context_without_auto_classification(tmp_path):
    assert _contributor().provide(_request(tmp_path)) == ()


def test_workspace_execution_context_uses_artifact_mode_as_single_policy_source(
    tmp_path,
):
    seen: list[tuple[str, bool]] = []

    def sandbox_prompt(use_output_dir: bool) -> str:
        seen.append(("sandbox", use_output_dir))
        return f"sandbox-output={use_output_dir}"

    def delivery_prompt(use_output_dir: bool) -> str:
        seen.append(("delivery", use_output_dir))
        return f"delivery-output={use_output_dir}"

    contributor = SessionModeContextContributor(
        {},
        sandbox_prompt_builder=sandbox_prompt,
        file_delivery_prompt_builder=delivery_prompt,
    )

    project_items = contributor.provide(
        _request(tmp_path, artifact_mode="project")
    )
    output_items = contributor.provide(
        _request(tmp_path, artifact_mode="output")
    )

    assert seen == [
        ("sandbox", False),
        ("delivery", False),
        ("sandbox", True),
        ("delivery", True),
    ]
    assert "sandbox-output=False" in str(project_items[0].content)
    assert "delivery-output=False" in str(project_items[0].content)
    assert "sandbox-output=True" in str(output_items[0].content)
    assert "delivery-output=True" in str(output_items[0].content)
    assert all(item.pinned for item in (*project_items, *output_items))


def test_data_analysis_mode_contributes_plot_contract(tmp_path):
    items = _contributor().provide(_request(tmp_path, session_mode="data_analysis"))
    content = "\n".join(str(item.content) for item in items)
    assert "Interactive Chart Data Output" in content
    assert "<!--PLOT_DATA:" in content
    assert all(item.metadata["role"] == "system" for item in items)


def test_code_agent_mode_contributes_engineering_and_project_contract(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# Project Rules\n\n- Run focused tests before reporting done.\n",
        encoding="utf-8",
    )
    items = _contributor().provide(
        _request(tmp_path, session_mode="code_agent", artifact_mode="project")
    )
    content = "\n".join(str(item.content) for item in items)
    assert "Software Engineering Mode (code_agent)" in content
    assert "优先用 `rg` 定位" in content
    assert "Project Workspace Mode" in content
    assert "Do not create or use an `output/` folder" in content
    assert "Project Startup Context" in content
    assert "Run focused tests before reporting done." in content


def test_code_agent_project_context_reports_git_status(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    content = "\n".join(
        str(item.content)
        for item in _contributor().provide(
            _request(tmp_path, session_mode="code_agent", artifact_mode="project")
        )
    )
    assert "Git repository: yes" in content
    assert "Status: 1 changed entry" in content
    assert "?? README.md" in content
