"""Executable checks for architecture and E2E documentation facts."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    ROOT / "docs" / "ARCHITECTURE_CN.md",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "docs" / "runtime-capability-matrix.md",
)


def _documented_paths(text: str) -> set[str]:
    paths: set[str] = set()
    for token in re.findall(r"`([^`]+)`", text):
        normalized = token.strip().replace("\\", "/")
        if normalized.startswith(("box_agent/", "docs/", "tests/")):
            # Tables sometimes document a module plus a prose suffix. Keep
            # only the path-shaped prefix for an existence check.
            match = re.match(
                r"((?:box_agent|docs|tests)/[^\s,;:)]+(?:\.py|\.md|\.json|\.html|/))",
                normalized,
            )
            if match:
                paths.add(match.group(1).rstrip("/"))
    return paths


def test_architecture_docs_reference_existing_paths() -> None:
    missing: list[str] = []
    for document in DOCS:
        for relative in _documented_paths(document.read_text(encoding="utf-8")):
            if not (ROOT / relative).exists():
                missing.append(f"{document.name}: {relative}")
    assert missing == []


def test_e2e_guide_documents_all_case_ids_and_report_command() -> None:
    guide = ROOT / "docs" / "e2e" / "ACP_E2E_GUIDE_CN.md"
    assert guide.is_file()
    text = guide.read_text(encoding="utf-8")
    for case_id in ("text", "tool_permission", "context_memory", "workflow_continuation", "resume"):
        assert f"`{case_id}`" in text
    assert "run_acp_cases.py" in text
    assert "report.html" in text
