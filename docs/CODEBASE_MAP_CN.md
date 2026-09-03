# Box-Agent 代码导览

本文按“谁拥有状态、谁只做翻译”的顺序解释项目逻辑，并列出核心运行时中每个手写源文件的职责。生成物、静态资源和测试夹具不逐文件复述：它们分别由生成器、所属 Skill 契约和同名 `tests/test_<area>.py` 约束。

## 一条请求怎样运行

1. CLI、ACP 或 SDK 把输入翻译成 `RunRequest`，交给 `KernelAgentService`。
2. Service 校验 Session 的固定工作区、请求幂等键、租约和恢复事实，再让 `PluginKernelComposer` 从类型化注册表解析本轮能力。
3. 唯一的 `AgentLoopKernel` 依次组装 Context、请求 LLM、校验并执行 Tool、提交有序事件，并询问 Workflow Plugin 是否继续、暂停或结束。
4. Session、Event、Checkpoint、Effect 和 Lease 进入同一持久化模型；恢复时从这些事实重建，不存在第二套 `SessionLog` 或备用 Loop。
5. Adapter 只把事件渲染成终端文本、ACP update 或 SDK 返回值，不复制权限、工作流、Provider 或恢复逻辑。

## 稳定内核与协议

| 文件 | 作用 |
| --- | --- |
| `box_agent/api/__init__.py` | 汇总并稳定导出公共协议。 |
| `api/contracts.py` | Message、Run、Session、Tool、Artifact、Usage 等不可变 DTO。 |
| `api/events.py` | 定义有序、可持久化的 `AgentEvent`。 |
| `api/controls.py` | 外部控制命令及幂等 ACK。 |
| `api/errors.py` | 跨宿主稳定错误码和异常。 |
| `api/handles.py` | `AgentLoop`、`AgentService`、`AgentRunHandle` 抽象。 |
| `api/ports.py` | Context、Tool、Permission、Memory、LLM、Hook、Workflow SPI。 |
| `api/workflows.py` | 工作流动作、继续、暂停和 checkpoint DTO。 |
| `api/permissions.py` | 权限请求、决定和作用域协议。 |
| `api/plan.py` | Plan 的宿主展示与审批载荷。 |
| `api/artifact_context.py` | 产物根目录和发布上下文。 |
| `api/tool_context.py` | 单次工具调用可见的 Run/Session 上下文。 |
| `kernel/__init__.py` | 导出唯一 Kernel 入口。 |
| `kernel/loop.py` | 唯一执行状态机：Context → Model → Tool → Workflow → Terminal。 |
| `kernel/composer.py` | 从 PluginHost 解析本轮端口并构造 Kernel。 |
| `kernel/workflow_composite.py` | 通用聚合多个 WorkflowPolicy，不导入具体产品策略。 |
| `kernel/model_stream.py` | 模型流活动监控、stale 检测和 chunk 规范化。 |
| `kernel/model_recovery.py` | Provider 中断、截断和重试的有界恢复。 |
| `services/__init__.py` | 导出服务层入口。 |
| `services/kernel.py` | Session/Run 生命周期、固定工作区、幂等、重放、租约、控制和恢复。 |
| `services/delegation.py` | 子任务/子 Agent 的服务级委派编排。 |
| `services/workspace_profiles.py` | 工作区运行档位和配置快照。 |
| `services/utility_prompt.py` | 工具型/辅助模型请求的最小提示组装。 |
| `services/follow_up_suggestions.py` | Run 结束后的后续建议生成。 |

## 插件、上下文、记忆与权限

