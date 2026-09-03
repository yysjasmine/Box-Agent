import json
from pathlib import Path

from box_agent.artifacts import artifact_envelope
from box_agent.api import ArtifactPublishRequest
from box_agent.events import ArtifactEvent
from box_agent.task_context import TaskContext, normalize_task_id
from box_agent.task_registry import finish_task, register_artifact_revision
from box_agent.persistence import TaskRegistryArtifactProcessor


def _artifact(path: Path, *, rel_path: str = "report.md") -> ArtifactEvent:
    return ArtifactEvent(
        tool_call_id="tool-1",
        kind="document",
        filename=path.name,
        rel_path=rel_path,
        abs_path=str(path),
        uri=path.as_uri(),
        size=path.stat().st_size,
        produced_at="2026-08-21T00:00:00+00:00",
    )


def test_task_id_normalization_rejects_path_traversal() -> None:
    assert normalize_task_id(" task-1 ") == "task-1"
    assert normalize_task_id("../task-1") is None


def test_registry_keeps_artifact_id_stable_and_versions_content(tmp_path: Path) -> None:
    output = tmp_path / "output" / "tasks" / "task-1"
    output.mkdir(parents=True)
    file_path = output / "report.md"
    context = TaskContext(session_id="session-1", task_id="task-1", turn_id="turn-1")

    file_path.write_text("first", encoding="utf-8")
    first = register_artifact_revision(
        tmp_path,
        context,
        _artifact(file_path),
        artifact_root_dir=output,
    )
    file_path.write_text("second", encoding="utf-8")
    second = register_artifact_revision(
        tmp_path,
        TaskContext(session_id="session-1", task_id="task-1", turn_id="turn-2"),
        _artifact(file_path),
        artifact_root_dir=output,
    )
    finish_task(
        tmp_path,
        context,
        execution_status="completed",
        delivery_status="incomplete",
        artifact_root_dir=output,
    )

    assert first.artifact_id == second.artifact_id
    assert first.artifact_revision_id != second.artifact_revision_id
    record = json.loads(Path(second.manifest_path).read_text(encoding="utf-8"))
    assert record["delivery_status"] == "incomplete"
    assert len(record["artifacts"][0]["revisions"]) == 2


def test_artifact_envelope_exposes_canonical_lineage(tmp_path: Path) -> None:
    file_path = tmp_path / "report.md"
    file_path.write_text("content", encoding="utf-8")
    artifact = _artifact(file_path)
    context = TaskContext(session_id="session-1", task_id="task-1", turn_id="turn-1")
    lineage = register_artifact_revision(
        tmp_path,
        context,
        artifact,
        artifact_root_dir=tmp_path,
    )

    payload = artifact_envelope(
        artifact,
        str(tmp_path),
        session_id=context.session_id,
        task_id=context.task_id,
        turn_id=context.turn_id,
        lineage=lineage,
    )

    assert payload["artifact_id"] == lineage.artifact_id
    assert payload["artifact_revision_id"] == lineage.artifact_revision_id
    assert payload["task_id"] == "task-1"


def test_native_artifact_processor_persists_lineage_before_publication(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    file_path = output / "report.md"
    file_path.write_text("content", encoding="utf-8")

    payload = TaskRegistryArtifactProcessor().process(
        ArtifactPublishRequest(
            session_id="kernel-session",
            run_id="run-1",
            turn_id="turn-1",
            call_id="call-1",
            artifact={
                "kind": "document",
                "filename": file_path.name,
                "rel_path": "output/report.md",
                "abs_path": str(file_path),
                "uri": file_path.as_uri(),
                "size": file_path.stat().st_size,
            },
            metadata={
                "workspace_dir": str(tmp_path),
                "task_id": "task-1",
                "correlation_session_id": "office-session",
                "correlation_turn_id": "office-turn",
            },
        )
    )

    assert payload["artifact_id"].startswith("artifact_")
    assert payload["artifact_revision_id"].startswith("revision_")
    assert payload["session_id"] == "office-session"
    assert payload["task_id"] == "task-1"
    assert Path(payload["manifest_path"]).is_file()


def test_native_artifact_processor_records_run_terminal_state(tmp_path: Path) -> None:
    processor = TaskRegistryArtifactProcessor()
    request = type(
        "Request",
        (),
        {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "metadata": {
                "workspace_dir": str(tmp_path),
                "task_id": "task-1",
            },
        },
    )()

    processor.begin_run(request)
    processor.end_run(
        request,
        event_type="run.completed",
        payload={"stop_reason": "stop"},
    )

    record = json.loads(
        (
            tmp_path
            / ".box-agent"
            / "task-registry"
            / "tasks"
            / "task-1.json"
        ).read_text(encoding="utf-8")
    )
    assert record["execution_status"] == "completed"
    assert record["delivery_status"] == "complete"
