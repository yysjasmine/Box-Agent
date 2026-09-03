"""Provider-stream liveness invariant for the Agent Loop Kernel."""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import AsyncIterator
from time import perf_counter

from box_agent.api import ModelChunk


MODEL_ACTIVITY_INTERVAL_SECONDS = 15.0
DEFAULT_PROVIDER_STALE_SECONDS = 180.0
_PROVIDER_STALE_SECONDS_ENV = "BOX_AGENT_PROVIDER_STALE_SECONDS"


def resolve_provider_stale_seconds(config_value: float | None = None) -> float:
    """Resolve the finite positive provider-stale cutoff at the host boundary."""

    raw = os.environ.get(_PROVIDER_STALE_SECONDS_ENV)
    if raw is not None and raw.strip():
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
    if config_value is not None:
        value = float(config_value)
        if math.isfinite(value) and value > 0:
            return value
    return DEFAULT_PROVIDER_STALE_SECONDS


async def stream_with_liveness(
    stream: AsyncIterator[ModelChunk],
    *,
    stale_seconds: float | None = None,
) -> AsyncIterator[ModelChunk]:
    """Emit bounded heartbeats and terminate a provider that stops producing data."""

    effective_stale = (
        DEFAULT_PROVIDER_STALE_SECONDS
        if stale_seconds is None
        else float(stale_seconds)
    )
    if not math.isfinite(effective_stale) or effective_stale <= 0:
        effective_stale = DEFAULT_PROVIDER_STALE_SECONDS
    iterator = stream.__aiter__()
    next_chunk: asyncio.Task[ModelChunk] | None = None
    last_provider_chunk = perf_counter()
    try:
        next_chunk = asyncio.create_task(iterator.__anext__())
        while True:
            done, _ = await asyncio.wait(
                {next_chunk}, timeout=MODEL_ACTIVITY_INTERVAL_SECONDS
            )
            if not done:
                elapsed = perf_counter() - last_provider_chunk
                activity = {
                    "protocol": "agent_activity_v1",
                    "phase": "provider_wait",
                    "seconds_since_provider_chunk": round(elapsed, 1),
                }
                if elapsed >= effective_stale:
                    yield ModelChunk(
                        finish_reason="provider_stale",
                        activity=activity,
                    )
                    return
                yield ModelChunk(activity=activity)
                continue
            try:
                chunk = next_chunk.result()
            except StopAsyncIteration:
                return
            last_provider_chunk = perf_counter()
            yield chunk
            next_chunk = asyncio.create_task(iterator.__anext__())
    finally:
        if next_chunk is not None and not next_chunk.done():
            next_chunk.cancel()
            try:
                await next_chunk
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
        closer = getattr(iterator, "aclose", None)
        if callable(closer):
            try:
                await closer()
            except (RuntimeError, asyncio.CancelledError):
                pass


__all__ = [
    "DEFAULT_PROVIDER_STALE_SECONDS",
    "MODEL_ACTIVITY_INTERVAL_SECONDS",
    "resolve_provider_stale_seconds",
    "stream_with_liveness",
]
