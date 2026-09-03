# Development Guide

## Table of Contents

- [Development Guide](#development-guide)
  - [Table of Contents](#table-of-contents)
  - [1. Project Architecture](#1-project-architecture)
  - [2. Basic Usage](#2-basic-usage)
    - [2.1 Interactive Commands](#21-interactive-commands)
    - [2.2 Integrated MCP Tools](#22-integrated-mcp-tools)
      - [Tavily - Web Search and Extraction](#tavily---web-search-and-extraction)
      - [Memory - MCP Knowledge Graph Server](#memory---mcp-knowledge-graph-server)
      - [Playwright - Browser Automation](#playwright---browser-automation)
  - [3. Extended Abilities](#3-extended-abilities)
    - [3.1 Adding Custom Tools](#31-adding-custom-tools)
      - [Steps](#steps)
      - [Example](#example)
    - [3.2 Adding MCP Tools](#32-adding-mcp-tools)
    - [3.3 Built-in Skills](#33-built-in-skills)
      - [Recommended Skills for officev3](#recommended-skills-for-officev3)
    - [3.4 Adding a New Skill](#34-adding-a-new-skill)
    - [3.5 Customizing System Prompt](#35-customizing-system-prompt)
      - [What You Can Customize](#what-you-can-customize)
  - [4. Troubleshooting](#4-troubleshooting)
    - [4.1 Common Issues](#41-common-issues)
      - [API Key Configuration Error](#api-key-configuration-error)
      - [Dependency Installation Failure](#dependency-installation-failure)
      - [MCP Tool Loading Failure](#mcp-tool-loading-failure)
    - [4.2 Debugging Tips](#42-debugging-tips)
      - [Enable Verbose Logging](#enable-verbose-logging)
      - [Using the Python Debugger](#using-the-python-debugger)
      - [Inspecting Tool Calls](#inspecting-tool-calls)

---

## 1. Project Architecture

The ownership rules, dependency direction, and stable integration API are
defined in the [Layered Architecture](ARCHITECTURE.md). Read it before adding
shared runtime behavior.

```
box-agent/
├── box_agent/
│   ├── api/                 # Stable DTOs, events, controls, ports, and handles
│   ├── kernel/              # The single AgentLoopKernel and run composition
│   ├── services/            # Session/run lifecycle, replay, leases, delegation
│   ├── plugins/             # Typed registries and plugin lifecycle
│   ├── adapters/            # Thin CLI, ACP, and SDK protocol adapters
│   ├── context/             # Context providers, contributors, and compaction
│   ├── memory_engine/       # Memory SPI, store, extraction, and maintenance
│   ├── permissions/         # Fail-closed permission policy and negotiation
│   ├── persistence/         # Sessions, events, checkpoints, effects, and leases
│   ├── tools/               # Tool engine and built-in/MCP implementations
│   ├── workflows/           # Goal, Plan, PPT, Skill, and completion policies
│   ├── llm/                 # Provider clients and streaming wrapper
│   ├── acp/                 # ACP bootstrap and protocol support
│   ├── compat/              # Historical API/import facades over the Kernel
│   └── skills/              # Built-in Skills and generated manifest
├── tests/                   # Unit, parity, and E2E coverage
├── docs/                    # Maintainer and integration documentation
├── workspace/               # Runtime scratch space; do not commit
└── pyproject.toml
```

`AgentLoopKernel` is the only execution owner. `KernelAgentService` owns
sessions, runs, replay, controls, and recovery. CLI, ACP, SDK, and the legacy
`Agent` API submit the same `box_agent.api` requests and only render or project
events. Root modules such as `core.py`, `agent.py`, and `cli.py` are
compatibility or executable facades; do not add a second loop to them.

## 2. Basic Usage

### 2.1 Interactive Commands

When running the agent in interactive mode (`box-agent`), the following commands are available:

| Command                | Description                                                 |
| ---------------------- | ----------------------------------------------------------- |
| `/exit`, `/quit`, `/q` | Exit the agent and display session statistics               |
| `/help`                | Display help information and available commands             |
| `/clear`               | Clear message history and start a new session               |
| `/clear_all`           | Clear message history and shut down the sandbox kernel      |
| `/history`             | Show the current session message count                      |
| `/stats`               | Display session statistics (steps, tool calls, tokens used) |
| `/sandbox_status`      | Show sandbox session status                                 |
| `/log`                 | Show log directory or read a specific log file              |
| `/goal`                | Show or manage the durable session goal                     |
| `/memory review`       | Review memory promotion candidates                          |

CLI management commands are also scriptable:

```bash
box-agent config --get model
box-agent config --set max_steps 300
box-agent config --json
box-agent doctor --json
box-agent --task "summarize README.md" --json
box-agent --task "create a PPT" --force-plan-start
box-agent --task "create a PPT" --no-completion-gate
box-agent --goal "Finish release checklist" --task "run verification"
box-agent --goal "Finish release checklist" --task "run verification" --no-goal-autopilot
box-agent goal status --json
box-agent goal progress "updated ACP docs"
box-agent goal complete --evidence "uv run pytest tests/ -q passed"
box-agent --deep-think --task "review this repository"
```

### 2.2 Integrated MCP Tools

This project ships a disabled-by-default MCP example configuration at `box_agent/config/mcp-example.json`.
Run `box-agent install-browser` to install Chromium and enable the Playwright entry in the user config.
Other MCP servers must be enabled explicitly in `~/.box-agent/config/mcp.json`.

#### Tavily - Web Search and Extraction

**Function**: Web search and content extraction via Tavily MCP.

**Status**: Disabled by default; requires a Tavily API key in the MCP URL.

#### Memory - MCP Knowledge Graph Server

**Function**: Optional Model Context Protocol memory server.

**Status**: Disabled by default. Box-Agent's built-in memory tools are separate from this MCP server and are controlled by `enable_memory`.

#### Playwright - Browser Automation

**Function**: Browser automation through `@playwright/mcp`.

**Status**: Disabled by default. Run `box-agent install-browser` to install Chromium and flip `mcpServers.playwright.disabled` to `false` in the user MCP config.

**Configuration Example**

```json
{
  "mcpServers": {
    "tavily": {
      "description": "Tavily - Web search and content extraction",
      "url": "https://mcp.tavily.com/mcp/?tavilyApiKey=YOUR_API_KEY",
      "type": "streamable_http",
      "disabled": false
    },
    "playwright": {
      "description": "Playwright - Browser automation (Chromium)",
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"],
      "disabled": false
    }
  }
}
```

## 3. Extended Abilities

### 3.1 Adding Custom Tools

#### Steps

1. Create a built-in tool under `box_agent/tools/`, or implement it in a
   third-party package.
2. Implement `Tool`, or provide a compatible object with `name`,
   `description`, `parameters`, and `execute`/`invoke`.
3. Register the executor in `tools.executors`. Register a separate
   `tools.descriptors` entry only when the model-facing schema has a different
   owner.
4. Compose the runtime with `KernelAgentService.from_plugin_host(host)`, or
   expose a distributable plugin through the `box_agent.plugins` entry-point
   group. Do not patch CLI and ACP separately for shared tools.

The runtime dispatches tool calls through `Tool.invoke(arguments)`. This
validates each call's arguments against `parameters` before delegating to the
tool's `execute()` implementation. Tool authors implement `execute()`; runtime
callers should use `invoke()` so they do not bypass argument validation.
Malformed parameter schemas fail closed with `INVALID_TOOL_SCHEMA`; schema and
argument values are omitted from that diagnostic.

Adapters that must invoke a deterministic Tool outside the agent loop, such as
ACP processing a structured attachment before the next model turn, must use
`box_agent.runtime.invoke_tool_with_permissions()`. It preserves the same
schema validation, host permission negotiation, bounded retry, and repeated-
request protection as model-selected tool calls. Calling `Tool.invoke()`
directly is appropriate only when no runtime permission request can occur.

#### Tool Names and Aliases

`Tool.name` is the canonical name serialized in the provider-facing tool
schema. A tool may additionally declare execution-only compatibility names:

```python
class MyTool(Tool):
    aliases = ("legacy_my_tool",)
```

For the canonical name and every declared alias, Box-Agent accepts the exact
name and a generated variant with every underscore replaced by a hyphen. For
example, the declaration above accepts `my_tool`, `my-tool`,
`legacy_my_tool`, and `legacy-my-tool`. This conversion is one-way: a declared
hyphenated name does not generate an underscore variant.

Aliases are resolved only against the tools offered in the current model step
and are converted back to the canonical name before permission checks, loop
guards, deduplication, and execution. Aliases are not added to the provider
schema. Empty, repeated, or conflicting canonical/alias/generated names fail
closed. Deferred MCP tools whose canonical names conflict with this complete
call-name namespace are rejected before activation; other conflicts raise
`ValueError` when the offered tool index is built.

Built-in tools accept these compatibility names from equivalent OpenClaw and
Hermes capabilities:

| Canonical Box-Agent name | Compatibility names |
| --- | --- |
| `read_file` | `read` (OpenClaw) |
| `write_file` | `write` (OpenClaw) |
| `edit_file` | `edit` (OpenClaw) |
| `bash` | `exec` (OpenClaw), `terminal` (Hermes) |
| `generate_image` | `image_generate` (OpenClaw and Hermes) |
| `sub_agent` | `sessions_spawn` (OpenClaw), `delegate_task` (Hermes) |
| `request_user_input` | `clarify` (Hermes) |
| `get_skill` | `skill_view` (Hermes) |

These are name-only compatibility mappings. Calls still use the canonical
Box-Agent parameter schema advertised to the model; aliases do not translate
another agent's argument format. Equivalent tools already named `read_file`,
`write_file`, `search_files`, `execute_code`, or `memory_search` need no
additional alias.

#### Example

```python
# box_agent/tools/my_tool.py
from box_agent.tools.base import Tool, ToolResult
from typing import Dict, Any

class MyTool(Tool):
    @property
    def name(self) -> str:
        """A unique name for the tool."""
        return "my_tool"

    @property
    def description(self) -> str:
        """A description for the LLM to understand the tool's purpose."""
        return "My custom tool for doing something useful"

    @property
    def parameters(self) -> Dict[str, Any]:
        """Parameter schema in JSON Schema format."""
        return {
            "type": "object",
            "properties": {
                "param1": {
                    "type": "string",
                    "description": "First parameter"
                },
                "param2": {
                    "type": "integer",
                    "description": "Second parameter",
                    "default": 10
                }
            },
            "required": ["param1"]
        }

    async def execute(self, param1: str, param2: int = 10) -> ToolResult:
        """
        The main logic of the tool.

        Args:
            param1: The first parameter.
            param2: The second parameter, with a default value.

        Returns:
            A ToolResult object.
        """
        try:
            # Implement your logic here
            result = f"Processed {param1} with param2={param2}"

            return ToolResult(
                success=True,
                content=result
            )
        except Exception as e:
            return ToolResult(
                success=False,
                content=f"Error: {str(e)}"
            )

# At the composition boundary
from box_agent.plugins import PluginHost
from box_agent.services import KernelAgentService
from box_agent.tools.my_tool import MyTool

host = PluginHost()
host.registries["tools.executors"].register(
    "my_tool", MyTool(), source="acme.my-tool", version="1.0.0"
)
# Register the required LLM/Context/Memory/Permission/store capabilities too,
# or activate plugins that provide them.
service = KernelAgentService.from_plugin_host(host)
```

The Kernel tool boundary is fixed: schema validation → permission preflight →
`Hook.before_tool` → revalidation after hook mutation → effect fence → executor
→ `Hook.after_tool` → normalized result. Preserve this path; calling an
executor directly bypasses safety, replay, and observability guarantees.

Durable goals use bounded autopilot in CLI `--task` mode and ACP sessions. If a turn ends while the goal is still `active`, Box-Agent injects an internal continuation until the model calls `goal_write complete`, calls `goal_write block`, the user cancels, the `goal_autopilot_max_turns` / `goal_autopilot_max_seconds` config budget is reached, or `goal_autopilot_no_progress_turns` consecutive automatic continuations make no recorded goal progress.

### 3.2 Adding MCP Tools

Edit `mcp.json` to add a new MCP Server:

```json
{
  "mcpServers": {
    "my_custom_mcp": {
      "description": "My custom MCP server",
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@my-org/my-mcp-server"],
      "env": {
        "API_KEY": "your-api-key"
      },
      "disabled": false,
      "notes": {
        "description": "This is a custom MCP server.",
        "api_key_url": "https://example.com/api-keys"
      }
    }
  }
}
```

### 3.3 Built-in Skills

Built-in skills are committed under `box_agent/skills/` and loaded through `box_agent/skills/_manifest.json`.
No git submodule setup is required for normal development.

The generated manifest is the authoritative list of built-in Skills. The
generator explicitly maintains 12 core built-ins; other packaged marketplace
Skills, including the five Midu Skills, remain on disk without loading by
default. The main capability groups include:

- 📄 **Document Processing**: Create and edit PDF, DOCX, XLSX, PPTX
- 🎨 **Design Creation**: Generate artwork, posters, GIF animations
- 🧪 **Development & Testing**: Web automation testing (Playwright), MCP server development
- 🏢 **Enterprise Applications**: Internal communication, brand guidelines, theme customization

Before release, regenerate and commit the manifest if built-in skills change:

```bash
uv run python scripts/generate_skills_manifest.py
```

When a host negotiates `skillhub_search` / `skillhub_install`, plugin
composition adds `search_skillhub` and `install_skillhub_skill` for that
Session. Search is bounded and read-only per Run. Installation binds a prior
candidate, requires one-shot user confirmation, delegates to the host, and
refreshes the catalog. SkillHub does not belong in `kernel/loop.py`.

#### Recommended Skills for officev3

Some submitted skills should ship inside the Box-Agent runtime so officev3 can
show them as installable recommendation cards, but they should not load as
always-on builtin skills. Add those skills through both repositories:

1. Put the skill directory under `box_agent/skills/<skill-slug>/`. Keep
   `SKILL.md` frontmatter complete, including `name`, `description`, and
   `author` when the card should show attribution.
2. Add the top-level directory name to `EXCLUDED_SKILL_DIRS` in
   `scripts/generate_skills_manifest.py`. This leaves the skill on disk for
   packaging, while keeping it out of the builtin `_manifest.json` whitelist.
3. Regenerate the manifest:

   ```bash
   uv run python scripts/generate_skills_manifest.py
   ```

   Verify the script logs `info: excluding '<skill-slug>/SKILL.md'` and that
   `box_agent/skills/_manifest.json` does not list the skill.
4. In the officev3 repository, add the recommendation card to
   `electron/main/skillManager.ts` in `DEFAULT_RECOMMENDED`. Use `sourcePath`
   matching the skill directory under `box_agent/skills/`, set
   `installable: true`, and use category `featured` for community
   recommendations.
5. Rebuild or sync the Box-Agent runtime used by officev3. The recommendation
   card can only install successfully when the runtime contains the physical
   skill directory; the manifest exclusion only controls builtin loading.

**More information:**

- [Claude Skills Official Documentation](https://docs.claude.com/zh-CN/docs/agents-and-tools/agent-skills)
- [Anthropic Blog: Equipping agents for the real world](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)

### 3.4 Adding a New Skill

Create a custom Skill:

```bash
# Create a user skill directory
mkdir -p ~/.box-agent/skills/my-custom-skill
cd ~/.box-agent/skills/my-custom-skill

# Create the SKILL.md file
cat > SKILL.md << 'EOF'
---
name: my-custom-skill
description: My custom skill for handling specific tasks.
allowed-tools:
  - read_file
---

# Overview

This skill provides the following capabilities:
- Capability 1
- Capability 2

# Usage

1. Step one...
2. Step two...

# Best Practices

- Practice 1
- Practice 2

# FAQ

Q: Question 1
A: Answer 1
```

The new Skill will be automatically loaded and recognized by the Agent.

`allowed-tools` (or `allowed_tools`) is normalized, deduplicated, and sorted at
load time. It remains routing metadata in the Skill catalog; selecting a Skill
for a sub-agent does not add tools or widen the derived child policy. Callers
must name needed tools explicitly. Use the smallest list the Skill actually
needs. Skill dependencies belong in `required_skills`; `related_skills` are
suggestions and are not loaded automatically. See
[Sub-agent Delegation](SUB_AGENT_DELEGATION.md).

### 3.5 Customizing System Prompt

The system prompt (`system_prompt.md`) defines the Agent's behavior, capabilities, and working guidelines. You can customize it to tailor the Agent for specific use cases.

#### What You Can Customize

1. **Core Capabilities**: Add or modify tool descriptions
2. **Working Guidelines**: Define custom workflows and best practices
3. **Domain-Specific Knowledge**: Add expertise in specific areas
4. **Communication Style**: Adjust how the Agent interacts with users
5. **Task Priorities**: Set preferences for how tasks should be approached

After modifying `system_prompt.md`, be sure to restart the Agent to apply changes

## 4. Troubleshooting

### 4.1 Common Issues

#### API Key Configuration Error

```bash
# Error message
Error: Invalid API key

# Solution
1. Check that the API key in `config.yaml` is correct.
2. Ensure there are no extra spaces or quotes.
3. Verify that the API key has not expired.
```

#### Dependency Installation Failure

```bash
# Error message
uv sync failed

# Solution
1. Update uv to the latest version: `uv self update`
2. Clear the cache: `uv cache clean`
3. Try syncing again: `uv sync`
```

#### MCP Tool Loading Failure

```bash
# Error message
Failed to load MCP server

# Solution
1. Check the configuration in `mcp.json` is correct.
2. Ensure Node.js is installed (required for most MCP tools).
3. Verify that any required API keys are configured.
4. View detailed logs: `pytest tests/test_mcp.py -v -s`
```

### 4.2 Debugging Tips

#### Enable Verbose Logging

```bash
BOX_AGENT_LOG_LEVEL=DEBUG box-agent --task "reproduce the issue"
BOX_AGENT_LLM_DEBUG=1 box-agent --task "inspect provider traffic"
```

Provider debug logs redact credentials and summarize payloads by default. Do
not enable full-payload logging with production prompts or customer data.

#### Using the Python Debugger

```python
# Set a breakpoint in your code
import pdb; pdb.set_trace()

# Or use ipdb for a better experience
import ipdb; ipdb.set_trace()
```

#### Inspecting Tool Calls

```bash
box-agent trace-viewer
# Development directory service, if live refresh is needed:
uv run python -m box_agent.trace_viewer.server --port 8766
```

The read-only viewer consumes redacted JSONL traces from
`~/.box-agent/log/sessions/`. Add observability through a registered Hook when
programmatic inspection is required; do not add ad hoc prints to Kernel, CLI,
or ACP paths.
