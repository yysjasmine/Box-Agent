# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## Response Language

默认使用中文回复。

## Implementation Principles

实现功能时必须同时考虑 macOS、Windows、Linux 三端，以及 CLI 与 ACP runtime
两种入口。共享行为应通过 API、Kernel、Service、Plugin 或 capability 模块实现；
CLI、ACP 和 SDK 只做协议适配，不维护分叉的 Agent 循环。

修改前先运行 `git status --short --branch`，保留工作区里与当前任务无关的修改。
分析、审查和状态查询保持只读；没有明确授权时，不提交、推送、发布或安装 runtime。

## Code Discovery With Understand Anything

调查代码路径、所有权、架构、依赖或影响范围时，先检查已提交的
`.understand-anything/knowledge-graph.json`，并比较图谱与
`.understand-anything/meta.json` 记录的 `gitCommitHash` 和当前源码。图谱只用于
导航，结论必须通过 `rg`、源码直读、聚焦测试、日志或运行探针确认。

共享图谱、metadata 与 fingerprints 必须由 Understand Anything 一起生成，不得
手工修改。若图谱缺失或过期，应说明限制；任务仍可继续时使用常规源码搜索。
除非任务明确要求，不启动 dashboard 或长时间索引。具体维护流程见
`docs/UNDERSTAND_ANYTHING_CN.md`。

## Collaboration and Review Rules

非简单改动使用 TPR 框架：

- Task：改什么行为、影响哪些入口、哪些内容不在范围内；
- Proof：准确列出测试、探针、日志、截图、生成产物或 runtime 证据；
- Risk：兼容性、打包/runtime、迁移、配置/密钥、回滚和跨仓库影响。

共享行为只实现一次。稳定 Kernel 不应承载可由 Tool、Skill、Hook、事件消费者、
运行选项、Completion Gate 或 `WorkflowPolicy` 表达的产品特定逻辑。

## Project Overview

Box-Agent 是一个支持 Anthropic、OpenAI-compatible 及第三方端点的 Agent runtime，
具备流式思考、工具调用、MCP、Skills、持久化会话、权限协商、Context/Memory、
工作流策略以及 CLI/ACP/SDK 多宿主适配能力。

## Build and Run

```bash
# Setup
uv sync

# Development CLI
uv run python -m box_agent.cli
uv run python -m box_agent.cli --help

# Installed entry points
box-agent
box-agent-acp

# Non-interactive and diagnostics
box-agent --task "do something"
box-agent setup
box-agent config
box-agent doctor
box-agent trace-viewer

# Focused architecture checks
uv run pytest tests/test_agent_loop_kernel.py tests/test_kernel_service.py -q
uv run pytest tests/test_acp_kernel_adapter.py tests/test_acp_projection.py -q

# Full suite
uv run pytest tests/ -v
```

Pytest 配置已启用 `asyncio_mode = "auto"`，异步测试无需重复添加 marker。

## Architecture

运行时只有一个执行所有者：`box_agent/kernel/loop.py` 中的
`AgentLoopKernel`。`box_agent/services/kernel.py` 的 `KernelAgentService`
负责会话、Run、事件提交与重放、控制命令、租约和恢复。CLI、ACP、SDK 与历史
`Agent.run_events()` API 都提交 `box_agent.api` 契约，并只渲染或投影
`AgentEvent`。

```text
box_agent/
├─ api/              稳定 DTO、事件、控制命令、端口和句柄
├─ kernel/           唯一 AgentLoopKernel、工作流组合与 run-scope DI
├─ services/         会话/运行生命周期、重放、控制、租约和子任务委派
├─ plugins/          manifest、类型化注册表、激活与释放
├─ adapters/         CLI、ACP、SDK 的协议适配层
├─ context/          Context SPI、贡献器与确定性压缩
├─ memory_engine/    Memory SPI、存储、抽取、整合与维护
├─ permissions/      默认拒绝的权限策略与协商 gateway
├─ persistence/      Session/Event/Checkpoint/Effect/Lease 持久层
├─ tools/            注册表驱动的工具引擎及具体工具
├─ workflows/        Goal、Plan、PPT、Skill、Completion Gate 等策略
├─ llm/              Provider 客户端、重试、流式响应与 usage
├─ observability/    日志、session trace 与请求指纹
├─ acp/              ACP bootstrap、协议与 runtime 组装
└─ compat/           历史 API/import 形状到 Kernel 的兼容层
```

根目录的 `core.py`、`agent.py`、`runtime.py`、`events.py`、`cli.py` 以及若干
持久化模块是兼容或可执行 facade，不是第二套实现。详细所有权和依赖方向见
`docs/ARCHITECTURE_CN.md`，当前能力清单见
`docs/runtime-capability-matrix.md`。

