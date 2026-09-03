# 第三方 API 兼容性

## 事件顺序错误

### 问题描述

某些声称兼容 Anthropic 协议的第三方 API 可能发送不符合规范的 SSE 事件顺序。例如，在发送 `message_start` 事件之前就发送了 `content_block_start` 事件。

当这种情况发生时，anthropic SDK (v0.72.1+) 会抛出错误：
```
RuntimeError: Unexpected event order, got content_block_start before "message_start"
```

### 错误示例

```json
{
  "timestamp": "2026-06-12T08:29:49.292Z",
  "level": "DEBUG",
  "event": "llm/error_meta",
  "provider": "anthropic",
  "mode": "stream",
  "error_type": "RuntimeError",
  "error": "Unexpected event order, got content_block_start before \"message_start\""
}
```

### 解决方案

从 v0.8.68 开始，Box-Agent 会捕获这类错误并提供更友好的提示：

```
API 返回的事件顺序不符合 Anthropic 协议规范: Unexpected event order, got content_block_start before "message_start"
这通常表示第三方 API 的兼容性问题。请检查:
1. API 端点是否正确实现了 Anthropic 流式协议
2. 是否应该使用 OpenAI 兼容模式（provider: openai）而不是 Anthropic 模式
```

### 推荐操作

如果遇到此错误：

1. **检查 API 配置** - 确认使用的 API 端点是否真正支持 Anthropic 协议
2. **切换到 OpenAI 模式** - 如果 API 实际上是 OpenAI 兼容的，修改配置：
   ```yaml
   llm:
     provider: openai  # 而不是 anthropic
     api_base: "your-api-endpoint"
     model: "your-model"
   ```
3. **联系 API 提供商** - 报告事件顺序问题，要求修复协议兼容性

## Anthropic vs OpenAI 协议选择

### 何时使用 `provider: anthropic`

- 官方 Anthropic API (api.anthropic.com)
- 明确声称完全兼容 Anthropic 协议的第三方 API
- 需要使用 Anthropic 特有功能（如 thinking blocks）

### 何时使用 `provider: openai`

- OpenAI 官方 API
- 大多数国内大模型 API（如 DeepSeek、SiliconFlow、智谱等）
- 使用 OpenAI 兼容格式的第三方代理

### 诊断工具

运行 `box-agent doctor` 可以测试 API 连接性和基本兼容性。

## SenseNova OpenAI 兼容模式

以下是常见宿主/ACP 元数据的等价写法（它不是 `config.yaml` 的文件格式；
本地 YAML 仍使用 `api_key`/`api_base`）。`apiKey` 只能来自宿主的密钥管理，
不能写入事件、checkpoint 或日志）：

```json
{
  "protocol": "openai_chat_completions",
  "apiKey": "<provided-by-secret-manager>",
  "baseURL": "http://host/v1",
  "model": "SenseNova-Flash-Lite-...",
  "chatTemplateKwargs": {
    "thinking": true,
    "reasoningEffort": "high"
  }
}
```

适配边界只保留以下语义：

| 宿主字段 | Box-Agent 语义 | 是否进入 Provider wire |
| --- | --- | --- |
| `protocol` | 选择 `provider: openai` | 否 |
| `apiKey` | 初始化 Provider 的鉴权输入 | 仅作为 HTTP 鉴权；不进入事件/日志 |
| `baseURL` | `LLMConfig.api_base` | 是，作为请求基址 |
| `model` | `LLMConfig.model` | 是，作为模型名 |
| `chatTemplateKwargs.thinking` | `RunOptions.thinking_enabled` | 由 Provider 方言转换 |
| `chatTemplateKwargs.reasoningEffort` | 宿主提示；当前高推理开关由 `thinking=true` 决定 | 统一为 `reasoning_effort=high`（SenseNova） |

Native ACP 与兼容 ACP 都在适配器边界把 `chatTemplateKwargs` /
`chat_template_kwargs` 的 `thinking` 归一为 `RunOptions.thinking_enabled`；
显式的中性字段优先。凭据字段会在 session/run metadata 进入 Service 前被
递归剔除，防止持久化恢复或事件订阅泄露密钥。

当 `provider: openai` 且模型名以 `sensenova-` 或 `sn-sensenova-` 开头时，
Box-Agent 会启用 SenseNova 协议兼容处理。使用 `--deep-think` 或 ACP 的
`deep_think` 开关时，请求会附带：

```json
{
  "reasoning_effort": "high"
}
```

`chatTemplateKwargs` / `chat_template_kwargs` 不会原样透传到 SenseNova。
OpenAI Provider 按模型方言生成顶层 `reasoning_effort`；关闭思考时发送
`"none"`。这样 ACP、CLI、SDK 的输入命名可以不同，但实际 Provider wire
只有一套确定语义。

Native ACP 还接受兼容字段 `deepThink`；`adapters/acp_kernel.py` 会在进入
Kernel 前将这些拼写统一为 `RunOptions.thinking_enabled`，避免第三方
Provider 适配器重复解析 ACP 元数据。

部分 Flash-Lite 版本会把工具调用以 `<tool_call>` 标记输出到 reasoning，或
输出到不含其他可见文本的 content。Box-Agent 会把这类标记恢复为标准工具调用，
但仅接受当前步骤实际开放的 canonical tool name、显式 alias，以及它们的
下划线转连字符兼容形式。未声明的工具名和夹杂普通可见文本的内容不会执行，
仍作为文本返回。Provider-facing 工具 Schema 始终只包含 canonical name。
