from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
from threading import Thread

import pytest


@pytest.fixture
def trace_viewer_server():
    from box_agent.trace_viewer.server import create_server

    server = create_server(host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _request(
    server,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    extra_headers: dict[str, str] | None = None,
):
    connection = HTTPConnection(*server.server_address, timeout=5)
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    headers.update(extra_headers or {})
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    content_type = response.getheader("Content-Type") or ""
    raw = response.read()
    connection.close()
    return response.status, content_type, raw


def test_local_service_serves_the_viewer_and_health_endpoint(trace_viewer_server) -> None:
    """Removing either the UI or service probe must break the deployable viewer."""

    status, content_type, raw = _request(trace_viewer_server, "GET", "/")
    assert status == 200
    assert content_type.startswith("text/html")
    assert b"Agent Trace" in raw

    status, content_type, raw = _request(trace_viewer_server, "GET", "/api/health")
    assert status == 200
    assert content_type.startswith("application/json")
    assert json.loads(raw) == {"service": "box-agent-trace-viewer", "version": 1}


def test_local_service_reads_only_top_level_jsonl_files(
    trace_viewer_server,
    tmp_path: Path,
) -> None:
    """Returning nested or non-trace files would disclose data outside the chosen set."""

    trace = tmp_path / "one.jsonl"
    trace.write_bytes(b'{"type":"session_start"}\n')
    (tmp_path / "notes.txt").write_text("private notes", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "two.jsonl").write_bytes(b'{"type":"session_start"}\n')

    status, content_type, raw = _request(
        trace_viewer_server,
        "POST",
        "/api/directory",
        {"path": str(tmp_path)},
    )

    assert status == 200
    assert content_type.startswith("application/json")
    payload = json.loads(raw)
    assert payload["directory"] == {"name": tmp_path.name, "path": str(tmp_path.resolve())}
    assert payload["skipped"] == []
    assert [entry["name"] for entry in payload["entries"]] == ["one.jsonl"]
    assert payload["entries"][0]["text"] == '{"type":"session_start"}\n'
    assert payload["entries"][0]["size"] == trace.stat().st_size


def test_local_service_metadata_scan_omits_bodies_and_sees_new_trace_files(
    trace_viewer_server,
    tmp_path: Path,
) -> None:
    """A live directory poll must detect additions without rereading every trace body."""

    first = tmp_path / "one.jsonl"
    first.write_bytes(b'{"event":"session.start"}\n')

    status, _, raw = _request(
        trace_viewer_server,
        "POST",
        "/api/directory",
        {"path": str(tmp_path), "metadataOnly": True},
    )

    assert status == 200
    initial = json.loads(raw)
    assert [entry["name"] for entry in initial["entries"]] == ["one.jsonl"]
    assert "text" not in initial["entries"][0]

    second = tmp_path / "two.jsonl"
    second.write_bytes(b'{"event":"session.start"}\n')
    status, _, raw = _request(
        trace_viewer_server,
        "POST",
        "/api/directory",
        {"path": str(tmp_path), "metadataOnly": True},
    )

    assert status == 200
    refreshed = json.loads(raw)
    assert [entry["name"] for entry in refreshed["entries"]] == [
        "one.jsonl",
        "two.jsonl",
    ]
    assert all("text" not in entry for entry in refreshed["entries"])


def test_local_service_rejects_a_non_directory_path(trace_viewer_server, tmp_path: Path) -> None:
    """A mistyped path must return a useful client error instead of an empty catalog."""

    missing = tmp_path / "missing"
    status, content_type, raw = _request(
        trace_viewer_server,
        "POST",
        "/api/directory",
        {"path": str(missing)},
    )

    assert status == 400
    assert content_type.startswith("application/json")
    assert json.loads(raw) == {"error": f"Directory does not exist: {missing.resolve()}"}


@pytest.mark.parametrize(
    "extra_headers",
    [
        {"Host": "evil.example:8766"},
        {"Origin": "http://evil.example:8766"},
    ],
)
def test_local_service_rejects_non_loopback_request_authority(
    trace_viewer_server,
    tmp_path: Path,
    extra_headers: dict[str, str],
) -> None:
    """A rebinding origin must not read a guessed local trace directory."""

    (tmp_path / "secret.jsonl").write_text(
        '{"secret":"must-not-leak"}\n',
        encoding="utf-8",
    )

    status, content_type, raw = _request(
        trace_viewer_server,
        "POST",
        "/api/directory",
        {"path": str(tmp_path)},
        extra_headers,
    )

    assert status == 403
    assert content_type.startswith("application/json")
    assert b"must-not-leak" not in raw
    assert json.loads(raw) == {
        "error": "Trace viewer requests must use the loopback origin"
    }


def test_local_service_drains_rejected_post_before_closing_connection(
    trace_viewer_server,
) -> None:
    """Windows must not reset a rejected POST while its body is still unread."""

    status, content_type, raw = _request(
        trace_viewer_server,
        "POST",
        "/api/directory",
        {"padding": "x" * 60_000},
        {"Host": "evil.example:8766"},
    )

    assert status == 403
    assert content_type.startswith("application/json")
    assert json.loads(raw) == {
        "error": "Trace viewer requests must use the loopback origin"
    }


def test_local_service_rejects_rebinding_host_before_serving_assets(
    trace_viewer_server,
) -> None:
    status, content_type, raw = _request(
        trace_viewer_server,
        "GET",
        "/",
        extra_headers={"Host": "evil.example:8766"},
    )

    assert status == 403
    assert content_type.startswith("application/json")
    assert b"Agent Trace" not in raw


def test_local_service_accepts_its_exact_loopback_origin(
    trace_viewer_server,
    tmp_path: Path,
) -> None:
    trace = tmp_path / "one.jsonl"
    trace.write_text('{"event":"session.start"}\n', encoding="utf-8")
    host, port = trace_viewer_server.server_address

    status, _, raw = _request(
        trace_viewer_server,
        "POST",
        "/api/directory",
        {"path": str(tmp_path)},
        {"Origin": f"http://{host}:{port}"},
    )

    assert status == 200
    assert json.loads(raw)["entries"][0]["text"] == trace.read_text(
        encoding="utf-8"
    )