## Runtime Invariants

- `box_agent/api/` 只放稳定、可序列化、与宿主无关的契约。
- Kernel 不导入 CLI、ACP 或具体工作流；`kernel/composer.py` 从
  `PluginHost` 的类型化注册表解析一次运行所需能力。
- Tool 固定经过：Schema 校验 → 权限预检 → `Hook.before_tool` → 修改后重验
  → effect fence → executor → `Hook.after_tool` → 结果归一化。
- Context、Memory、Permission、LLM、Tool、Workflow、Hook 与持久层都通过
  public port/registry 组合。第三方插件使用 `box_agent.plugins` entry point。
- 未注册的显式 `workflow_id`、未知 capability、plugin/schema replay 不匹配都
  fail closed，不静默降级为另一条执行路径。
- Event 具有稳定 identity 与单调 sequence；终态只出现一次。`attach()` 只观察，
  `resume()` 才显式取得执行租约。
- Session/Event/Checkpoint/Effect/Lease 持久化的是可重放事实，不保存 Python
  调用栈；恢复不得重复已经确认的副作用。
- 权限范围默认受 workspace 限制。宿主协商批准后才能重试被拒工具；非交互运行
  不得隐式放宽权限。
- 产物统一进入 `{workspace}/output/`。宿主只信任该目录下经过校验的 artifact
  envelope。
- ACP stdout 只允许协议帧，所有日志与第三方诊断输出写入 stderr。
- Goal、Plan、PPT、Skill、Completion Gate 和 Autopilot 都是原生 workflow
  plugin；parity 状态以 `tests/parity/migration_status.json` 为准。
- 内置 Skills 通过 `box_agent/skills/_manifest.json` 加载。修改后运行
  `uv run python scripts/generate_skills_manifest.py` 并检查 diff。
- LibreOffice (`soffice`) 和 Playwright Chromium 是外部运行依赖，不应假定已安装。

## Change Ownership and Proof

- API、Kernel、调度、取消、安全不变量：聚焦回归测试后运行相关 Kernel/ACP
  suite，条件允许时再跑全量测试。
- Tool：覆盖成功路径与重要失败路径；不要直接调用 executor 绕过工具引擎。
- Provider/LLM wire：覆盖正常、畸形响应和错误响应。
- Context/Memory/Persistence：覆盖配置开关、恢复、重放与持久化边界。
- CLI-only：使用聚焦 CLI 测试或捕获输出，并确认没有在 ACP 重复实现。
- ACP/host metadata：验证协议投影以及 stdout/stderr 边界。
- MCP：运行 loader/config 测试和有边界的状态或连接探针。
- 文档：检查路径、链接、命令，并运行 `git diff --check`。

对于 officev3 或其他打包宿主，分别报告：源码修改、源码测试、runtime 构建、
runtime 安装、探针、宿主重启、全新 live task。源码测试不能证明已安装 runtime
行为。

## Configuration

运行 `box-agent setup` 交互配置，或以
`box_agent/config/config-example.yaml` 为示例创建用户配置。Provider 选择和
`api_base` 会传给对应 LLM 客户端；第三方兼容端点的具体差异见
`docs/THIRD_PARTY_API_COMPATIBILITY.md`。MCP 示例位于
`box_agent/config/mcp-example.json`，实际用户配置位于
`~/.box-agent/config/mcp.json`。

## Publishing

没有用户明确授权时，不执行以下发布动作。

```bash
# 同步 pyproject.toml 与 box_agent/__init__.py 中的版本
uv run python scripts/generate_skills_manifest.py
uv build
uvx twine upload dist/box_agent-<version>*
gh release create v<version> dist/box_agent-<version>* \
  --repo Raccoon-Office/Box-Agent --title "v<version>"
```

### Standalone Runtime Build

```bash
uv run python scripts/build_runtime.py
# dist/runtime/box-agent-runtime-v{version}-{platform}-{arch}.tar.gz

gh release upload v<version> dist/runtime/box-agent-runtime-*.tar.gz \
  --repo Raccoon-Office/Box-Agent
```

Runtime 结构以生成的 manifest 为准；ACP binary 通过 stdio JSON-RPC 通信，
stdout 必须保持纯协议输出。关键入口：

- `scripts/build_runtime.py`：runtime 构建；
- `box_agent/acp/runtime_entry.py`：独立 runtime 入口；
- `box_agent/observability/logger.py`：共享日志；
- `box_agent/observability/session_trace.py`：可脱敏 JSONL trace。

PyPI: https://pypi.org/project/box-agent/

GitHub: https://github.com/Raccoon-Office/Box-Agent
