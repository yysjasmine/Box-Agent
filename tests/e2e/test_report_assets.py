"""Static checks for the browser report assets."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.run_acp_cases import write_report


ROOT = Path(__file__).resolve().parent


def test_report_assets_reference_each_other_and_all_cases() -> None:
    html = (ROOT / "report.html").read_text(encoding="utf-8")
    css = (ROOT / "report.css").read_text(encoding="utf-8")
    js = (ROOT / "report.js").read_text(encoding="utf-8")
    assert 'href="report.css"' in html
    assert 'src="report.js"' in html
    assert "report.json" in js
    assert "empty-state" in html and "setEmpty" in js
    for case_id in ("text", "tool_permission", "context_memory", "workflow_continuation", "resume"):
        assert case_id in html or case_id in js
    assert "prefers-reduced-motion" in css
    assert "aria-label" in html


def test_report_writer_rejects_non_json_targets(tmp_path: Path) -> None:
    target = tmp_path / "report.html"

    with pytest.raises(ValueError, match=r"\.json"):
        write_report(target, {"cases": []})

    assert not target.exists()
