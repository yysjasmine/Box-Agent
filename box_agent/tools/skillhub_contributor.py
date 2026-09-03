"""Plugin-composed SkillHub tools and context for capable hosts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from box_agent.context.api import ContextBuildRequest, ContextItem

from .runtime_context import current_runtime_invocation
from .skillhub_install_tool import (
    SKILLHUB_INSTALL_CAPABILITY_VERSION,
    SKILLHUB_INSTALL_METHOD,
    SkillHubInstallTool,
)
from .skillhub_search_tool import (
    HARD_CAPABILITY_GAP_PROMPT,
    SKILLHUB_SEARCH_CAPABILITY_VERSION,
    SKILLHUB_SEARCH_METHOD,
    SkillHubSearchTool,
)


class SkillHubHostBridge:
    """Late-bind the ACP connection without making Tools transport owners."""

    def __init__(self) -> None:
        self._connection: Any | None = None

    def bind(self, connection: Any) -> None:
        self._connection = connection

    async def ext_method(self, method: str, payload: Mapping[str, Any]) -> Any:
        connection = self._connection
        sender = getattr(connection, "ext_method", None) or getattr(
            connection, "extMethod", None
        )
        if not callable(sender):
            return {"status": "unavailable"}
        return await sender(method, dict(payload))


def _capability_version(metadata: Mapping[str, Any], *names: str) -> int | None:
    raw_capabilities = metadata.get(
        "host_capabilities", metadata.get("hostCapabilities", {})
    )
    if not isinstance(raw_capabilities, Mapping):
        return None
    value: Any = None
    for name in names:
        if name in raw_capabilities:
            value = raw_capabilities[name]
            break
    if isinstance(value, Mapping):
        value = value.get("version")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _search_enabled(metadata: Mapping[str, Any]) -> bool:
    return _capability_version(metadata, "skillhub_search", "skillhubSearch") == (
        SKILLHUB_SEARCH_CAPABILITY_VERSION
    )


def _install_enabled(metadata: Mapping[str, Any]) -> bool:
    return _search_enabled(metadata) and _capability_version(
        metadata, "skillhub_install", "skillhubInstall"
    ) == SKILLHUB_INSTALL_CAPABILITY_VERSION


def skillhub_capabilities(metadata: Mapping[str, Any]) -> tuple[bool, bool]:
    """Return the negotiated search/install capability flags."""
    return _search_enabled(metadata), _install_enabled(metadata)


class SkillHubDiscoveryState:
    """Observe deferred MCP discovery without coupling the Kernel to SkillHub."""

    def __init__(self, *, tool_search_available: bool) -> None:
        self._tool_search_available = bool(tool_search_available)
        self._used_runs: set[tuple[str, str]] = set()

    async def on_event(self, event: Any) -> None:
        key = (
            str(getattr(event, "session_id", "") or ""),
            str(getattr(event, "run_id", "") or ""),
        )
        event_type = str(getattr(event, "type", "") or "")
        payload = getattr(event, "payload", {})
        if (
            event_type == "tool.call.requested"
            and isinstance(payload, Mapping)
            and str(payload.get("tool_name", "") or "") == "tool_search"
        ):
            self._used_runs.add(key)
        elif event_type in {"run.completed", "run.cancelled", "run.failed"}:
            self._used_runs.discard(key)

    def snapshot(self, session_id: str, run_id: str) -> dict[str, bool]:
        return {
            "tool_search_available": self._tool_search_available,
            "tool_search_used": (session_id, run_id) in self._used_runs,
        }

    def current_snapshot(self) -> dict[str, bool]:
        invocation = current_runtime_invocation()
        return self.snapshot(invocation.session_id, invocation.run_id)


class SkillHubToolContributor:
    """Create Session-local marketplace tools only for a capable host."""

    def __init__(
        self,
        connection: Any,
        *,
        skill_loader: Any | None = None,
        discovery_state: SkillHubDiscoveryState | None = None,
    ) -> None:
        self._connection = connection
        self._skill_loader = skill_loader
        self._discovery_state = discovery_state

    async def _request(
        self,
        method: str,
        session_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        sender = getattr(self._connection, "ext_method", None) or getattr(
            self._connection, "extMethod", None
        )
        if not callable(sender):
            return {"status": "unavailable"}
        try:
            response = await sender(method, {"sessionId": session_id, **dict(payload)})
        except Exception:
            return {"status": "unavailable"}
        return dict(response) if isinstance(response, Mapping) else {"status": "unavailable"}

    def provide_tools(self, request: Any) -> tuple[Any, ...]:
        raw_metadata = getattr(request, "metadata", {})
        metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
        if not _search_enabled(metadata):
            return ()
        session_id = str(getattr(request, "session_id", "") or "")

        async def searcher(payload: dict[str, Any]) -> dict[str, Any]:
            return await self._request(SKILLHUB_SEARCH_METHOD, session_id, payload)

        search_tool = SkillHubSearchTool(
            searcher,
            snapshot_provider=(
                self._discovery_state.current_snapshot
                if self._discovery_state is not None
                else None
            ),
            installation_available=_install_enabled(metadata),
        )
        tools: list[Any] = [search_tool]
        if _install_enabled(metadata):

            async def installer(payload: dict[str, Any]) -> dict[str, Any]:
                outbound = {
                    "skillId": payload.get("skillId"),
                    "slug": payload.get("slug"),
                    "displayName": payload.get("name"),
                    "publisherDisplayName": payload.get("publisherDisplayName"),
                    "version": payload.get("version"),
                }
                return await self._request(
                    SKILLHUB_INSTALL_METHOD, session_id, outbound
                )

            tools.append(
                SkillHubInstallTool(
                    installer,
                    candidate_provider=search_tool.candidate,
                    candidate_list_provider=search_tool.candidates,
                    skill_loader=self._skill_loader,
                )
            )
        return tuple(tools)


class SkillHubContextContributor:
    """Expose marketplace policy only in Sessions whose host supports it."""

    def provide(self, request: ContextBuildRequest) -> tuple[ContextItem, ...]:
        metadata = request.metadata
        if not _search_enabled(metadata):
            return ()
        scope = request.session_id or request.run_id or "run"
        return (
            ContextItem(
                item_id=f"{scope}:context:skill-marketplace",
                kind="system",
                content=HARD_CAPABILITY_GAP_PROMPT.strip(),
                priority=840,
                pinned=True,
                metadata={"role": "system", "contributor": "skillhub"},
            ),
        )


__all__ = [
    "SkillHubContextContributor",
    "SkillHubDiscoveryState",
    "SkillHubHostBridge",
    "SkillHubToolContributor",
    "skillhub_capabilities",
]
