"""Fast ACP transport bootstrap with deferred Kernel runtime assembly."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Awaitable
from typing import Any

from box_agent.acp.protocol import initialize_response


class DeferredACPAgent:
    """Protocol adapter that materializes its Kernel-backed delegate on demand."""

    def __init__(self, conn: Any, runtime: Awaitable[Any]) -> None:
        self._conn = conn
        self._runtime = asyncio.ensure_future(runtime)
        self._delegate: Any | None = None
        self._initialize_params: Any | None = None
        self._delegate_lock = asyncio.Lock()

    async def initialize(self, params: Any) -> Any:
        self._initialize_params = params
        return initialize_response()

    async def _resolve_delegate(self) -> Any:
        if self._delegate is not None:
            return self._delegate
        async with self._delegate_lock:
            if self._delegate is None:
                runtime = await self._runtime
                delegate = runtime.build_agent(self._conn)
                if self._initialize_params is not None:
                    await delegate.initialize(self._initialize_params)
                self._delegate = delegate
        return self._delegate

    async def _call(self, name: str, params: Any, *, optional: bool = False) -> Any:
        delegate = await self._resolve_delegate()
        method = getattr(delegate, name, None)
        if not callable(method):
            if optional:
                return None
            raise AttributeError(f"ACP runtime agent does not implement {name}")
        return await method(params)

    async def newSession(self, params: Any) -> Any:
        return await self._call("newSession", params)

    async def loadSession(self, params: Any) -> Any:
        return await self._call("loadSession", params)

    async def setSessionMode(self, params: Any) -> Any:
        return await self._call("setSessionMode", params, optional=True)

    async def setSessionModel(self, params: Any) -> Any:
        return await self._call("setSessionModel", params, optional=True)

    async def authenticate(self, params: Any) -> Any:
        return await self._call("authenticate", params, optional=True)

    async def prompt(self, params: Any) -> Any:
        return await self._call("prompt", params)

    async def cancel(self, params: Any) -> None:
        # Before a Session exists there is nothing in the Kernel to cancel.
        if self._delegate is None and not self._runtime.done():
            return
        await self._call("cancel", params)

    async def extMethod(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        delegate = await self._resolve_delegate()
        handler = getattr(delegate, "extMethod", None)
        if not callable(handler):
            raise AttributeError("ACP runtime agent does not implement extMethod")
        return await handler(method, params)

    async def extNotification(self, method: str, params: dict[str, Any]) -> None:
        delegate = await self._resolve_delegate()
        handler = getattr(delegate, "extNotification", None)
        if callable(handler):
            await handler(method, params)


def _configure_protocol_streams() -> tuple[Any, Any]:
    protocol_stdout = sys.__stdout__
    sys.stdout = sys.stderr
    logging.root.handlers.clear()
    logging.root.addHandler(logging.StreamHandler(sys.stderr))
    logging.root.setLevel(logging.INFO)
    return protocol_stdout, sys.stderr


def _stderr_print(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def _load_runtime_builder(config: Any | None) -> tuple[Any, Any]:
    """Import heavyweight providers/tools off the ACP protocol event loop."""

    if config is None:
        from box_agent.config import Config

        config = Config.load()
    from box_agent.acp.kernel_runtime import build_kernel_acp_runtime

    return build_kernel_acp_runtime, config


async def _assemble_runtime(config: Any | None) -> Any:
    build_kernel_acp_runtime, loaded_config = await asyncio.to_thread(
        _load_runtime_builder,
        config,
    )
    return await build_kernel_acp_runtime(loaded_config, output=_stderr_print)


async def run_acp_bootstrap(config: Any | None = None) -> None:
    """Own ACP transport while the plugin-composed Kernel assembles lazily."""

    from acp import AgentSideConnection
    from box_agent import __version__
    from box_agent.acp.stdio_compat import stdio_streams_largebuf

    protocol_stdout, _ = _configure_protocol_streams()
    sys.stdout = protocol_stdout
    reader, writer = await stdio_streams_largebuf()
    if sys.platform == "win32":
        stdout_buffer = sys.stdout.buffer
        transport = writer.transport

        def pinned_write(data: bytes) -> None:
            if getattr(transport, "_is_closing", False):
                return
            try:
                stdout_buffer.write(data)
                stdout_buffer.flush()
            except Exception:
                logging.exception("error writing ACP response")

        transport.write = pinned_write  # type: ignore[method-assign]

    sys.stdout = sys.stderr
    runtime_task = asyncio.create_task(_assemble_runtime(config))
    AgentSideConnection(
        lambda conn: DeferredACPAgent(conn, runtime_task),
        writer,
        reader,
    )
    _stderr_print(f"Box-Agent ACP protocol ready (v{__version__})")
    try:
        await asyncio.Event().wait()
    finally:
        if not runtime_task.done():
            runtime_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            runtime = await runtime_task
            await runtime.close()


__all__ = ["DeferredACPAgent", "run_acp_bootstrap"]