| 文件 | 作用 |
| --- | --- |
| `plugins/__init__.py` | 插件公共导出。 |
| `plugins/api.py` | Plugin manifest、依赖和生命周期协议。 |
| `plugins/registry.py` | 有作用域、冲突检查的类型化注册表。 |
| `plugins/host.py` | 插件发现、依赖解析、激活、释放与 lock snapshot。 |
| `plugins/hooks.py` | Hook 注册和组合辅助。 |
| `plugins/builtins.py` | CLI/ACP 共用的内置能力与 Workflow 组合。 |
| `context/__init__.py` | Context 能力公共导出。 |
| `context/api.py` | Context item/provider/compactor 协议。 |
| `context/composite.py` | 按顺序组合多个 Context contributor。 |
| `context/in_memory.py` | 参考内存 ContextEngine。 |
| `context/model_history.py` | 模型历史、占位符和指令保留规则。 |
| `context/resource_ledger.py` | 大结果资源收据与可重建内容账本。 |
| `context/environment.py` | 面向用户的环境/附件查找上下文。 |
| `context/session_environment.py` | Session 固定环境信息。 |
| `context/session_mode.py` | 会话模式提示贡献。 |
| `context/project.py` | 项目根和项目说明上下文。 |
| `context/task.py` | Session/Task/Turn 身份验证。 |
| `context/skills.py` | 当前激活 Skill 的固定指令上下文。 |
| `context/action_hints.py` | 宿主动作提示上下文。 |
| `context/experts.py` | 专家元数据上下文。 |
| `context/evidence.py` | 检索证据标准化。 |
| `memory_engine/__init__.py` | Memory 能力公共导出。 |
| `memory_engine/api.py` | recall/write/flush SPI。 |
| `memory_engine/composite.py` | 拆分并组合 Memory provider/store/writer。 |
| `memory_engine/in_memory.py` | 测试和嵌入使用的内存实现。 |
| `memory_engine/store.py` | 长期记忆索引、检索、写入和兼容只读入口。 |
| `memory_engine/extraction.py` | 从会话事件抽取记忆候选。 |
| `memory_engine/maintenance.py` | 衰减、去重、压缩和维护任务。 |
| `memory_engine/proposals.py` | 记忆晋升提议与状态转换。 |
| `permissions/__init__.py` | 权限能力导出。 |
| `permissions/gateway.py` | Tool 执行前 fail-closed 权限网关。 |
| `permissions/session.py` | Session 级授权和一次性决定状态。 |

## 持久化

| 文件 | 作用 |
| --- | --- |
| `persistence/__init__.py` | 持久化公共导出。 |
| `persistence/api.py` | Store、事务和 RecoveryBundle 协议。 |
| `persistence/sqlite.py` | Session/Event/Checkpoint/Effect/Lease/ACK 的共享 SQLite 事务事实源。 |
| `persistence/sessions.py` | Session 元数据和固定身份持久化。 |
| `persistence/event_log.py` | 有序事件追加、读取和 committed-prefix 校验。 |
| `persistence/checkpoints.py` | Run checkpoint 存取和摘要校验。 |
| `persistence/effects.py` | 外部副作用 prepare/running/complete/unknown 账本。 |
| `persistence/leases.py` | 单写者租约、续租和失效检测。 |
| `persistence/session_continuation.py` | 有界宿主续跑快照。 |
| `persistence/task_registry.py` | 任务与产物 lineage。 |
| `persistence/workspace_registry.py` | 工作区档位原子存储。 |
| `persistence/workflow_checkpoint_store.py` | 工作流暂停/恢复状态。 |
| `persistence/workflow_owner_store.py` | 可信工作流所有者，优先于产物猜测。 |
| `persistence/artifacts.py` | Artifact envelope、扫描、命名和校验。 |
| `persistence/roadmap_artifacts.py` | Roadmap 类产物元数据。 |
| `persistence/artifact_processor.py` | 事件到产物记录的处理。 |

## LLM 层

| 文件 | 作用 |
| --- | --- |
| `llm/__init__.py` | 轻量公共 facade，避免提前加载所有 Provider SDK。 |
| `llm/base.py` | LLM client/chunk/message 基类和公共请求参数。 |
| `llm/anthropic_client.py` | Anthropic wire、流式响应和动态鉴权。 |
| `llm/openai_client.py` | OpenAI 兼容 wire、GLM/SenseNova 方言和动态鉴权。 |
| `llm/binding.py` | 把宿主模型 binding 规范化并绑定到本轮 client。 |
| `llm/model_profiles.py` | 校验和解析不可变 `profileId + profileRevision` 模型档案。 |
| `llm/capabilities.py` | 图片、thinking、工具等模型能力判断。 |
| `llm/model_routing.py` | 在宿主允许候选中选择辅助/视觉模型。 |
| `llm/llm_wrapper.py` | 兼容统一调用 facade。 |
| `llm/lightweight.py` | 轻量辅助请求。 |
| `llm/error_messages.py` | Provider 异常分类和面向用户的稳定错误。 |
| `llm/retry.py` | 可重试错误和退避策略。 |
| `llm/async_utils.py` | 异步流/取消辅助。 |
| `llm/think_tag_splitter.py` | 拆分文本中的 thinking 标签。 |
| `llm/token_meter.py` | usage 和 token 预算计量。 |
| `llm/debug_logging.py` | 脱敏 Provider 调试日志。 |

