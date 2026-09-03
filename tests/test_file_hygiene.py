"""Safety contract for evidence-based disposable-file cleanup."""

from __future__ import annotations

from pathlib import Path

from scripts.audit_unused_files import audit


ROOT = Path(__file__).resolve().parents[1]


def test_audit_marks_unreferenced_model_artifact_safe_to_delete(tmp_path: Path) -> None:
    # Exercise the rule with a temporary artifact so the repository can stay
    # clean after the one-time model output has been removed.
    (tmp_path / "output.png").write_bytes(b"generated")
    report = audit(tmp_path)
    output = next(item for item in report["candidates"] if item["path"] == "output.png")
    assert output["safe_to_delete"] is True
    assert output["reference_count"] == 0


def test_audit_prunes_repository_local_test_artifacts(tmp_path: Path) -> None:
    for directory in (".tmp-tests", ".uv-cache", ".venv-codex"):
        generated = tmp_path / directory / "run" / "output.png"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b"generated")

    report = audit(tmp_path)

    assert report["scanned_files"] == 0
    assert report["candidates"] == []


def test_audit_never_marks_compatibility_or_parity_paths_as_candidates() -> None:
    report = audit(ROOT)
    candidate_paths = {item["path"] for item in report["candidates"]}
    for protected in (
        "box_agent/agent.py",
        "box_agent/core.py",
        "box_agent/compat/agent.py",
        "tests/parity/migration_status.json",
        "tests/e2e/report.json",
    ):
        assert protected not in candidate_paths
