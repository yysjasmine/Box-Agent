"""Real stdio transport smoke test for the ACP-to-Kernel adapter."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FROZEN_BINARY = os.environ.get("BOX_AGENT_ACP_BINARY")


async def _rpc(
    proc: asyncio.subprocess.Process,
    request_id: int,
    method: str,
    params: dict,
    *,
    timeout: float = 10,
) -> dict:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(
        (json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n").encode()
    )
    await proc.stdin.drain()
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    if not line:
        stderr = b""
        if proc.stderr is not None:
            stderr = await proc.stderr.read()
        raise AssertionError(
            "ACP stdio server exited before replying: "
            + stderr.decode("utf-8", errors="replace")
        )
    return json.loads(line)


@pytest.mark.asyncio
async def test_acp_stdio_initialize_and_session_new_round_trip(tmp_path: Path) -> None:
    frozen_binary = FROZEN_BINARY
    command = (
        [frozen_binary]
        if frozen_binary
        else [sys.executable, "-m", "box_agent.acp.server"]
    )
    env = os.environ.copy()
    config_dir = tmp_path / ".box-agent" / "config"
    config_dir.mkdir(parents=True)
    example = (
        PROJECT_ROOT / "box_agent" / "config" / "config-example.yaml"
    ).read_text(encoding="utf-8")
    # This probe verifies the ACP transport and Kernel session boundary.
    # Disable network-discovered MCP here so a deterministic local smoke test
    # never depends on remote authentication or connection latency; the
    # frozen bundled MCP entry point is probed separately.
    example = example.replace(
        "enable_mcp: true # Enable MCP tools",
        "enable_mcp: false # Disabled by deterministic ACP smoke test",
    )
    (config_dir / "config.yaml").write_text(
        example.replace("YOUR_API_KEY_HERE", "test-only-key"),
        encoding="utf-8",
    )
    env.update(
        {
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "BOX_AGENT_LOG_DIR": str(tmp_path / "logs"),
        }
    )
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(tmp_path if frozen_binary else PROJECT_ROOT),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        initialized = await _rpc(
            proc,
            1,
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "box-agent-e2e", "version": "1"},
            },
        )
        assert initialized.get("error") is None, initialized
        assert initialized["result"]["agentCapabilities"]["loadSession"] is True

        created = await _rpc(
            proc,
            2,
            "session/new",
            {"cwd": str(tmp_path), "mcpServers": []},
            # initialize is transport readiness; session/new is the first
            # request that intentionally waits for full plugin assembly.
            timeout=15,
        )
        assert created.get("error") is None, created
        assert created["result"]["sessionId"]
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.terminate()
            await proc.wait()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not FROZEN_BINARY,
    reason="bundled MCP entry point exists only in the frozen runtime",
)
async def test_frozen_runtime_exposes_bundled_web_extract_mcp() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(FROZEN_BINARY),
        args=["--web-extract-mcp"],
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            info = await session.initialize()
            tools = await session.list_tools()

    assert info.serverInfo.name == "box-agent-web-extract"
    assert [tool.name for tool in tools.tools] == ["web_extract"]