## Tool 层

Tool 的固定执行顺序是 schema validate → permission preflight → hook/effect fence → executor → normalized result。

| 文件 | 作用 |
| --- | --- |
| `tools/__init__.py` | Tool 公共导出。 |
| `tools/base.py` | Tool/ToolResult 基类和统一 invoke 边界。 |
| `tools/engine.py` | RegistryToolEngine、权限、Hook、副作用栅栏和执行。 |
| `tools/registration.py` | Tool 注册与别名冲突检查。 |
| `tools/schema_validation.py` | 调用前 JSON Schema 验证。 |
| `tools/argument_limits.py` | 工具参数硬上限。 |
| `tools/runtime_context.py`、`runtime.py` | 绑定调用上下文和 Skill runtime 环境。 |
| `tools/setup.py` | 组装 CLI/ACP 共用的工作区工具。 |
| `tools/session_workspace.py`、`workspace.py` | 为 Session 创建作用域正确的工具集合。 |
| `tools/file_tools.py` | search/write/append/edit；分块写采用原子提交和回滚。 |
| `tools/file/read_tool.py` | 有界文本读取。 |
| `tools/file/jsonl_tool.py` | JSONL 字段投影和游标查询。 |
| `tools/file/path_candidates.py` | 缺失路径的有界候选。 |
| `tools/file/__init__.py` | 文件工具导出。 |
| `tools/bash_tool.py` | 前后台 Shell、可配置超时、危险命令和产品级拦截。 |
| `tools/_win_job.py` | Windows Job Object 进程树回收。 |
| `tools/shell_inspection.py`、`safety.py` | 结构化命令检查、作用域判断与删除前备份。 |
| `tools/pptx_safety.py` | PPTX self-check 与 image-status 绕过拦截。 |
| `tools/jupyter_tool.py` | 隔离 Jupyter kernel、代码执行和产物发现。 |
| `tools/image_generation_tool.py` | 图像生成/编辑服务调用。 |
| `tools/image_inspection_tool.py` | 指令驱动的只读视觉检查。 |
| `tools/vision_review_tool.py` | 旧 Python 名称到 `inspect_images` 的导入兼容。 |
| `tools/mcp_loader.py` | MCP 连接、远端名保留、错误和 OAuth 映射。 |
| `tools/mcp_tool_catalog.py` | 延迟加载 MCP catalog。 |
| `tools/mcp_exposure_engine.py` | Session 级 MCP 激活和动态透传。 |
| `tools/mcp_tool_search.py` | 搜索并激活延迟 MCP 工具。 |
| `tools/mcp_bootstrap.py`、`mcp_config_tool.py` | 托管 MCP 配置同步和配置工具。 |
| `tools/skillhub_search_tool.py` | 对宿主 SkillHub 做一次/Run 的有界只读搜索。 |
| `tools/skillhub_install_tool.py` | 候选绑定、确认后委托宿主安装并刷新 catalog。 |
| `tools/skillhub_contributor.py` | 根据宿主协商能力按 Session 注入 SkillHub Tool/Context。 |
| `tools/skill_loader.py`、`skill_preload.py`、`skill_tool.py` | Skill 发现、预载、选择和指令注入。 |
| `tools/skill_execution_env.py`、`skill_scratch.py` | Skill 子进程环境和私有 scratch 目录。 |
| `tools/sub_agent_tool.py`、`sub_agent_capabilities.py` | 子 Agent 调用与运行时派生的最小权限。 |
| `tools/plan_tool.py`、`todo_tool.py` | Plan/Todo 状态工具。 |
| `tools/request_user_input_tool.py`、`request_user_decision_tool.py` | 可恢复的补充输入和结构化决策边界。 |
| `tools/memory_tool.py` | Memory 搜索/读写工具。 |
| `tools/execution_result_tool.py` | 宿主中立的执行收据。 |
| `tools/result_storage.py`、`staged_file_write_tool.py` | 大结果落盘和阶段性文件写入。 |
| `tools/browser_intent.py`、`browser_runtime_scope.py`、`browser_tool_names.py` | 浏览器意图、运行作用域和工具名规则。 |
| `tools/obsidian_tool.py`、`schedule_tool.py` | Obsidian 与计划任务原生工具。 |
| `tools/experts.py`、`model_tool_context.py` | 专家工具和模型可见工具上下文。 |
| `tools/permissions.py` | Tool 层权限适配。 |
| `tools/watermark.py` | 产物水印辅助。 |

