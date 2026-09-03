# Box-Agent ACP 对接索引

本目录下与 ACP 宿主对接相关的协议文档总索引。这些扩展从 Box-Agent 0.8.26
开始逐步加入；每份文档中的兼容性小节才是对应能力的版本边界。CLI 不消费 ACP
wire 格式，但 CLI、ACP 与 SDK 都共享 `box_agent.api` 契约、
`KernelAgentService`、`AgentLoopKernel` 和同一组能力插件。

> ACP 入口：`box-agent-acp` 走 stdio JSON-RPC。
> 公共扩展点：`session/new._meta`（一次性配置）、`session/prompt._meta`（每轮可变）、`update_tool_call.rawOutput`（结构化产物）。

工作流可在 `session/new._meta.workflow_id` 设置会话默认值；后续
`session/prompt._meta.workflow_id` 可按轮覆盖。适配器会把该选择转换为
`RunOptions.workflow_id`，只从已注册的 Workflow Plugin 组装 Kernel，未注册的
ID 会显式报错，不会静默退回其他工作流。

Kernel ACP runtime 还注册了显式的 `goal`、`autopilot`、`plan` Workflow
Plugin key；第三方可在 `_meta.workflow_id` 选择它们，直接调用新的状态/续跑
协议。自由文本 `/goal` 和历史调用形状通过兼容 facade 进入同一组 Workflow
Plugin；Goal、Plan、PPT、Skill、Completion Gate 与 Autopilot 的事件流 parity
fixture 已通过，pre-Kernel fallback 已退役。

Native ACP 的无凭据端到端验收和静态事件报告见
[ACP E2E 指南](e2e/ACP_E2E_GUIDE_CN.md)；它是宿主接入前验证 session、权限、插件、
工作流续跑和恢复边界的最小回归集。

---

## 协议清单

| 协议                  | 方向        | 入口                                  | 文档                                                             | 用途                                                                              |
| --------------------- | ----------- | ------------------------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| **Action Hint**       | 后端 → 前端 | 模型 markdown 围栏块                  | [ACTION_HINT_PROTOCOL.md](./ACTION_HINT_PROTOCOL.md)             | 模型在合适场景输出 `action_hint` 块，前端解析为可点击设置入口                     |
| **Memory Match**      | 后端 → 前端 | `update_tool_call.rawOutput`          | [MEMORY_MATCH_PROTOCOL.md](./MEMORY_MATCH_PROTOCOL.md)           | 宿主展示本轮 `memory_search` 或后端自动匹配到的上下文记忆                         |
| **Memory Proposal**   | 双向        | `session/memory_proposal` + extension methods | [MEMORY_PROPOSAL_PROTOCOL.md](./MEMORY_PROPOSAL_PROTOCOL.md) | 记忆晋升提案的 push、list、apply 流程 |
| **Env Context**       | 前端 → 后端 | `session/new._meta.env_context`       | [ENV_CONTEXT_PROTOCOL.md](./ENV_CONTEXT_PROTOCOL.md)             | 宿主把 CLI 路径 / 平台 / 浏览器工具状态等已知事实喂给模型，避免它否认已可用的工具 |
| **Filesystem Policy** | 前端 → 后端 | `session/new._meta.filesystem_policy` | [FILESYSTEM_POLICY_PROTOCOL.md](./FILESYSTEM_POLICY_PROTOCOL.md) | 宿主声明 session 工作区根 + 额外允许目录，避免反复触发 `permission/request` 协商  |
| **Permission Mode**   | 前端 → 后端 | `session/new._meta.permission_mode`   | [FILESYSTEM_POLICY_PROTOCOL.md](./FILESYSTEM_POLICY_PROTOCOL.md) | `default` 显式受限；`full_access` 仅在服务端 `tools.allow_full_access` 已启用时生效 |
| **Artifact**          | 后端 → 前端 | `update_tool_call.rawOutput`          | [ARTIFACT_PROTOCOL.md](./ARTIFACT_PROTOCOL.md)                   | 宿主收集、解析并渲染 Agent 生成的文件产物                                          |
| **Host Progress**     | 后端 → 前端 | `update_tool_call.rawOutput`          | [integration/host-progress-events.md](./integration/host-progress-events.md) | 宿主分组渲染 sub-agent、plan、todo、goal、turn usage 等结构化执行状态 |
| **User Decision**     | 双向        | `update_tool_call.rawOutput` + `session/prompt._meta` | [USER_DECISION_PROTOCOL_CN.md](./USER_DECISION_PROTOCOL_CN.md) | Skill/模型发起结构化执行决策，宿主选择或按运行时批准的默认项超时续跑 |

> 已经存在但本次未变更的扩展点：`_meta.session_mode`（会话模式）、`_meta.deep_think`（深度思考开关）、`_meta.chatTemplateKwargs.thinking`（宿主思考开关）、`_meta.officev3_permissions_override`（已废弃）。`apiKey` 等凭据字段不会进入持久化 metadata。

