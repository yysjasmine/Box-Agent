import json
import os

import pytest

from box_agent.tools.base import ToolResult
from box_agent.tools.file_tools import WriteTool
from box_agent.workflows.controlled_presentation import ControlledPresentationPolicy
from box_agent.workflows.presentation_checkpoint import (
    CONTROLLED_PRESENTATION_CHECKPOINT_MARKER,
)


@pytest.mark.asyncio
async def test_controlled_patch_chunk_transaction_survives_filesystem_checkpoint(
    tmp_path,
):
    """A pending atomic patch write must not look like a missing patch."""
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "cover-hero-v1",
                        "source_outline_page": 1,
                        "props": {"title": "输入演示标题"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )
    initial = policy.build_checkpoint()
    assert initial is not None
    policy.update_checkpoint(initial)
    assert policy.stage == "content_patch"

    tool = WriteTool(
        workspace_dir=str(tmp_path),
        relative_root_dir=str(output),
    )
    first_arguments = {
        "path": "deck.patch.json",
        "content": '{"slides":',
        "chunk_index": 0,
        "final": False,
    }
    accepted = await tool.execute(**first_arguments)
    assert accepted.success is True
    assert not (output / "deck.patch.json").exists()
    policy.record_tool_result("write_file", first_arguments, accepted)

    pending = policy.build_checkpoint()

    assert pending is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}content_patch" in pending
    assert "patch=write_pending" in pending
    assert 'WRITE_PENDING={"path":"deck.patch.json","next_chunk_index":1' in pending
    assert "Do not restart at chunk_index=0" in pending
    policy.update_checkpoint(pending)
    blocked_restart = policy.tool_call_error(
        "write_file",
        {
            "path": "deck.patch.json",
            "content": '{"slides":{"slide-01":{}}}',
            "chunk_index": 0,
            "final": True,
        },
        verified_evidence_urls=set(),
    )
    assert blocked_restart is not None
    assert "CONTROLLED_PRESENTATION_WRITE_TRANSACTION_PENDING" in blocked_restart
    assert (
        policy.tool_call_error(
            "write_file",
            {
                "path": "deck.patch.json",
                "content": "{}}",
                "chunk_index": 1,
                "final": True,
            },
            verified_evidence_urls=set(),
        )
        is None
    )

    final_arguments = {
        "path": "deck.patch.json",
        "content": "{}}",
        "chunk_index": 1,
        "final": True,
    }
    committed = await tool.execute(**final_arguments)
    assert committed.success is True
    policy.record_tool_result("write_file", final_arguments, committed)
    complete = policy.build_checkpoint()
    assert complete is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}apply_patch" in complete
    assert "WRITE_PENDING=" not in complete


@pytest.mark.asyncio
async def test_controlled_patch_can_restart_after_final_chunk_validation_failure(
    tmp_path,
):
    """A discarded final chunk must clear policy state for a chunk-zero retry."""
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "cover-hero-v1",
                        "source_outline_page": 1,
                        "props": {"title": "输入演示标题"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )
    initial = policy.build_checkpoint()
    assert initial is not None
    policy.update_checkpoint(initial)
    assert policy.stage == "content_patch"

    tool = WriteTool(
        workspace_dir=str(tmp_path),
        relative_root_dir=str(output),
    )
    first_arguments = {
        "path": "deck.patch.json",
        "content": '{"slides":{"slide-01":{"props":{"title":"safe",',
        "chunk_index": 0,
        "final": False,
    }
    accepted = await tool.execute(**first_arguments)
    assert accepted.success is True
    policy.record_tool_result("write_file", first_arguments, accepted)

    final_arguments = {
        "path": "deck.patch.json",
        "content": (
            '"notes":"await window.domToPptx.exportToPptx([]); '
            'require(\\"./dom-to-pptx.bundle.js\\");"}}}}'
        ),
        "chunk_index": 1,
        "final": True,
    }
    assert (
        policy.tool_call_error(
            "write_file",
            final_arguments,
            verified_evidence_urls=set(),
        )
        is None
    )
    blocked = await tool.execute(**final_arguments)
    assert blocked.success is False
    assert "PPTX HTML self-check bypass blocked" in blocked.error
    assert blocked.raw_output == {
        "type": "write_file_transaction_discarded",
        "path": str(output / "deck.patch.json"),
        "transaction_state": "discarded",
        "transaction_discarded": True,
        "reason": "pptx_self_check_bypass",
        "next_chunk_index": 2,
        "size_bytes": len(
            (first_arguments["content"] + final_arguments["content"]).encode("utf-8")
        ),
        "chunks": 2,
        "final": True,
    }
    policy.record_tool_result("write_file", final_arguments, blocked)

    retry_checkpoint = policy.build_checkpoint()
    assert retry_checkpoint is not None
    assert (
        f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}content_patch"
        in retry_checkpoint
    )
    assert "WRITE_PENDING=" not in retry_checkpoint

    restart_arguments = {
        "path": "deck.patch.json",
        "content": '{"slides":{}}',
        "chunk_index": 0,
        "final": True,
    }
    assert (
        policy.tool_call_error(
            "write_file",
            restart_arguments,
            verified_evidence_urls=set(),
        )
        is None
    )
    recovered = await tool.execute(**restart_arguments)
    assert recovered.success is True
    policy.record_tool_result("write_file", restart_arguments, recovered)

    complete = policy.build_checkpoint()
    assert complete is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}apply_patch" in complete
    assert "WRITE_PENDING=" not in complete


