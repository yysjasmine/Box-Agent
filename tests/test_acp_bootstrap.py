"""Tests for the lightweight ACP protocol bootstrap."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from box_agent.acp.bootstrap import DeferredACPAgent
from box_agent.acp import bootstrap


class _Delegate:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def initialize(self, params: object) -> object:
        self.calls.append(("initialize", params))
        return object()

    async def newSession(self, params: object) -> str:
        self.calls.append(("newSession", params))
        return "created"


class _Runtime:
    def __init__(self, delegate: _Delegate) -> None:
        self._delegate = delegate

    def build_agent(self, _conn: object) -> _Delegate:
        return self._delegate


@pytest.mark.asyncio
async def test_initialize_does_not_wait_for_runtime_assembly() -> None:
    runtime: asyncio.Future[_Runtime] = asyncio.get_running_loop().create_future()
    agent = DeferredACPAgent(object(), runtime)

    params = SimpleNamespace(field_meta={"host": "officev3"})
    response = await asyncio.wait_for(agent.initialize(params), timeout=0.1)

    assert response.agentCapabilities.loadSession is True
    assert runtime.done() is False


@pytest.mark.asyncio
async def test_first_runtime_request_replays_initialize_before_delegating() -> None:
    delegate = _Delegate()
    runtime: asyncio.Future[_Runtime] = asyncio.get_running_loop().create_future()
    agent = DeferredACPAgent("connection", runtime)
    initialize = SimpleNamespace(field_meta={"host": "officev3"})
    session = SimpleNamespace(cwd="D:/workspace")

    await agent.initialize(initialize)
    pending = asyncio.create_task(agent.newSession(session))
    await asyncio.sleep(0)
    assert pending.done() is False

    runtime.set_result(_Runtime(delegate))

    assert await pending == "created"
    assert delegate.calls == [
        ("initialize", initialize),
        ("newSession", session),
    ]


def test_importing_acp_entrypoint_does_not_load_kernel_runtime() -> None:
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import box_agent.acp; "
                "assert 'box_agent.acp.kernel_runtime' not in sys.modules; "
                "assert 'box_agent.adapters.acp_kernel' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_runtime_imports_do_not_block_the_protocol_loop(monkeypatch) -> None:
    marker = object()

    async def build(config: object, *, output) -> object:
        del output
        assert config is marker
        return "runtime"

    def load(_config: object) -> tuple[object, object]:
        time.sleep(0.1)
        return build, marker

    monkeypatch.setattr(bootstrap, "_load_runtime_builder", load)

    assembly = asyncio.create_task(bootstrap._assemble_runtime(None))
    await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.05)

    assert assembly.done() is False
    assert await assembly == "runtime"
