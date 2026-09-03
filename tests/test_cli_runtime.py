"""CLI-mode runtime wiring tests."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import box_agent.cli as cli
import pytest
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import FunctionCall, LLMResponse, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.skill_loader import Skill, SkillLoader
from box_agent.tools.runtime import build_skill_runtime_context, build_skill_runtime_prompt
from box_agent.tools.setup import add_workspace_tools
from box_agent.tools.skill_tool import GetSkillTool
from box_agent.workspace_registry import WorkspaceRegistry


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_cli_parser_exposes_only_the_kernel_runtime(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["box-agent"])
    assert not hasattr(cli.parse_args(), "runtime")

    monkeypatch.setattr(sys, "argv", ["box-agent", "--runtime", "legacy"])
    with pytest.raises(SystemExit):
        cli.parse_args()


def _write_skill(
    skills_dir: Path,
    name: str,
    *,
    description: str,
    keywords: list[str],
    content: str,
    required_skills: list[str] | None = None,
) -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    required = (
        f"required_skills: [{', '.join(required_skills)}]\n"
        if required_skills
        else ""
    )
    skill_dir.joinpath("SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"keywords: [{', '.join(keywords)}]\n"
        f"{required}"
        "---\n"
        f"{content}\n",
        encoding="utf-8",
    )


def test_cli_node_execution_env_preserves_user_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8443")
    monkeypatch.setenv("npm_config_prefix", "/system/npm")
    monkeypatch.setenv("npm_config_cache", "/system/npm-cache")
    monkeypatch.setattr(
        cli,
        "build_skill_runtime_context",
        lambda **_kwargs: build_skill_runtime_context(
            sandbox_mode=False,
            node_runtime_root=tmp_path / "missing-node",
            office_node_runtime_root=tmp_path / "missing-office-node",
        ),
    )

    _npx, env = cli._cli_node_execution_env()

    assert env["HTTPS_PROXY"] == "http://proxy.example.test:8443"
    assert env["NPM_CONFIG_PREFIX"] == str(tmp_path / ".box-agent" / "skill-tools")
    assert "npm_config_prefix" not in env
    assert "npm_config_cache" not in env


class _CaptureStreamLLM:
    instances: list["_CaptureStreamLLM"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.system_prompts: list[str] = []
        self.message_snapshots: list[list[tuple[str, str]]] = []
        self.retry_callback = None
        self.instances.append(self)

    async def generate(self, *args, **kwargs):
        return LLMResponse(content="ok", finish_reason="stop")

    async def generate_stream(self, *, messages, **kwargs):
        self.message_snapshots.append([(message.role, message.content) for message in messages])
        self.system_prompts.append(messages[0].content)
        yield StreamEvent(type="text", delta="done.")
        yield StreamEvent(type="finish", finish_reason="stop")


class _PreloadedSkillThenGetSkillLLM(_CaptureStreamLLM):
    async def generate_stream(self, *, messages, **kwargs):
        self.message_snapshots.append([(message.role, message.content) for message in messages])
        self.system_prompts.append(messages[0].content)
        if len(self.message_snapshots) == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="preloaded-skill",
                        type="function",
                        function=FunctionCall(
                            name="get_skill", arguments={"skill_name": "pptx"}
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="text", delta="done.")
        yield StreamEvent(type="finish", finish_reason="stop")


def test_kernel_cli_conversation_keeps_one_durable_session_across_turns(
    tmp_path: Path,
) -> None:
    from box_agent.adapters.cli.kernel_runtime import KernelCLIConversation

    async def scenario() -> None:
        llm = _CaptureStreamLLM()
        config = Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(
                max_steps=2,
                workspace_dir=str(tmp_path),
                enable_memory=False,
                enable_memory_extraction=False,
                memory_maintainer_enabled=False,
                memory_promotion_proposal_enabled=False,
            ),
            tools=ToolsConfig(
                enable_file_tools=False,
                enable_bash=False,
                enable_todo=False,
                enable_plan=False,
                enable_sub_agent=False,
                enable_mcp=False,
                enable_skills=False,
                allow_full_access=True,
            ),
        )
        conversation = await KernelCLIConversation.create(
            workspace_dir=tmp_path,
            config=config,
            llm=llm,
            tools={},
            system_prompt="system",
            memory_manager=None,
            permission_negotiator=None,
            skill_loader=None,
            initial_goal=None,
            goal_autopilot_enabled=False,
        )
        try:
            first = await conversation.start_turn(
                "first question",
                completion_gate=None,
                force_plan_start=False,
                thinking_enabled=False,
            )
            _ = [event async for event in first.events()]
            await first.wait()
            second = await conversation.start_turn(
                "second question",
                completion_gate=None,
                force_plan_start=False,
                thinking_enabled=False,
            )
            _ = [event async for event in second.events()]
            await second.wait()

            assert conversation.turn_count == 2
            assert any(
                role == "user" and content == "first question"
                for role, content in llm.message_snapshots[1]
            )
            assert any(
                role == "assistant" and "done." in content
                for role, content in llm.message_snapshots[1]
            )
        finally:
            await conversation.close()

    asyncio.run(scenario())


class _EmptyFinalAnswerLLM:
    def __init__(self, *args, **kwargs) -> None:
        self.calls = 0

    async def generate_stream(self, *, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="echo-1",
                        type="function",
                        function=FunctionCall(
                            name="echo",
                            arguments={"text": "evidence"},
                        ),
                    )
                ],
            )
            return
        yield StreamEvent(type="finish", finish_reason="stop")


class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo input"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    async def execute(self, text: str) -> ToolResult:
        return ToolResult(success=True, content=text)


class _EOFPromptSession:
    prompt_count = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def prompt_async(self, *args, **kwargs) -> str:
        type(self).prompt_count += 1
        raise EOFError


class _ExplicitSkillPromptSession:
    prompt_count = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def prompt_async(self, *args, **kwargs) -> str:
        type(self).prompt_count += 1
        if type(self).prompt_count == 1:
            return "请用 /report-skill 生成报告"
        raise EOFError


def test_cli_ctrl_d_exits_without_empty_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    system_prompt_path = tmp_path / "system_prompt.md"
    system_prompt_path.write_text(
        "base system\n\n{SKILLS_METADATA}\n\n{SANDBOX_INFO}",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
            system_prompt_path=str(system_prompt_path),
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )

    async def fake_initialize_base_tools(*args, **kwargs):
        return [], None, None, None

    monkeypatch.setattr(
        cli.Config,
        "get_default_config_path",
        staticmethod(lambda: config_path),
    )
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(
        cli.Config,
        "find_config_file",
        staticmethod(
            lambda name: Path(name) if name == str(system_prompt_path) else None
        ),
    )
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "PromptSession", _EOFPromptSession)
    _EOFPromptSession.prompt_count = 0

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            sandbox_mode=False,
            verify_api=False,
            completion_gate_enabled=False,
            goal_autopilot_enabled=False,
        )
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert _EOFPromptSession.prompt_count == 1
    assert "Goodbye! Thanks for using Box Agent" in output
    assert "❌ Error:" not in output


def test_interactive_cli_passes_explicit_skill_gate_to_kernel_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    system_prompt_path = tmp_path / "system_prompt.md"
    system_prompt_path.write_text(
        "base system\n\n{SKILLS_METADATA}\n\n{SANDBOX_INFO}",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    skill_path = tmp_path / "skills" / "report-skill" / "SKILL.md"
    skill_loader = SkillLoader(skill_path.parent.parent)
    skill_loader.loaded_skills["report-skill"] = Skill(
        name="report-skill",
        description="Generate an HTML report.",
        content="Generate the requested report.",
        source="user",
        skill_path=skill_path,
    )
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
            system_prompt_path=str(system_prompt_path),
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=True,
            allow_full_access=True,
        ),
    )
    run_options: list[dict[str, object]] = []

    async def fake_initialize_base_tools(*args, **kwargs):
        return [GetSkillTool(skill_loader)], skill_loader, None, None

    from box_agent.api import RunResult
    from box_agent.adapters.cli.kernel_runtime import KernelCLIConversation

    class Handle:
        run_id = "interactive-skill-run"

        async def events(self):
            if False:
                yield None

        async def wait(self):
            return RunResult(status="completed", stop_reason="stop")

    async def fake_start_turn(self, text, **kwargs):
        run_options.append({"text": text, **kwargs})
        return Handle()

    monkeypatch.setattr(
        cli.Config,
        "get_default_config_path",
        staticmethod(lambda: config_path),
    )
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(
        cli.Config,
        "find_config_file",
        staticmethod(
            lambda name: Path(name) if name == str(system_prompt_path) else None
        ),
    )
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "PromptSession", _ExplicitSkillPromptSession)
    monkeypatch.setattr(KernelCLIConversation, "start_turn", fake_start_turn)
    _ExplicitSkillPromptSession.prompt_count = 0

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            sandbox_mode=False,
            verify_api=False,
            goal_autopilot_enabled=False,
        )
    )

    assert exit_code == 0
    assert len(run_options) == 1
    assert run_options[0]["text"] == "请用 /report-skill 生成报告"
    gate = run_options[0]["completion_gate"]
    assert gate.workflow_checkpoint_kind == "external_skill"
    assert gate.workflow_options["skill_name"] == "report-skill"


def test_cli_workspace_tools_receive_self_managed_node_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    node_root = tmp_path / ".box-agent" / "runtimes" / "node"
    node_bin = node_root / "versions" / "node-v22-test-darwin-arm64" / "bin"
    node = node_bin / "node"
    npm = node_bin / "npm"
    npx = node_bin / "npx"
    for path in (node, npm, npx):
        _make_executable(path)
    node_root.mkdir(parents=True, exist_ok=True)
    (node_root / "manifest.json").write_text(
        json.dumps(
            {
                "active": {
                    "version": "v22-test",
                    "node": str(node),
                    "npm": str(npm),
                    "npx": str(npx),
                }
            }
        ),
        encoding="utf-8",
    )

    runtime_context = build_skill_runtime_context(
        sandbox_mode=False,
        node_runtime_root=node_root,
    )
    tools = []
    add_workspace_tools(
        tools,
        Config(
            llm=LLMConfig(api_key="test-key"),
            agent=AgentConfig(workspace_dir=str(tmp_path / "workspace")),
            tools=ToolsConfig(enable_file_tools=False, enable_todo=False),
        ),
        tmp_path / "workspace",
        sandbox_mode=False,
        output=lambda _msg: None,
        skill_runtime_context=runtime_context,
    )

    bash_tool = next(tool for tool in tools if tool.name == "bash")
    assert bash_tool._subprocess_env["BOX_AGENT_NODE"] == str(node)
    assert bash_tool._subprocess_env["BOX_AGENT_NPM"] == str(npm)
    assert bash_tool._subprocess_env["BOX_AGENT_NPX"] == str(npx)
    skill_tools = tmp_path / ".box-agent" / "skill-tools"
    portable_node_layout = node.parent.name.casefold() == "bin"
    npm_global_modules = (
        skill_tools / "lib" / "node_modules"
        if os.name != "nt" or portable_node_layout
        else skill_tools / "node_modules"
    )
    assert bash_tool._subprocess_env["NODE_PATH"].split(os.pathsep) == [
        str(npm_global_modules),
        str(node_root / "sandbox" / "node_modules"),
    ]
    assert bash_tool._subprocess_env["NPM_CONFIG_CACHE"] == str(skill_tools / "npm-cache")
    assert bash_tool._subprocess_env["NPM_CONFIG_PREFIX"] == str(skill_tools)
    path_entries = bash_tool._subprocess_env["PATH"].split(os.pathsep)
    expected_npm_bin = skill_tools if os.name == "nt" else skill_tools / "bin"
    assert path_entries[0] == str(expected_npm_bin)
    assert str(node_bin) in path_entries

    prompt = build_skill_runtime_prompt(runtime_context)
    assert "- Node:" in prompt
    assert "标准 `node`/`npm`/`npx`" in prompt
    assert "$BOX_AGENT_NODE" in prompt


def test_cli_uses_saved_code_workspace_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    WorkspaceRegistry().set(workspace, "code")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    system_prompt_path = tmp_path / "system_prompt.md"
    system_prompt_path.write_text(
        "base system\n\n{SKILLS_METADATA}\n\n{SANDBOX_INFO}\n\n{FILE_DELIVERY_INFO}",
        encoding="utf-8",
    )
    code_prompt_path = tmp_path / "code_prompt.md"
    code_prompt_path.write_text(
        "## Software Engineering Mode (code_agent)\nCODE MODE MARKER",
        encoding="utf-8",
    )
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
            system_prompt_path=str(system_prompt_path),
            code_prompt_path=str(code_prompt_path),
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )
    workspace_tool_options: dict[str, object] = {}

    async def fake_initialize_base_tools(*args, **kwargs):
        return [], None, None, None

    def fake_add_workspace_tools(*args, **kwargs):
        workspace_tool_options.update(kwargs)

    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(
        cli.Config,
        "find_config_file",
        staticmethod(
            lambda name: Path(name)
            if name in {str(system_prompt_path), str(code_prompt_path)}
            else None
        ),
    )
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", fake_add_workspace_tools)
    _CaptureStreamLLM.instances.clear()

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task="fix the project",
            sandbox_mode=True,
            verify_api=False,
            completion_gate_enabled=False,
            goal_autopilot_enabled=False,
        )
    )

    assert exit_code == 0
    assert workspace_tool_options["use_output_dir"] is False
    system_prompt = _CaptureStreamLLM.instances[0].system_prompts[0]
    assert "Project Workspace Mode" in system_prompt
    assert "Software Engineering Mode (code_agent)" in system_prompt
    assert "CODE MODE MARKER" in system_prompt
    assert "Do not create or use an `output/` folder" in system_prompt


def test_cli_task_preloads_pptx_even_when_filter_drops_it(tmp_path: Path, monkeypatch) -> None:
    skills_dir = tmp_path / "skills"
    prompt = "做一份 12 页新员工入职培训 PPT，1920×1080 可编辑"
    for index in range(16):
        _write_skill(
            skills_dir,
            f"lark-noise-{index}",
            description="做一份 新员工 入职 培训 可编辑 会议室 HR 友好 流程 清单",
            keywords=["做一份", "新员工", "入职", "培训", "可编辑", "会议室", "HR"],
            content=f"# Noise {index}",
        )
    _write_skill(
        skills_dir,
        "pptx",
        description="Create editable PowerPoint PPTX slide decks.",
        keywords=["ppt", "pptx", "powerpoint", "slide"],
        required_skills=["html-templates"],
        content="# PPTX FULL RULES\nUse the editable deck workflow.",
    )
    _write_skill(
        skills_dir,
        "html-templates",
        description="Select visual style constraints for HTML slide decks.",
        keywords=["html", "template", "visual"],
        content="# HTML TEMPLATE RULES\nSelect a Visual DNA profile.",
    )
    skill_loader = SkillLoader(skills_dir)
    skill_loader.discover_skills()
    assert "pptx" not in [skill.name for skill in skill_loader.filter_by_query(prompt)]

    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    system_prompt_path = tmp_path / "system_prompt.md"
    system_prompt_path.write_text(
        "base system\n\n{SKILLS_METADATA}\n\n{SANDBOX_INFO}",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
            system_prompt_path=str(system_prompt_path),
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=True,
            allow_full_access=True,
        ),
    )

    async def fake_initialize_base_tools(*args, **kwargs):
        return [GetSkillTool(skill_loader)], skill_loader, None, None

    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(
        cli.Config,
        "find_config_file",
        staticmethod(lambda name: Path(name) if name == str(system_prompt_path) else None),
    )
    monkeypatch.setattr(cli, "LLMClient", _PreloadedSkillThenGetSkillLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    _CaptureStreamLLM.instances.clear()

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task=prompt,
            sandbox_mode=False,
            verify_api=False,
            goal_autopilot_enabled=False,
        )
    )

    assert exit_code == 0
    snapshots = _CaptureStreamLLM.instances[0].message_snapshots
    first_system_context = "\n\n".join(
        content for role, content in snapshots[0] if role == "system"
    )
    assert "# Skill: pptx" in first_system_context
    assert "# PPTX FULL RULES" in first_system_context
    assert "# Skill: html-templates" in first_system_context
    assert "# HTML TEMPLATE RULES" in first_system_context
    assert len(snapshots) == 2
    tool_messages = [content for role, content in snapshots[1] if role == "tool"]
    assert tool_messages == [
        "Skill 'pptx' is already preloaded in this session. "
        "Follow its system instructions directly."
    ]
    assert "# PPTX FULL RULES" not in tool_messages[0]


def test_cli_task_returns_failure_for_done_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=3,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )
    async def fake_initialize_base_tools(*args, **kwargs):
        return [_EchoTool()], None, None, None

    monkeypatch.setattr(
        cli.Config,
        "get_default_config_path",
        staticmethod(lambda: config_path),
    )
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(cli, "LLMClient", _EmptyFinalAnswerLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task="Use echo and summarize the result",
            sandbox_mode=False,
            verify_api=False,
            json_summary=True,
            completion_gate_enabled=False,
            goal_autopilot_enabled=False,
        )
    )

    output = capsys.readouterr().out
    summary = json.loads(output[output.rfind("\n{") + 1 :])
    assert exit_code == 1
    assert summary["ok"] is False
    assert summary["error"]
    assert summary["runStatus"] == "failed"
    assert summary["completed"] is False
    assert summary["error"]["code"] == "MODEL_PROVIDER_ERROR"


def test_cli_kernel_runtime_routes_task_through_plugin_service(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )

    class KernelLLM:
        def __init__(self, *args, **kwargs):
            pass

        async def generate_stream(self, messages, tools, **kwargs):
            yield StreamEvent(type="text", delta="kernel answer")
            yield StreamEvent(type="finish", finish_reason="stop")

    async def fake_initialize_base_tools(*args, **kwargs):
        return [], None, None, None

    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(cli, "LLMClient", KernelLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task="answer through kernel",
            sandbox_mode=False,
            verify_api=False,
            json_summary=False,
            completion_gate_enabled=False,
            goal_autopilot_enabled=False,
        )
    )

    assert exit_code == 0
    assert "kernel answer" in capsys.readouterr().out


def test_cli_kernel_routes_natural_plan_request_to_native_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=1,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=True,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )
    seen: dict[str, object] = {}

    async def fake_initialize_base_tools(*args, **kwargs):
        return [], None, None, None

    async def fake_run_kernel_task(**kwargs):
        seen.update(kwargs)
        return 0

    class DummyLLM:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(cli, "LLMClient", DummyLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_run_kernel_conversation_task", fake_run_kernel_task)

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task="请先制定计划",
            sandbox_mode=False,
            verify_api=False,
            json_summary=False,
            completion_gate_enabled=False,
            goal_autopilot_enabled=False,
        )
    )

    assert exit_code == 0
    assert seen["task"] == "请先制定计划"


def test_cli_kernel_routes_presentation_gate_to_native_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A controlled presentation gate is now a first-class Kernel policy."""

    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=1,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )
    seen: dict[str, object] = {}

    async def fake_initialize_base_tools(*args, **kwargs):
        return [], None, None, None

    async def fake_run_kernel_task(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(cli, "LLMClient", _CaptureStreamLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_run_kernel_conversation_task", fake_run_kernel_task)

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task="Create an investor presentation about the roadmap and market analysis",
            sandbox_mode=False,
            verify_api=False,
            json_summary=False,
            completion_gate_enabled=True,
            goal_autopilot_enabled=False,
        )
    )

    assert exit_code == 0
    assert seen["task"] == "Create an investor presentation about the roadmap and market analysis"
    gate = seen["completion_gate"]
    assert gate is not None
    assert gate.workflow_checkpoint_kind == "controlled_presentation"