---

## 典型对接场景

### 场景 A：用户首次打开应用

1. 宿主侧检测：`MEMORY.md` 是否已存在、各个 CLI 是否已安装、Chromium 是否已通过 `box-agent install-browser` 装好。
2. 创建会话时，把这些事实通过 `_meta.env_context` 一次性喂给后端。
3. 用户问"你好/你是谁"时，后端发现 MEMORY.md 稀缺 → system prompt 注入 onboarding hint 规则 → 模型在回复末尾输出 `action_hint{tab:"onboarding"}` → 前端渲染为"点击完善个人记忆"链接，点击打开设置页 onboarding tab。

### 场景 B：用户问"帮我打开 example.com"

1. 后端发现 mcp.json 中 playwright 缺失或 `disabled=true` → system prompt 注入 browser-tools hint 规则。
2. 模型回复："我目前还没有可用的浏览器工具" + `action_hint{tab:"browser-tools"}`。
3. 前端剥离 hint 块，原位渲染为可点击链接，点击打开设置页 browser-tools tab。

### 场景 C：用户问"用飞书 CLI 给我发条消息"

1. 宿主在 `env_context.cli["lark-cli"]` 里传了 bundled 路径。
2. 后端 sanitize（绝对路径校验、长度限制等）后写入 system prompt 的"可用 CLI"清单。
3. 模型不再说"你机器上没装"，而是直接通过 bash 工具调用绝对路径。

---

## 安全约束（最低限度）

- **`_meta.env_context.extras`** 不会进入 system prompt（仅入后端日志），但仍然在 ACP 入站记录中可见。**不要传 token / API key / 用户隐私字段。**
- **CLI value 必须是绝对路径，不能含控制字符 / 反引号。** 后端会单条丢弃违规条目并 WARNING 日志。
- **action_hint 块由模型生成，前端必须做白名单校验**（`tab` 只接受协议定义的值），未知 tab 应当忽略不报错。

详细规则见各协议文档第 2 节。

---

## 实现位置（后端）

```
box_agent/acp/
├── __init__.py            # 稳定入口与兼容导出；不加载重型运行时
├── bootstrap.py           # 建立 stdio、立即握手、按需转发给 Kernel Agent
├── kernel_runtime.py      # 组装 PluginHost / Service / Kernel；不处理协议帧
├── protocol.py            # bootstrap 与 ACP adapter 共用的握手能力声明
├── action_hints.py        # MEMORY 稀缺检测 + playwright disabled 检测 + prompt 段
└── env_context.py         # 宿主环境注入 schema + sanitize + markdown 渲染

box_agent/adapters/
├── acp_kernel.py          # ACP payload / callback 与稳定 Run/Event 契约互转
├── acp_projection.py      # AgentEvent → ACP update
├── service.py             # 协议无关 payload → AgentService 调用
├── hosts.py               # ACP / CLI / SDK 薄适配器
└── plugin_host.py         # 内置能力 → 类型化 PluginHost / KernelAgentService

tests/
├── test_acp_kernel_adapter.py
├── test_acp_projection.py
├── test_service_adapter.py
├── test_action_hints.py
└── test_env_context.py
```

---

## 版本与变更

| 版本   | 变更                                                                                                                                                                             |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Unreleased (`0c46137`) | ACP、CLI、SDK 与历史 API 统一进入 `KernelAgentService` / `AgentLoopKernel`；工作流 parity 通过，旧执行循环退役；`api/`、`plugins/`、`adapters/` 成为稳定扩展边界 |
| 0.8.38 | 新增 Memory Match 协议：`memory_search.rawOutput` 返回本轮显式搜索或后端自动匹配到的 context memory；core memory 只注入模型，不返回前端                                      |
| 0.8.29 | `PermissionEngine` 把 `~/.box-agent/` 视为引擎自有数据，所有 scope 下都默认放行（skills / runtime-packages / browsers / log / trash 等子目录不再触发 `permission/request` 弹窗） |
| 0.8.28 | system prompt 注入 skills 源目录，限定模型只从 `~/.box-agent/skills/`（user）和 builtin 包内目录加载 skill，禁止扫描其它路径                                                     |
| 0.8.27 | 新增 `_meta.filesystem_policy`（宿主声明 session 工作区根 + 额外允许目录）；修复 bash 路径提取裸系统根误报（`cd /; ls` → `/`）；权限拒绝诊断日志增强                             |
| 0.8.26 | 首次引入 `action_hint` 协议、`_meta.env_context`、`enable_mcp` 防护；env_context 包含输入校验与 extras 不进 prompt                                                               |
| 0.8.25 | `context_window` / `max_output_tokens` 配置化（与 ACP 无关）                                                                                                                     |
| 0.8.24 | `max_tokens` 截断防护（与 ACP 无关）                                                                                                                                             |

后续协议变更会在本表追加，并在对应协议文档第 1 节注明适用版本。
