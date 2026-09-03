"""Loopback-only HTTP service for the Agent Trace viewer."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import webbrowser


MAX_REQUEST_BYTES = 64 * 1024
MAX_TRACE_BYTES = 50 * 1024 * 1024
MAX_DIRECTORY_BYTES = 200 * 1024 * 1024


class TraceViewerRequestHandler(SimpleHTTPRequestHandler):
    """Serve viewer assets and the selected local trace directory."""

    server_version = "BoxAgentTraceViewer/1"

    def _loopback_authorities(self) -> set[str]:
        port = int(self.server.server_address[1])
        if port == 80:
            return {"127.0.0.1", "localhost"}
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _request_is_trusted(self) -> bool:
        authorities = self._loopback_authorities()
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in authorities:
            return False

        origin = self.headers.get("Origin")
        if origin is None:
            return True
        parsed = urlsplit(origin.strip())
        return (
            parsed.scheme == "http"
            and parsed.netloc.lower() in authorities
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )

    def _reject_untrusted_request(self) -> bool:
        if self._request_is_trusted():
            return False
        self._drain_bounded_request_body()
        self._send_json(
            403,
            {"error": "Trace viewer requests must use the loopback origin"},
        )
        return True

    def _drain_bounded_request_body(self) -> None:
        """Consume a rejected bounded body before the socket is closed.

        Windows resets a TCP connection closed with unread inbound data.  A
        reset can discard the 403 response that protects this loopback-only
        service, so rejected normal-sized requests must reach a clean HTTP
        message boundary before the response is sent.
        """

        try:
            remaining = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return
        if remaining <= 0 or remaining > MAX_REQUEST_BYTES:
            return
        while remaining:
            chunk = self.rfile.read(min(remaining, 16 * 1024))
            if not chunk:
                return
            remaining -= len(chunk)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP handler contract
        if self._reject_untrusted_request():
            return
        if urlsplit(self.path).path == "/api/health":
            self._send_json(
                200,
                {"service": "box-agent-trace-viewer", "version": 1},
            )
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - inherited HTTP handler contract
        if self._reject_untrusted_request():
            return
        if urlsplit(self.path).path != "/api/directory":
            self._send_json(404, {"error": "Not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._send_json(400, {"error": "Invalid request body"})
            return

        try:
            request = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Request body must be valid JSON"})
            return

        raw_path = request.get("path") if isinstance(request, dict) else None
        if not isinstance(raw_path, str) or not raw_path.strip():
            self._send_json(400, {"error": "Directory path is required"})
            return

        directory = Path(raw_path.strip()).expanduser().resolve()
        if not directory.is_dir():
            self._send_json(400, {"error": f"Directory does not exist: {directory}"})
            return

        metadata_only = request.get("metadataOnly") is True
        try:
            entries, skipped = _read_trace_directory(
                directory,
                include_text=not metadata_only,
            )
        except OSError as error:
            self._send_json(400, {"error": f"Could not read directory: {error}"})
            return

        self._send_json(
            200,
            {
                "directory": {"name": directory.name, "path": str(directory)},
                "entries": entries,
                "skipped": skipped,
            },
        )


def _read_trace_directory(
    directory: Path,
    *,
    include_text: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    total_bytes = 0
    candidates = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
    for path in candidates:
        if path.is_symlink() or not path.is_file() or path.suffix.casefold() != ".jsonl":
            continue
        stat = path.stat()
        if stat.st_size > MAX_TRACE_BYTES:
            skipped.append({"fileName": path.name, "reason": "larger than 50 MiB"})
            continue
        if total_bytes + stat.st_size > MAX_DIRECTORY_BYTES:
            skipped.append({"fileName": path.name, "reason": "directory exceeds 200 MiB"})
            continue
        entry: dict[str, Any] = {
            "name": path.name,
            "size": stat.st_size,
            "lastModified": round(stat.st_mtime * 1000),
        }
        if include_text:
            content = path.read_bytes()
            entry["size"] = len(content)
            # Match ``Path.read_text`` semantics used by hosts/tests: trace
            # payloads are exposed with normalized LF newlines even when the
            # Windows filesystem stores CRLF bytes.
            entry["text"] = content.decode("utf-8", errors="replace").replace(
                "\r\n", "\n"
            )
        total_bytes += int(entry["size"])
        entries.append(entry)
    return entries, skipped


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    viewer_root: Path | None = None,
) -> ThreadingHTTPServer:
    """Create a loopback viewer server without starting its event loop."""

    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Trace viewer service must bind to the loopback interface")
    root = (viewer_root or Path(__file__).parent).resolve()
    handler = partial(TraceViewerRequestHandler, directory=str(root))
    return ThreadingHTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the Agent Trace viewer locally")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser")
    args = parser.parse_args(argv)

    server = create_server(port=args.port)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"Agent Trace viewer: {url}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