def test_cli_kernel_rebinds_goal_and_plan_tools_to_session_plugins(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The native CLI must not register Agent-bound or global stateful tools."""

    from box_agent.api import RunResult

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=1,
            workspace_dir=str(tmp_path),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
            goal_autopilot_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=True,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )

    class StatefulTool(Tool):
        def __init__(self, name: str) -> None:
            self._name = name

        @property
        def name(self) -> str:
            return self._name

        @property
        def description(self) -> str:
            return self._name

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, **_kwargs) -> ToolResult:
            return ToolResult(success=True, content="legacy")

    from box_agent.adapters.cli.kernel_runtime import KernelCLIConversation

    async def scenario():
        conversation = await KernelCLIConversation.create(
            workspace_dir=tmp_path,
            config=config,
            llm=object(),
            tools={
            "goal_read": StatefulTool("goal_read"),
            "goal_write": StatefulTool("goal_write"),
            "plan_read": StatefulTool("plan_read"),
            "plan_write": StatefulTool("plan_write"),
            },
            system_prompt="system",
            memory_manager=None,
            permission_negotiator=None,
            skill_loader=None,
            initial_goal=None,
            goal_autopilot_enabled=False,
        )
        try:
            registry = conversation.host.registries["tools.executors"]
            tools = {
                name: registry.resolve(name, scope="run")
                for name in ("goal_read", "goal_write", "plan_read", "plan_write")
            }
            assert tools["goal_read"].__class__.__module__ == "box_agent.workflows.goal"
            assert tools["goal_write"].__class__.__module__ == "box_agent.workflows.goal"
            assert tools["plan_read"].__class__.__module__ == "box_agent.workflows.plan"
            assert tools["plan_write"].__class__.__module__ == "box_agent.workflows.plan"
            workflow = conversation.host.registries["workflows"].resolve(
                "default", scope="run"
            )
            assert [policy.kind for policy in workflow.policies] == [
                "browser_intent",
                "goal",
                "plan",
                "response_continuation",
            ]
        finally:
            await conversation.close()

    asyncio.run(scenario())


def test_cli_kernel_runtime_uses_native_generic_completion_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_key: test\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=2,
            workspace_dir=str(workspace),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )
    seen: dict[str, object] = {}

    class KernelLLM:
        def __init__(self, *args, **kwargs):
            pass

    async def fake_initialize_base_tools(*args, **kwargs):
        return [], None, None, None

    async def fake_run_kernel_task(**kwargs):
        seen["gate"] = kwargs["completion_gate"]
        return 0

    monkeypatch.setattr(cli.Config, "get_default_config_path", staticmethod(lambda: config_path))
    monkeypatch.setattr(cli.Config, "from_yaml", staticmethod(lambda _path: config))
    monkeypatch.setattr(cli, "LLMClient", KernelLLM)
    monkeypatch.setattr(cli, "initialize_base_tools", fake_initialize_base_tools)
    monkeypatch.setattr(cli, "add_workspace_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_run_kernel_conversation_task", fake_run_kernel_task)

    exit_code = asyncio.run(
        cli.run_agent(
            workspace,
            task="Create a markdown report artifact",
            sandbox_mode=False,
            verify_api=False,
            json_summary=False,
            goal_autopilot_enabled=False,
        )
    )

    gate = seen["gate"]
    assert exit_code == 0
    assert gate is not None
    assert getattr(gate, "workflow_checkpoint_kind", None) is None


def test_cli_kernel_renders_native_pause_message_and_metadata(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Native pause results remain visible and machine-readable at the CLI boundary."""

    from box_agent.api import RunResult

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=1,
            workspace_dir=str(tmp_path),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )

    class Handle:
        run_id = "native-pause-run"

        async def events(self):
            if False:
                yield None

        async def wait(self):
            return RunResult(
                status="completed",
                stop_reason="checkpoint_paused",
                final_message="计划已生成，等待用户确认后再执行。",
                metadata={
                    "runStatus": "paused",
                    "recoverable": True,
                    "planApproval": {"required": True, "state": "pending"},
                },
            )

    class Conversation:
        session_id = "session-1"

        async def start_turn(self, *_args, **_kwargs):
            return Handle()

        def record_result(self, result):
            self.result = result

        def current_goal(self):
            return None

    exit_code = asyncio.run(
        cli._run_kernel_conversation_task(
            conversation=Conversation(),
            workspace_dir=tmp_path,
            task="请先制定计划",
            config=config,
            completion_gate=None,
            force_plan_start=False,
            deep_think=False,
            json_summary=True,
            session_start=cli.datetime.now(),
        )
    )

    output = capsys.readouterr().out
    summary = json.loads(output[output.rfind("\n{") + 1 :])
    assert exit_code == 0
    assert "计划已生成，等待用户确认后再执行。" in output
    assert summary["ok"] is False
    assert summary["runStatus"] == "paused"
    assert summary["completed"] is False
    assert summary["recoverable"] is True
    assert summary["metadata"]["planApproval"]["state"] == "pending"
    assert summary["planApproval"]["required"] is True