@pytest.mark.asyncio
async def test_controlled_patch_repair_stalls_after_different_invalid_rewrites(
    tmp_path,
):
    """Changing parse-error details must not disguise semantic non-progress."""
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "cover-hero-v1",
                        "source_outline_page": 1,
                        "props": {"title": "输入演示标题"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )
    initial = policy.build_checkpoint()
    assert initial is not None
    policy.update_checkpoint(initial)
    assert policy.stage == "content_patch"
    tool = WriteTool(
        workspace_dir=str(tmp_path),
        relative_root_dir=str(output),
    )

    invalid_documents = (
        '{"slides":',
        '{"slides":{"slide-01":',
        '{"slides":{"slide-01":{"props":',
    )
    for attempt, content in enumerate(invalid_documents):
        arguments = {
            "path": "deck.patch.json",
            "content": content,
            "chunk_index": 0,
            "final": True,
        }
        assert policy.tool_call_error(
            "write_file",
            arguments,
            verified_evidence_urls=set(),
        ) is None
        result = await tool.execute(**arguments)
        assert result.success is True
        assert result.raw_output["transaction_state"] == "committed"
        policy.record_tool_result("write_file", arguments, result)

        checkpoint = policy.build_checkpoint()
        assert checkpoint is not None
        update = policy.update_checkpoint(checkpoint)
        if attempt < 2:
            assert policy.stage == "content_patch_repair"
            assert policy.repair_stalled is False

    assert policy.repair_stalled is True
    assert policy.stage == "repair_stalled"
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}repair_stalled" in update.text