## Workflow 层

| 文件 | 作用 |
| --- | --- |
| `workflows/__init__.py` | Workflow 公共导出。 |
| `workflows/contract.py` | 稳定 Workflow SPI/DTO 的兼容导出。 |
| `workflows/composite.py` | Kernel 通用组合器的兼容入口。 |
| `workflows/hooks.py` | 把有序 Kernel 事件转给 Workflow Plugin。 |
| `workflows/selection.py`、`routing.py` | 根据 RunOptions/意图选择工作流。 |
| `workflows/state.py` | 通用 Workflow 状态容器。 |
| `workflows/guards.py` | 完成证据、预算和安全的纯判断。 |
| `workflows/completion.py`、`completion_gate.py` | 通用 Completion Gate 组装、继续和暂停。 |
| `workflows/completion_intent.py`、`delivery.py` | 交付意图与分句级请求识别。 |
| `workflows/controlled_presentation.py` | PPT 研究、生成、修复、pending write 和交付状态机。 |
| `workflows/presentation_checkpoint.py` | PPT checkpoint、重放和继续提示。 |
| `workflows/presentation_contract.py` | PPT 工具/产物契约。 |
| `workflows/presentation_preflight.py` | 开始生成前的输入与环境检查。 |
| `workflows/presentation_provider.py` | PPT Provider/模型策略。 |
| `workflows/presentation_recovery.py` | PPT 中断后的恢复辅助。 |
| `workflows/presentation_routing.py` | PPT 请求路由和受限工具选择。 |
| `workflows/external_skill.py` | 第三方 Skill 的通用生命周期和产物门禁。 |
| `workflows/goal.py`、`plan.py` | Session 级 Goal/Plan 状态、工具、控制和恢复。 |
| `workflows/response_continuation.py` | 截断回复的一次性/有界续写。 |
| `workflows/attachment.py` | 附件驱动工作流提示。 |
| `workflows/browser.py` | 浏览器工作流策略。 |
| `workflows/execution_profile.py` | 执行档位和预算。 |
| `workflows/turn_policy.py` | 每轮任务类型与执行策略分类。 |

## Adapter、ACP 与兼容层

| 文件 | 作用 |
| --- | --- |
| `adapters/service.py`、`hosts.py` | 协议中立的 CLI/ACP/SDK 事件消费 facade。 |
| `adapters/plugin_host.py` | 用配置构造默认 PluginHost。 |
| `adapters/capabilities.py` | 把具体能力适配为稳定 Port；包含模型 profile 选择。 |
| `adapters/projections.py` | 稳定事件到宿主展示数据的投影。 |
| `adapters/extensions.py`、`builtin_extensions.py` | 宿主扩展 RPC 注册和内置扩展。 |
| `adapters/acp_kernel.py` | Kernel 事件与 ACP session/prompt/control 的互译。 |
| `adapters/acp_metadata.py`、`acp_projection.py` | ACP 元数据校验和 update 投影。 |
| `adapters/acp/kernel.py` | ACP adapter 的包内兼容入口。 |
| `adapters/cli/app.py` | CLI 命令、输入、配置管理与终端渲染。 |
| `adapters/cli/kernel_runtime.py` | CLI 的 Service/Plugin 组装。 |
| `adapters/cli/memory_proposal*.py` | CLI 记忆提议协议与实现。 |
| `adapters/cli/permission_broker*.py` | CLI 权限询问协议与实现。 |
| 各级 `__init__.py` | 只做稳定导出，不拥有执行逻辑。 |
| `acp/bootstrap.py` | 先完成 stdio 握手，再后台加载重依赖。 |
| `acp/protocol.py`、`server.py` | ACP JSON-RPC 协议和服务入口。 |
| `acp/kernel_runtime.py` | 把配置/插件组装成 ACP 可用 Service。 |
| `acp/runtime_entry.py` | console/frozen runtime 启动。 |
| `acp/stdio_compat.py` | stdio 平台兼容。 |
| `acp/env_context.py`、`project_context.py` | ACP 特有输入转共享 Context。 |
| `acp/action_hints.py` | ACP 动作提示投影。 |
| `acp/debug_logger.py` | 保持 stdout 纯净的 stderr 调试日志。 |
| `compat/_forward.py` | 通用延迟转发辅助。 |
| `compat/agent.py`、`core.py`、`runtime.py` | 历史调用形状转 `RunRequest/AgentEvent`。 |
| `compat/acp.py`、`cli.py` | 历史 ACP/CLI 导入路径。 |
| `compat/events.py`、`hooks.py` | 历史事件/Hook 形状转换。 |
| `compat/goal.py`、`workflow_policy.py` | 历史 Goal/Workflow 导入兼容。 |