def test_cli_kernel_renders_native_autopilot_stop_boundary(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    """Native autopilot stop causes remain visible outside JSON metadata."""

    from box_agent.api import RunResult

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=1,
            workspace_dir=str(tmp_path),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )

    class Handle:
        run_id = "native-autopilot-run"

        async def events(self):
            if False:
                yield None

        async def wait(self):
            return RunResult(
                status="completed",
                stop_reason="end_turn",
                final_message="done",
                metadata={
                    "goalAutopilot": {
                        "enabled": True,
                        "continuations": 1,
                        "stopCause": "no_progress",
                        "noProgressTurns": 1,
                    }
                },
            )

    result, _ = asyncio.run(cli._render_kernel_handle(Handle()))

    output = capsys.readouterr().out
    assert result.status == "completed"
    assert "without recorded goal progress" in output


def test_cli_kernel_renders_native_completion_gate_pause_boundary(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from box_agent.api import RunResult

    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=1,
            workspace_dir=str(tmp_path),
            enable_memory=False,
            enable_memory_extraction=False,
            memory_maintainer_enabled=False,
            memory_promotion_proposal_enabled=False,
        ),
        tools=ToolsConfig(
            enable_file_tools=False,
            enable_bash=False,
            enable_todo=False,
            enable_plan=False,
            enable_sub_agent=False,
            enable_mcp=False,
            enable_skills=False,
            allow_full_access=True,
        ),
    )

    class Handle:
        run_id = "native-completion-run"

        async def events(self):
            if False:
                yield None

        async def wait(self):
            return RunResult(
                status="completed",
                stop_reason="checkpoint_paused",
                final_message="paused",
                metadata={
                    "completionGate": {
                        "continuations": 1,
                        "maxContinuations": 1,
                        "budgetExhausted": True,
                        "gaps": ["tool `verify` is missing"],
                    }
                },
            )

    result, _ = asyncio.run(cli._render_kernel_handle(Handle()))

    output = capsys.readouterr().out
    assert result.status == "completed"
    assert "completion gate stopped" in output.lower()