@pytest.mark.asyncio
async def test_controlled_patch_repair_locks_an_active_chunk_transaction(tmp_path):
    """Repair rewrites must not switch tools or restart while a chunk is pending."""
    output = tmp_path / "output"
    output.mkdir()
    (output / "outline.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "page": 1,
                        "title": "Exact title",
                        "message": "Exact message",
                        "bullets": ["Exact bullet"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    qa = output / "qa"
    qa.mkdir()
    (qa / "outline_check.json").write_text('{"ok": true}', encoding="utf-8")
    (output / "deck.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "id": "slide-01",
                        "layout_id": "cover-hero-v1",
                        "source_outline_page": 1,
                        "props": {"title": "输入演示标题"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    patch_path = output / "deck.patch.json"
    patch_path.write_text('{"slides":{"old":', encoding="utf-8")
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )
    initial = policy.build_checkpoint()
    assert initial is not None
    policy.update_checkpoint(initial)
    assert policy.stage == "content_patch_repair"

    tool = WriteTool(
        workspace_dir=str(tmp_path),
        relative_root_dir=str(output),
    )
    first_arguments = {
        "path": "deck.patch.json",
        "content": '{"slides":{"slide-01":{"props":{"title":"fixed',
        "chunk_index": 0,
        "final": False,
    }
    accepted = await tool.execute(**first_arguments)
    assert accepted.success is True
    policy.record_tool_result("write_file", first_arguments, accepted)

    pending = policy.build_checkpoint()
    assert pending is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}content_patch_repair" in pending
    assert 'WRITE_PENDING={"path":"deck.patch.json","next_chunk_index":1' in pending
    policy.update_checkpoint(pending)

    blocked_append = policy.tool_call_error(
        "append_file",
        {"path": "deck.patch.json", "content": "ignored"},
        verified_evidence_urls=set(),
    )
    blocked_restart = policy.tool_call_error(
        "write_file",
        {
            "path": "deck.patch.json",
            "content": '{"slides":{}}',
            "chunk_index": 0,
            "final": True,
        },
        verified_evidence_urls=set(),
    )
    assert "CONTROLLED_PRESENTATION_WRITE_TRANSACTION_PENDING" in blocked_append
    assert "CONTROLLED_PRESENTATION_WRITE_TRANSACTION_PENDING" in blocked_restart
    assert patch_path.read_text(encoding="utf-8") == '{"slides":{"old":'

    final_arguments = {
        "path": "deck.patch.json",
        "content": '"}}}}',
        "chunk_index": 1,
        "final": True,
    }
    assert (
        policy.tool_call_error(
            "write_file",
            final_arguments,
            verified_evidence_urls=set(),
        )
        is None
    )
    committed = await tool.execute(**final_arguments)
    assert committed.success is True
    policy.record_tool_result("write_file", final_arguments, committed)

    complete = policy.build_checkpoint()
    assert complete is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}apply_patch" in complete
    assert "WRITE_PENDING=" not in complete


@pytest.mark.parametrize(
    ("stage", "path", "apply_patch_repair_allowed"),
    [
        ("outline", "outline.json", False),
        ("outline_repair", "outline.json", False),
        ("content_patch", "deck.patch.json", False),
        ("content_patch_repair", "deck.patch.json", False),
        ("deck_spec_repair", "deck.patch.json", False),
        ("deck_redesign_repair", "deck.redesign.json", False),
        ("apply_patch", "deck.patch.json", True),
    ],
)
def test_controlled_write_stages_share_pending_transaction_lock(
    tmp_path,
    stage,
    path,
    apply_patch_repair_allowed,
):
    output = tmp_path / "output"
    output.mkdir()
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
        stage=stage,
        apply_patch_repair_allowed=apply_patch_repair_allowed,
    )
    arguments = {
        "path": path,
        "content": "prefix",
        "chunk_index": 0,
        "final": False,
    }
    policy.record_tool_result(
        "write_file",
        arguments,
        ToolResult(
            success=True,
            raw_output={
                "type": "write_file_chunk",
                "path": str(output / path),
                "transaction_state": "active",
                "next_chunk_index": 1,
                "size_bytes": 6,
                "final": False,
            },
        ),
    )

    error = policy.tool_call_error(
        "write_file",
        {
            "path": path,
            "content": "replacement",
            "chunk_index": 0,
            "final": True,
        },
        verified_evidence_urls=set(),
    )

    assert error is not None
    assert "CONTROLLED_PRESENTATION_WRITE_TRANSACTION_PENDING" in error


@pytest.mark.asyncio
async def test_controlled_durable_cleanup_allows_chunk_zero_restart(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )
    initial = policy.build_checkpoint()
    assert initial is not None
    policy.update_checkpoint(initial)
    assert policy.stage == "outline"
    tool = WriteTool(
        workspace_dir=str(tmp_path),
        relative_root_dir=str(output),
    )
    first_arguments = {
        "path": "outline.json",
        "content": '{"slides":',
        "chunk_index": 0,
        "final": False,
    }
    accepted = await tool.execute(**first_arguments)
    policy.record_tool_result("write_file", first_arguments, accepted)
    assert "WRITE_PENDING=" in policy.build_checkpoint()

    cleanup_records = tool.discard_pending_writes(reason="durable_checkpoint")
    policy.record_tool_cleanup("write_file", cleanup_records)

    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    assert "WRITE_PENDING=" not in checkpoint
    restart_arguments = {
        "path": "outline.json",
        "content": '{"slides": []}',
        "chunk_index": 0,
        "final": True,
    }
    assert (
        policy.tool_call_error(
            "write_file",
            restart_arguments,
            verified_evidence_urls=set(),
        )
        is None
    )
    restarted = await tool.execute(**restart_arguments)
    assert restarted.success is True


