# ACP 端到端验收与可视化报告

这组测试从 ACP 适配器进入唯一的 Agent Kernel，使用确定性的 fake 插件，不需要网络、API Key 或真实模型。它验证的是公共协议事实，而不是私有类的调用细节。

## 一键运行

```powershell
python tests/e2e/run_acp_cases.py --report tests/e2e/report.json
python -m pytest tests/e2e -q
python -m http.server 8765 --directory tests/e2e
# 浏览器打开 http://localhost:8765/report.html
```

页面读取和 runner 同目录的 `report.json`。报告不存在或 JSON 损坏时，页面会显示可操作的空状态，而不是抛出脚本错误。若直接打开 `file://` 页面，使用“载入 report.json”选择器即可查看。
Runner 的 `--report` 只接受 `.json` 目标，避免误把测试数据写入并覆盖 `report.html`、CSS 或 JavaScript 页面资产。

## Case

| Case | 作用 | 验收事实 |
| --- | --- | --- |
| `text` | 最小 ACP → Kernel 回路 | `run.started`、模型事件、`run.completed` 顺序正确，最终文本稳定 |
| `tool_permission` | 工具和权限边界 | `permission.requested` 先于 executor；拒绝不产生副作用 |
| `context_memory` | Context/Memory 插件接入 | 看到 `memory.recalled`、`context.assembled` 和可恢复 manifest |
| `workflow_continuation` | WorkflowPolicy 延续 | `workflow.continuation.requested` 后仅追加受预算限制的下一 turn |
| `resume` | 任意边界恢复 | 新 Service 从 SQLite 恢复，事件序列和终态与第一次运行一致 |

`test_acp_stdio_smoke.py` 会启动真实子进程，通过 stdio 验证
`initialize → session/new`；其余确定性 case 不调用真实 LLM。

## 报告字段

每个 case 的记录形状如下：

```json
{
  "id": "text",
  "status": "passed",
  "duration_ms": 3.2,
  "events": [{"sequence": 1, "type": "run.started", "payload": {}}],
  "result": {"status": "completed", "final_message": "..."},
  "assertions": [{"name": "ordered_events", "passed": true}]
}
```

页面提供 case 筛选、事件时间线、payload 展开、断言失败详情和 JSON 复制；它是零依赖的静态 HTML，不会连接生产服务。

## 失败定位

- `permission-before-executor`：检查 Tool 是否实现 `preflight`，以及是否通过 `tools.engine.RegistryToolEngine` 注册。
- `context-manifest`：检查 Context Engine 是否返回 `ContextBuildResult`，Kernel 会为缺省 manifest 生成确定性 hash。
- `continuation-budget`：检查 WorkflowPolicy 的 `next_continuation` 是否返回稳定 `WorkflowContinuation`，以及 `RunOptions.max_steps`。
- `resume-events`：检查 SQLite 路径是否可写、插件 lock 是否一致；换版本插件会按设计 fail closed。

## 与行为门禁的关系

ACP E2E 验证协议到 Kernel 的公共边界；复杂工作流行为另由 `tests/parity/`
中的确定性 fixture 验证。`migration_status.json` 已记录 Goal、Plan、PPT、
Skill、Completion Gate、Autopilot 全部 `parity_passed`，旧 Loop 已退役。
