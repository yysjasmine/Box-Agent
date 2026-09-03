from __future__ import annotations

from pathlib import Path

import pytest

from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skillhub_install_tool import SkillHubInstallTool

CANDIDATE = {
    "id": "6f6f41f0-572b-4e55-9b8b-137d4d9a54a5",
    "slug": "edge-tts",
    "name": "文字转语音",
    "publisherDisplayName": "林",
    "currentVersion": "1.0.0",
}


def _candidate_provider(skill_id: str):
    return CANDIDATE if skill_id == CANDIDATE["id"] else None


@pytest.mark.asyncio
async def test_install_requires_exact_returned_candidate_and_confirmation():
    calls = []

    async def installer(payload):
        calls.append(payload)
        return {"status": "installed"}

    tool = SkillHubInstallTool(
        installer,
        candidate_provider=_candidate_provider,
        candidate_list_provider=lambda: [CANDIDATE],
    )

    unknown = await tool.execute("文字转语音")
    pending = await tool.execute(CANDIDATE["id"])

    assert unknown.error.startswith("SEARCH_REQUIRED:")
    assert f"skill_id='{CANDIDATE['id']}'" in unknown.error
    assert pending.error.startswith("USER_CONFIRMATION_REQUIRED:")
    assert pending.permission_request["requested_scope"] == f"install:{CANDIDATE['id']}"
    assert calls == []


@pytest.mark.asyncio
async def test_confirmed_install_refreshes_catalog_and_exposes_skill(tmp_path: Path):
    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    loader = SkillLoader(sources=[(user_skills, "user")])
    loader.discover_skills()
    calls = []

    async def installer(payload):
        calls.append(payload)
        skill_dir = user_skills / "edge-tts"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: edge-tts\ndescription: Generate speech audio.\n---\n",
            encoding="utf-8",
        )
        return {"status": "installed", "skill": {"name": "edge-tts"}}

    tool = SkillHubInstallTool(
        installer,
        candidate_provider=_candidate_provider,
        skill_loader=loader,
    )
    permission = (await tool.execute(CANDIDATE["id"])).permission_request
    tool.approve_permission_request(permission)

    result = await tool.execute(CANDIDATE["id"])

    assert result.success
    assert calls[0]["skillId"] == CANDIDATE["id"]
    assert result.raw_output["skillName"] == "edge-tts"
    assert loader.get_skill("edge-tts") is not None


@pytest.mark.asyncio
async def test_approval_is_bound_to_exact_candidate():
    calls = []

    async def installer(payload):
        calls.append(payload)
        return {"status": "installed"}

    tool = SkillHubInstallTool(installer, candidate_provider=_candidate_provider)
    tool.approve_permission_request(
        {
            "scope": "skillhub",
            "requested_scope": "install:another-id",
            "skill_id": CANDIDATE["id"],
        }
    )

    result = await tool.execute(CANDIDATE["id"])

    assert result.permission_request is not None
    assert calls == []
