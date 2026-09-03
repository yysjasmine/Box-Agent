"""Confirmed Skill marketplace installation over the host protocol."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .base import Tool, ToolResult
from .skill_loader import SkillLoader

SKILLHUB_INSTALLATION_TYPE = "skillhub_installation"
SKILLHUB_INSTALL_METHOD = "session/skillhub_install"
SKILLHUB_INSTALL_CAPABILITY_VERSION = 1

CandidateProvider = Callable[[str], Mapping[str, Any] | None]
CandidateListProvider = Callable[[], list[Mapping[str, Any]]]
SkillHubInstaller = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _text(value: object, *, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


class SkillHubInstallTool(Tool):
    """Install a returned marketplace candidate after one-shot confirmation."""

    parallel_safe = False
    max_result_size_chars = 4_000

    def __init__(
        self,
        installer: SkillHubInstaller,
        *,
        candidate_provider: CandidateProvider,
        candidate_list_provider: CandidateListProvider | None = None,
        skill_loader: SkillLoader | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._installer = installer
        self._candidate_provider = candidate_provider
        self._candidate_list_provider = candidate_list_provider or (lambda: [])
        self._skill_loader = skill_loader
        self._timeout_seconds = timeout_seconds
        self._approved_skill_ids: set[str] = set()

    @property
    def name(self) -> str:
        return "install_skillhub_skill"

    @property
    def description(self) -> str:
        return (
            "Install one exact candidate returned by search_skillhub. Pass only its "
            "exact skill_id. This tool requests one-shot user confirmation, delegates "
            "download and verification to the host, and refreshes the live Skill catalog."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": "Exact candidate ID returned by search_skillhub.",
                    "minLength": 1,
                    "maxLength": 80,
                }
            },
            "required": ["skill_id"],
            "additionalProperties": False,
        }

    def approve_permission_request(self, permission_request: dict[str, Any]) -> None:
        if permission_request.get("scope") != "skillhub":
            return
        skill_id = _text(permission_request.get("skill_id"), limit=80)
        if permission_request.get("requested_scope") != f"install:{skill_id}":
            return
        if self._candidate_provider(skill_id) is not None:
            self._approved_skill_ids.add(skill_id)

    async def execute(self, skill_id: str) -> ToolResult:
        normalized_id = skill_id.strip()
        candidate = self._candidate_provider(normalized_id)
        if candidate is None:
            rendered = []
            for item in self._candidate_list_provider()[:3]:
                candidate_id = _text(item.get("id"), limit=80)
                if candidate_id:
                    rendered.append(
                        f"skill_id={candidate_id!r}, "
                        f"slug={_text(item.get('slug'), limit=80)!r}, "
                        f"name={_text(item.get('name'), limit=160)!r}"
                    )
            hint = (
                " Available candidates: "
                + "; ".join(rendered)
                + ". Use an exact skill_id; do not guess or bypass the Skill marketplace."
                if rendered
                else ""
            )
            return self._error(
                "SEARCH_REQUIRED",
                "Search the Skill marketplace first and use an exact returned ID."
                + hint,
                skill_id=normalized_id,
            )

        normalized = {
            "id": normalized_id,
            "slug": _text(candidate.get("slug"), limit=80),
            "name": _text(candidate.get("name"), limit=160),
            "publisherDisplayName": _text(
                candidate.get("publisherDisplayName"), limit=160
            ),
            "currentVersion": _text(candidate.get("currentVersion"), limit=40),
        }
        if not normalized["slug"] or not normalized["name"]:
            return self._error(
                "INVALID_CANDIDATE",
                "The selected marketplace candidate is incomplete.",
                skill_id=normalized_id,
            )

        installed_name = self._installed_skill_name(normalized_id, normalized["slug"])
        if installed_name:
            return self._successful_result(
                normalized,
                status="already_installed",
                visible_name=installed_name,
            )

        if normalized_id not in self._approved_skill_ids:
            publisher = normalized["publisherDisplayName"] or "unknown publisher"
            return ToolResult(
                success=False,
                error=(
                    "USER_CONFIRMATION_REQUIRED: Skill marketplace installation "
                    "needs approval."
                ),
                permission_request={
                    "type": "permission_request",
                    "scope": "skillhub",
                    "requested_scope": f"install:{normalized_id}",
                    "reason": (
                        f"Install '{normalized['name']}' from the Skill marketplace, "
                        f"published by {publisher}"
                    ),
                    "temporary_supported": True,
                    "persistent_supported": False,
                    "skill_id": normalized_id,
                    "slug": normalized["slug"],
                    "name": normalized["name"],
                    "version": normalized["currentVersion"],
                },
                raw_output={
                    "type": SKILLHUB_INSTALLATION_TYPE,
                    "status": "confirmation_required",
                    "candidate": normalized,
                },
            )

        self._approved_skill_ids.discard(normalized_id)
        payload = {
            "skillId": normalized_id,
            "slug": normalized["slug"],
            "name": normalized["name"],
            "publisherDisplayName": normalized["publisherDisplayName"],
            "version": normalized["currentVersion"],
        }
        try:
            response = await asyncio.wait_for(
                self._installer(payload), timeout=self._timeout_seconds
            )
        except Exception:
            response = {"status": "unavailable"}

        status = response.get("status") if isinstance(response, dict) else None
        if status not in {"installed", "already_installed"}:
            if status == "unavailable":
                return self._error(
                    "INSTALL_UNAVAILABLE",
                    "The host could not install this Skill right now.",
                    skill_id=normalized_id,
                )
            detail = _text(
                response.get("error") if isinstance(response, dict) else None,
                limit=500,
            )
            return self._error(
                "INSTALL_FAILED",
                detail or "The host reported that installation failed.",
                skill_id=normalized_id,
            )

        skill_payload = response.get("skill") if isinstance(response, dict) else None
        installed_name = (
            _text(skill_payload.get("name"), limit=80)
            or _text(skill_payload.get("slug"), limit=80)
            if isinstance(skill_payload, dict)
            else ""
        ) or normalized["slug"]
        visible_name = self._refresh_skill(installed_name, normalized["slug"])
        return self._successful_result(
            normalized,
            status=status,
            visible_name=visible_name,
            installed_name=installed_name,
        )

    def _installed_skill_name(self, skill_id: str, slug: str) -> str:
        if self._skill_loader is None:
            return ""
        try:
            self._skill_loader.discover_skills()
            skill = self._skill_loader.get_skill(slug)
            if skill is None or skill.source != "user" or skill.skill_path is None:
                return ""
            marker = json.loads(
                (skill.skill_path.parent / ".skill-installation.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError, TypeError):
            return ""
        if not isinstance(marker, dict):
            return ""
        return slug if marker.get("source") == "hub" and marker.get("skillId") == skill_id else ""

    @staticmethod
    def _successful_result(
        candidate: dict[str, str],
        *,
        status: str,
        visible_name: str,
        installed_name: str = "",
    ) -> ToolResult:
        visible = bool(visible_name)
        normalized_status = status if visible else "installed_not_visible"
        if visible:
            verb = "was already installed" if status == "already_installed" else "was installed"
            content = (
                f"Skill '{candidate['name']}' {verb} successfully and is available as "
                f"Skill '{visible_name}'. Call get_skill with skill_name={visible_name!r}, "
                "follow its instructions, and continue the original task. Do not construct "
                "executable paths from environment variables for optional validation."
            )
        else:
            content = (
                f"Skill '{candidate['name']}' was installed, but the live catalog cannot "
                "see it yet. Ask the user to start a fresh turn or restart the host."
            )
        return ToolResult(
            success=True,
            content=content,
            model_context=content,
            raw_output={
                "type": SKILLHUB_INSTALLATION_TYPE,
                "status": normalized_status,
                "candidate": candidate,
                "skillName": visible_name or installed_name,
                "items": [candidate],
            },
        )

    def _refresh_skill(self, *names: str) -> str:
        if self._skill_loader is None:
            return ""
        try:
            self._skill_loader.discover_skills()
        except Exception:
            return ""
        for name in names:
            if name.strip() and self._skill_loader.get_skill(name.strip()) is not None:
                return name.strip()
        return ""

    @staticmethod
    def _error(code: str, message: str, *, skill_id: str) -> ToolResult:
        return ToolResult(
            success=False,
            error=f"{code}: {message}",
            raw_output={
                "type": SKILLHUB_INSTALLATION_TYPE,
                "status": "failed",
                "code": code,
                "skillId": skill_id,
            },
        )


__all__ = [
    "SKILLHUB_INSTALL_CAPABILITY_VERSION",
    "SKILLHUB_INSTALL_METHOD",
    "SkillHubInstallTool",
]
