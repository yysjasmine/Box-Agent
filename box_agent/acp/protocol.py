"""Lightweight ACP protocol facts shared by bootstrap and host adapter."""

from __future__ import annotations

from typing import Any


def initialize_response() -> Any:
    """Describe the stable ACP surface without constructing the Agent runtime."""

    from acp import PROTOCOL_VERSION, InitializeResponse
    from acp.schema import AgentCapabilities, Implementation
    from box_agent import __version__

    return InitializeResponse(
        protocolVersion=PROTOCOL_VERSION,
        agentCapabilities=AgentCapabilities(loadSession=True),
        agentInfo=Implementation(
            name="box-agent",
            title="Box-Agent",
            version=__version__,
        ),
    )


__all__ = ["initialize_response"]