@pytest.mark.asyncio
async def test_controlled_commit_failure_synchronizes_the_next_chunk_index(
    tmp_path,
    monkeypatch,
):
    output = tmp_path / "output"
    output.mkdir()
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )
    initial = policy.build_checkpoint()
    assert initial is not None
    policy.update_checkpoint(initial)
    tool = WriteTool(
        workspace_dir=str(tmp_path),
        relative_root_dir=str(output),
    )
    first_arguments = {
        "path": "outline.json",
        "content": '{"slides":',
        "chunk_index": 0,
        "final": False,
    }
    first = await tool.execute(**first_arguments)
    policy.record_tool_result("write_file", first_arguments, first)

    real_replace = os.replace

    def fail_replace(source, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("box_agent.tools.file_tools.os.replace", fail_replace)
    failed_arguments = {
        "path": "outline.json",
        "content": " []}",
        "chunk_index": 1,
        "final": True,
    }
    failed = await tool.execute(**failed_arguments)
    policy.record_tool_result("write_file", failed_arguments, failed)

    assert failed.success is False
    assert failed.raw_output["transaction_state"] == "active"
    checkpoint = policy.build_checkpoint()
    assert checkpoint is not None
    assert 'WRITE_PENDING={"path":"outline.json","next_chunk_index":2' in checkpoint
    assert (
        policy.tool_call_error(
            "write_file",
            {
                "path": "outline.json",
                "content": "",
                "chunk_index": 2,
                "final": True,
            },
            verified_evidence_urls=set(),
        )
        is None
    )

    monkeypatch.setattr("box_agent.tools.file_tools.os.replace", real_replace)
    committed_arguments = {
        "path": "outline.json",
        "content": "",
        "chunk_index": 2,
        "final": True,
    }
    committed = await tool.execute(**committed_arguments)
    policy.record_tool_result("write_file", committed_arguments, committed)
    assert committed.success is True
    assert "WRITE_PENDING=" not in policy.build_checkpoint()


@pytest.mark.asyncio
async def test_controlled_complete_outline_chunk_can_finalize_with_empty_chunk(tmp_path):
    """A complete JSON body sent with final=false remains a live transaction."""
    output = tmp_path / "output"
    output.mkdir()
    policy = ControlledPresentationPolicy(
        workspace_dir=str(tmp_path),
        artifact_root_dir=None,
    )
    initial = policy.build_checkpoint()
    assert initial is not None
    policy.update_checkpoint(initial)
    assert policy.stage == "outline"

    tool = WriteTool(
        workspace_dir=str(tmp_path),
        relative_root_dir=str(output),
    )
    outline_body = json.dumps(
        {
            "deck_goal": "Explain the plan",
            "audience": "Decision makers",
            "source_mode": "user_provided",
            "storyline": "One clear page",
            "slides": [
                {
                    "page": 1,
                    "title": "Exact title",
                    "message": "Exact message",
                    "bullets": ["Exact bullet one", "Exact bullet two"],
                    "layout": "cover",
                    "visual": "hero",
                    "evidence": [],
                }
            ],
        }
    )
    first_arguments = {
        "path": "outline.json",
        "content": outline_body,
        "chunk_index": 0,
        "final": False,
    }
    accepted = await tool.execute(**first_arguments)
    assert accepted.success is True
    policy.record_tool_result("write_file", first_arguments, accepted)

    pending = policy.build_checkpoint()

    assert pending is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline" in pending
    assert "outline=write_pending" in pending
    assert 'WRITE_PENDING={"path":"outline.json","next_chunk_index":1' in pending
    assert 'send content="" with final=true' in pending
    policy.update_checkpoint(pending)
    final_arguments = {
        "path": "outline.json",
        "content": "",
        "chunk_index": 1,
        "final": True,
    }
    assert (
        policy.tool_call_error(
            "write_file",
            final_arguments,
            verified_evidence_urls=set(),
        )
        is None
    )
    committed = await tool.execute(**final_arguments)
    assert committed.success is True
    policy.record_tool_result("write_file", final_arguments, committed)

    complete = policy.build_checkpoint()

    assert complete is not None
    assert f"{CONTROLLED_PRESENTATION_CHECKPOINT_MARKER}outline_qa" in complete
    assert "WRITE_PENDING=" not in complete