## 其他包与根 facade

| 路径 | 作用 |
| --- | --- |
| `observability/logger.py`、`redaction.py` | 结构化日志与凭据脱敏。 |
| `observability/session_trace.py`、`session_trace_hook.py` | JSONL 诊断 trace 与 Hook。 |
| `observability/agent_logger_hook.py` | Agent 生命周期日志 Hook。 |
| `observability/cache_fingerprint.py` | 请求/提示/工具 catalog 指纹。 |
| `mcp_servers/web_extract.py`、`web_extract_server.py` | Web 抽取实现与独立 MCP stdio 服务。 |
| `schema/schema.py` | 旧工具 schema 辅助；新协议优先使用 `api/` DTO。 |
| `utils/terminal_utils.py` | 终端显示小工具。 |
| `trace_viewer/launcher.py`、`server.py` | 离线 trace viewer 启动与 loopback-only 服务。 |
| `trace_viewer/index.html`、`app.js`、`trace_model.js`、`styles.css` | 只读 trace 前端。 |
| `config/system_prompt.md`、`analysis_prompt.md`、`code_prompt.md` | 基础、分析和编码提示模板。 |
| `config/config-example.yaml`、`mcp-example.json` | 不固定运行时默认值的配置示例。 |
| `box_agent/config.py` | Pydantic 配置模型、默认值和 YAML 解析。 |
| `box_agent/auth.py` | Hosted JWT 检查、并发去重刷新和原子凭据替换。 |
| `box_agent/build_runtime_cli.py` | 独立运行时构建命令入口。 |
| `box_agent/loop_guards.py` | 与兼容调用共用的截断/循环纯判断。 |
| `box_agent/client_info.py` | 客户端版本与宿主信息。 |
| `box_agent/__init__.py` | 包版本和稳定公共导出。 |
| `agent.py`、`core.py`、`runtime.py`、`cli.py` | 根级兼容/可执行 facade，不是第二套实现。 |
| 其余根级 `.py` | 将历史 import 转发到 `context/`、`memory_engine/`、`persistence/`、`workflows/` 或 `observability/` 的同名新所有者。 |

## Skills、测试、脚本和文档

- `box_agent/skills/<skill>/SKILL.md` 是单个 Skill 的入口；`references/` 是按需加载说明，`scripts/` 是确定性实现，`assets/`/`themes/`/`layouts/` 是资源。`_manifest.json` 只能由 `scripts/generate_skills_manifest.py` 生成。
- `document-skills/pptx/` 的 JS/CSS/layout/theme 文件实现受控 HTML→PPTX 契约；最终安全同时由 Workflow 和 Tool guard 保证。
- `_midu_shared/` 提供五个 Midu 市场 Skill 共用的认证与调用代码；各 `midu-*` 目录只声明各自能力。
- `tests/test_<area>.py` 对应同名模块行为；`tests/parity/` 固化多入口等价性；`tests/e2e/` 验证真实 ACP 握手与事件序列。
- `scripts/` 放生成、构建、探针和审计脚本；`docker/` 放容器构建资源；`general_review/ci/preflight.sh` 是本地确定性 Review 门禁。
- `workspace/`、`output/`、`.box-agent/`、trace、cache 和虚拟环境都是本地运行状态，不是提交源代码。

需要判断改动归属时，先问：它是稳定状态机不变量、可替换能力、产品工作流策略，还是宿主协议翻译？答案分别对应 `api/kernel/services`、能力包、`workflows`、`adapters/acp`。
