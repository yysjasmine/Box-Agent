"""Asynchronous host projections produced after an Agent Run finishes."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from box_agent.api import HostProjection, Message, RunResult


@dataclass(frozen=True, slots=True)
class PostRunProjectionRequest:
    """Host-neutral input for a non-blocking post-run projection plugin."""

    session_id: str
    run_id: str
    turn_id: str
    user_input: Message
    result: RunResult
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("session_id", "run_id", "turn_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.user_input, Message):
            raise ValueError("user_input must be Message")
        if not isinstance(self.result, RunResult):
            raise ValueError("result must be RunResult")
        object.__setattr__(self, "metadata", dict(self.metadata))


class PostRunProjectionManager:
    """Own per-Session task cancellation while plugins own projection policy."""

    def __init__(
        self,
        plugins_provider: Callable[[], Iterable[Any]],
    ) -> None:
        if not callable(plugins_provider):
            raise ValueError("plugins_provider must be callable")
        self._plugins_provider = plugins_provider
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def begin_turn(self, session_id: str) -> None:
        """Cancel a stale projection before a newer turn can supersede it."""

        task = self._tasks.pop(str(session_id), None)
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def schedule(
        self,
        request: PostRunProjectionRequest,
        sink: Callable[[HostProjection], Any],
    ) -> asyncio.Task[None]:
        if request.session_id in self._tasks and not self._tasks[request.session_id].done():
            raise RuntimeError("begin_turn must cancel the previous projection first")
        task = asyncio.create_task(self._run(request, sink))
        self._tasks[request.session_id] = task
        task.add_done_callback(
            lambda completed: self._discard_if_current(request.session_id, completed)
        )
        return task

    async def wait_for_session(self, session_id: str) -> None:
        """Testing/shutdown boundary; normal prompts never await projections."""

        task = self._tasks.get(str(session_id))
        if task is not None:
            await task

    async def _run(
        self,
        request: PostRunProjectionRequest,
        sink: Callable[[HostProjection], Any],
    ) -> None:
        emitted: set[str] = set()
        for plugin in tuple(self._plugins_provider()):
            project = getattr(plugin, "project", None)
            if not callable(project):
                continue
            try:
                value = project(request)
                if hasattr(value, "__await__"):
                    value = await value
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            values = value if isinstance(value, (list, tuple)) else (value,)
            for projection in values:
                if not isinstance(projection, HostProjection):
                    continue
                if projection.projection_id in emitted:
                    continue
                emitted.add(projection.projection_id)
                emitted_value = sink(projection)
                if inspect.isawaitable(emitted_value):
                    await emitted_value

    def _discard_if_current(
        self,
        session_id: str,
        completed: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(session_id) is completed:
            self._tasks.pop(session_id, None)
        if not completed.cancelled():
            # Retrieve a background exception so the event loop does not emit
            # an unowned "Task exception was never retrieved" warning.
            completed.exception()


__all__ = ["PostRunProjectionManager", "PostRunProjectionRequest"]
