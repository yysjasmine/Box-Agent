"""Kernel-owned execution service for isolated child-agent runs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from box_agent.adapters.plugin_host import build_plugin_host
from box_agent.api import AgentEvent, Message, RunOptions, RunRequest, RunResult
from box_agent.kernel import PluginKernelComposer


class KernelChildAgentRunner:
    """Compose and execute one isolated child through ``AgentLoopKernel``.

    The runner is injected into the ``sub_agent`` Tool at the composition
    boundary. The Tool owns delegation policy; this service owns execution.
    Neither path imports or invokes the retired ``core.run_agent_loop``.
    """

    async def run(
        self,
        *,
        llm: Any,
        messages: Sequence[Any],
        tools: Mapping[str, Any],
        max_steps: int,
        max_tool_calls: int | None,
        max_parallel_tools: int,
        context_budget: int,
        permission_negotiator: Any | None,
        session_id: str,
        turn_id: str,
        metadata: Mapping[str, Any] | None = None,
        prefer_generate: bool = False,
        emit: Callable[[AgentEvent], Awaitable[None] | None],
    ) -> RunResult:
        system_prompt = "\n\n".join(
            str(getattr(message, "content", "") or "")
            for message in messages
            if str(getattr(message, "role", "")) == "system"
        ).strip()
        user_message = next(
            (
                message
                for message in reversed(tuple(messages))
                if str(getattr(message, "role", "")) == "user"
            ),
            None,
        )
        if user_message is None:
            raise ValueError("a child run requires one user message")

        host = build_plugin_host(
            llm=llm,
            tools=tuple(tools.values()),
            permission_negotiator=permission_negotiator,
            prefer_generate=prefer_generate,
        )
        request_id = f"child-request-{uuid4().hex}"
        request = RunRequest(
            request_id=request_id,
            session_id=session_id,
            turn_id=turn_id,
            user_input=Message(
                role="user",
                content=str(getattr(user_message, "content", "") or ""),
            ),
            options=RunOptions(
                max_steps=max_steps,
                max_tool_calls=max_tool_calls,
                max_parallel_tools=max_parallel_tools,
                context_budget=context_budget,
            ),
            metadata={
                **dict(metadata or {}),
                "system_prompt": system_prompt,
                "runtime": "kernel",
                "run_id": f"child-run-{uuid4().hex}",
            },
        )
        kernel = PluginKernelComposer(host).build(request)
        return await kernel.run(
            request,
            emit=emit,
            cancel_event=asyncio.Event(),
        )


__all__ = ["KernelChildAgentRunner"]
