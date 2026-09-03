<p align="center">
  <h1 align="center">Box Agent</h1>
  <p align="center">A general-purpose AI agent with sandboxed code execution, sub-agent parallelism, and multi-provider LLM support.</p>
</p>

<p align="center">
  <a href="https://pypi.org/project/box-agent/"><img src="https://img.shields.io/pypi/v/box-agent?color=orange" alt="PyPI"></a>
  <a href="https://pypi.org/project/box-agent/"><img src="https://img.shields.io/pypi/dm/box-agent?color=brightgreen" alt="Downloads"></a>
  <a href="https://pypi.org/project/box-agent/"><img src="https://img.shields.io/pypi/pyversions/box-agent?color=blue" alt="Python"></a>
  <a href="https://github.com/Raccoon-Office/Box-Agent/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Raccoon-Office/Box-Agent?color=green" alt="License"></a>
  <a href="https://github.com/Raccoon-Office/Box-Agent/releases"><img src="https://img.shields.io/github/v/release/Raccoon-Office/Box-Agent?color=blue" alt="Release"></a>
</p>

<p align="center">
  English | <a href="./README_CN.md">中文</a>
</p>

---

**Get started in 30 seconds:**

```bash
uv tool install box-agent   # or: pip install box-agent (Python 3.10+)
box-agent setup              # interactive config wizard
box-agent                    # start chatting
```

Or run a one-shot task:

```bash
box-agent --task "Analyze sales.csv — show top 10 products by revenue with a bar chart"
```

---

## Why Box Agent?

Most agent frameworks are either too simple (no sandbox, no tools) or too complex (massive dependencies, rigid architecture). Box Agent hits the sweet spot:

| Feature                      | Box Agent                                         | Open Interpreter      | Aider              |
| ---------------------------- | ------------------------------------------------- | --------------------- | ------------------ |
| Sandboxed code execution     | Jupyter kernel in isolated venv                   | Runs in host Python   | N/A                |
| Sub-agent parallelism        | Multiple sub-agents run concurrently              | No                    | No                 |
| Multi-provider LLM           | Anthropic, OpenAI, DeepSeek, SiliconFlow, any API | OpenAI + a few others | OpenAI + Anthropic |
| MCP tool integration         | Native                                            | No                    | No                 |
| ACP protocol (embed in apps) | Full support                                      | No                    | No                 |
| Standalone binary            | PyInstaller runtime, no Python needed             | No                    | No                 |
| Context compression          | Staged automatic compaction + LLM summary          | Manual                | Git-based          |

## Key Features

### Sub-Agent Parallelism

Delegate isolated work through a flat task contract with optional tools, Skills,
files, write scope, and hard step/tool-call budgets. Omitted tools resolve only
to trusted local readers; explicit capabilities still pass a fail-closed runtime
policy. Passing known local text paths in `files` selects the bounded,
completeness-checked batch fast path automatically when `read_file` is the only
resolved tool; requesting additional tools keeps the general child loop. The parent remains
responsible for conflict handling, the final deliverable, and verification.

```
You: "Analyze data1.csv, data2.csv, and data3.csv separately, then give me a combined summary"

┌─ Sub-Agent 1 ──────┐  ┌─ Sub-Agent 2 ──────┐  ┌─ Sub-Agent 3 ──────┐
│ Read data1.csv      │  │ Read data2.csv      │  │ Read data3.csv      │
│ Run statistics      │  │ Run statistics      │  │ Run statistics      │
│ Generate charts     │  │ Generate charts     │  │ Generate charts     │
│ → Summary: ...      │  │ → Summary: ...      │  │ → Summary: ...      │
└─────────────────────┘  └─────────────────────┘  └─────────────────────┘
                              ↓ parallel ↓
                    ┌─ Parent Agent ──────────┐
                    │ Combines 3 summaries    │
                    │ Produces final report   │
                    └─────────────────────────┘
```

Child policy is derived by the runtime: process tools, external side effects,
and unknown MCP tools fail closed; path writes require an exact scope. See the
[sub-agent delegation contract](docs/SUB_AGENT_DELEGATION.md) for schemas,
limits, compatibility behavior, and host diagnostics.

### Sandboxed Code Execution

Python runs in an isolated Jupyter kernel with pre-installed data science packages (`pandas`, `numpy`, `matplotlib`, `scikit-learn`, `openpyxl`, `xlrd`). Generated files (charts, CSVs, PDFs) are automatically detected and surfaced as structured artifacts.

### Multi-Provider LLM

One config, any provider:

```yaml
# Anthropic
api_base: "https://api.anthropic.com"
provider: "anthropic"
model: "claude-sonnet-4-20250514"

# DeepSeek
api_base: "https://api.deepseek.com"
provider: "openai"
model: "deepseek-chat"

# Any OpenAI-compatible endpoint
api_base: "https://your-api.example.com/v1"
provider: "openai"
model: "your-model"
```

### Staged Context Compression

- **Oversized tool results**: Individual results are persisted immediately when needed; fresh parallel results also share a 50k-character pre-request budget. The model receives a stable preview while full text remains on disk. Read results are exempt and stay bounded by Read's own line/character controls.
- **Usage-aware auto-summary**: The next request is estimated from the latest real API usage plus subsequent messages. When it reaches the model-derived safety threshold, older history is summarized into a `user` message while bounded recent messages and todo, plan, and skill state are restored.
- **Tool-call arguments**: Write/edit arguments remain verbatim until a whole-history summary replaces their turn; they are not independently compacted.
- **Legacy safety guard**: Internal history placeholders from older or externally supplied sessions are rejected if a model tries to reuse them as executable file/code arguments; Box-Agent requests one clean regeneration instead of writing the placeholder to disk.

### More

- **MCP Tools**: Connect to any [MCP server](https://github.com/modelcontextprotocol/servers) — web search, knowledge graphs, databases
- **Skills and SkillHub**: `_manifest.json` owns the built-in catalog; hosts that negotiate `skillhub_search` / `skillhub_install` can expose bounded per-run search and user-confirmed marketplace installation
- **Reliable presentations**: The controlled PPTX workflow checkpoints pending chunks for restart-safe writes, while Shell and Jupyter boundaries block self-check and image-status bypasses
- **Model profiles**: Hosts may select immutable `profileId + profileRevision` provider bindings; credentials are resolved at call time and never copied into Session metadata or events
- **ACP Protocol**: Embed Box Agent in Electron apps, Zed Editor, or any ACP-compatible host via JSON-RPC over stdio
- **Standalone Runtime**: PyInstaller binary bundles Python + all dependencies. No external Python needed — download and run
- **Cross-session Memory**: Persistent memory lets the agent retain key information across conversations
- **Safety Layer**: Dangerous command detection, workspace scope control, auto-backup before file modifications. Interactive permission negotiation for out-of-workspace access (CLI prompts user, ACP sends reverse RPC to host)
- **Planning Snapshots**: Structured plan tool for rendering objective, scope, steps, verification, and risks in host UIs
- **Task Tracking**: Built-in todo tool for multi-step task decomposition and progress tracking

## Runtime Architecture

Box Agent has one execution owner. CLI, ACP, SDK, sub-agents, and the
historical Python API all submit stable `box_agent.api` contracts through
`KernelAgentService`; only `AgentLoopKernel` advances a run. Host adapters
translate protocols and render events, while capabilities are selected from
typed `PluginHost` registries for each run.

![Box Agent single-kernel architecture](docs/assets/box-agent-architecture.png)

```text
CLI / ACP / SDK / historical API
            ↓
thin adapters or compatibility facades
            ↓
KernelAgentService → PluginKernelComposer → AgentLoopKernel
            ↓
Context · Tools · Permissions · Memory · Persistence · LLM · Workflows · Hooks
```

| Layer | Responsibility |
| --- | --- |
| `box_agent/api/` | Serializable requests, events, results, controls, handles, and capability ports |
| `box_agent/kernel/` | The single loop, model streaming/recovery, and per-run workflow composition |
| `box_agent/services/` | Session/run lifecycle, replay, idempotency, controls, checkpoints, and leases |
| `box_agent/plugins/` | Typed registries, manifests, dependency activation, disposal, and plugin locks |
| `box_agent/adapters/` / `box_agent/acp/` | CLI, ACP, and SDK translation or rendering without a second loop |
| Capability packages | `context/`, `memory_engine/`, `permissions/`, `persistence/`, `tools/`, `workflows/`, and `llm/` |
| `box_agent/compat/` and root facades | Preserve historical imports and call shapes while forwarding into the same Kernel |

The pre-Kernel runtime selector is retired. Goal, Plan, PPT, Skill, Completion
Gate, and Autopilot now run as workflow plugins with parity fixtures. See the
[architecture guide](docs/ARCHITECTURE.md),
[runtime capability matrix](docs/runtime-capability-matrix.md), and
[documentation index](docs/README.md) for the complete ownership map.
For a file-by-file source tour, see the [Chinese codebase map](docs/CODEBASE_MAP_CN.md).

## Demos

### Task Execution

_The agent creates a webpage and opens it in the browser._

![Demo: Task Execution](docs/assets/demo1-task-execution.gif)

### Claude Skill — PDF Generation

_The agent uses a skill to create a professional document._

![Demo: Claude Skill](docs/assets/demo2-claude-skill.gif)

### Web Search via MCP

_The agent searches the web and summarizes results._

![Demo: Web Search](docs/assets/demo3-web-search.gif)

## Installation

Choose the installation path for the entry point you intend to use:

| Goal | Recommended installation |
| ---- | ------------------------ |
| Use the interactive CLI, one-shot CLI, or ACP server | `uv tool install box-agent` |
| Embed Box Agent in a Python application | `uv add box-agent` in that application |
| Develop Box Agent or build a standalone runtime | Clone this repository, then run `uv sync --group dev` |
| Embed a published standalone ACP runtime | Download a release archive; no system Python is required |

The Python package requires Python 3.10+. A standalone runtime already bundles
Python and its dependencies.

### Install the command-line tools (recommended)

[uv](https://docs.astral.sh/uv/) handles Python version management for you — no need to upgrade your system Python:

```bash
# Install uv (if not already)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Box Agent (downloads Python 3.10+ if needed)
uv tool install box-agent

# Create the shared LLM/MCP configuration and verify it
box-agent setup
box-agent doctor

# Start the interactive CLI
box-agent

# Upgrade later
uv tool upgrade box-agent
```

If you already manage a Python 3.10+ virtual environment, `pip install
box-agent` exposes the same console commands inside that environment.

### Install the Python SDK

Add Box Agent to the Python application that will import it:

```bash
uv add box-agent
```

Or, inside an activated Python 3.10+ virtual environment:

```bash
pip install box-agent
```

`uv tool install` uses an isolated tool environment, so it is ideal for the
commands but does not add `box_agent` to another application's import path.

### Run from source

```bash
git clone https://github.com/Raccoon-Office/Box-Agent.git
cd Box-Agent
uv sync
uv run box-agent setup
uv run box-agent
```

In a source checkout, prefix console commands with `uv run`. Install the
development group before testing or packaging: `uv sync --group dev`.

### Entry-point reference

| Entry point | Installed command | Source-checkout command | Intended caller |
| ----------- | ----------------- | ----------------------- | --------------- |
| Interactive and one-shot CLI | `box-agent` | `uv run box-agent` | A person, shell script, or CI job |
| ACP server | `box-agent-acp` | `uv run box-agent-acp` | An editor or application speaking ACP over stdio |
| Python SDK | `box_agent` imports | `uv run python your_app.py` | Python application code |
| Web Extract MCP server | `box-agent-web-extract-mcp` | `uv run box-agent-web-extract-mcp` | An MCP client speaking MCP over stdio |
| Standalone runtime builder | — | `uv run box-agent-build-runtime` | Maintainers packaging the ACP server from a source checkout |

`box-agent-acp` and `box-agent-web-extract-mcp` are protocol servers: normally
configure a host to spawn them instead of typing into them in a terminal. The
sections below show the startup contract for every entry point.

## Contributor Quickstart

If you are joining the project as a collaborator, start here before changing
code:

```bash
git clone https://github.com/Raccoon-Office/Box-Agent.git
cd Box-Agent
uv sync
uv run python -m box_agent.cli --help
uv run pytest tests/test_agent_loop_kernel.py tests/test_kernel_service.py -q
```

Read these files first:

- `AGENTS.md` — repo-local engineering rules and verification expectations.
- `CONTRIBUTING.md` — contribution flow, PR checklist, and commit style.
- `docs/REVIEW_GUIDE.md` — maintainer review order, blockers, and proof requirements.
- `docs/DEVELOPMENT_GUIDE.md` — deeper architecture and development notes.
- `docs/INTEGRATION.md` — ACP/runtime integration details for host apps.

Project map:

| Area | Where to start |
| ---- | -------------- |
| Stable host contracts | `box_agent/api/` |
| Agent execution loop | `box_agent/kernel/` |
| Session and Run lifecycle | `box_agent/services/kernel.py` |
| Plugin composition | `box_agent/plugins/`, `box_agent/adapters/plugin_host.py` |
| CLI and config | `box_agent/adapters/cli/app.py`, `box_agent/config.py`, `box_agent/config/` |
| LLM providers | `box_agent/llm/` |
| Capability implementations | `box_agent/context/`, `box_agent/memory_engine/`, `box_agent/permissions/`, `box_agent/persistence/`, `box_agent/tools/`, `box_agent/workflows/` |
| ACP and SDK adapters | `box_agent/acp/`, `box_agent/adapters/` |
| Historical compatibility | `box_agent/compat/` and root module facades |
| Runtime packaging | `box_agent/build_runtime_cli.py`, `scripts/build_runtime.py` |
| Skills | `box_agent/skills/`, `box_agent/tools/skill_loader.py` |
| Tests | `tests/test_<area>.py` |

The [codebase map](docs/CODEBASE_MAP_CN.md) lists the core directories, source
files, and request flow in one place.

Common development loop:

```bash
# Run the smallest relevant test while iterating
uv run pytest tests/test_bash_tool.py -q

# Run the broader suite before handing off
uv run pytest tests/ -q

# Catch whitespace/patch formatting issues
git diff --check
```

Use focused tests for the area you touched: tools in `tests/test_*_tool.py`,
LLM behavior in `tests/test_llm*.py` / `tests/test_error_messages.py`, ACP in
`tests/test_acp*.py`, memory in `tests/test_memory*.py`, and runtime packaging
in `tests/test_build_runtime.py` / `tests/test_cli_runtime.py`. Tests that need
real provider credentials are skipped unless the required API keys are present.

When a change affects the standalone runtime used by a host app, source changes
are not enough: rebuild the runtime, install it into the host, restart the
running ACP process, then probe the installed runtime. For local packaging:

```bash
uv run box-agent-build-runtime
```

Build a versioned runtime and install the resulting archive into the usual
officev3 checkout in one command:

```bash
uv run box-agent-build-runtime --version 0.9.6 --install-officev3
```

Pass an explicit checkout path after `--install-officev3`, or set
`BOX_AGENT_OFFICEV3_DIR`, when officev3 is stored elsewhere.

## Configuration

After running `box-agent setup`, your config lives at `~/.box-agent/config/config.yaml`:

```yaml
api_key: "your-api-key"
api_base: "https://api.anthropic.com"
model: "claude-sonnet-4-20250514"
provider: "anthropic" # "anthropic" or "openai"
max_steps: 300
max_parallel_tools: 8
parallel_tool_timeout_seconds: 900
provider_stale_seconds: 300
sub_agent_token_limit: 50000
sub_agent_batch_synthesis_timeout_seconds: 600 # 0 disables the extra batch synthesis cap
goal_autopilot_enabled: true
goal_autopilot_max_turns: 3
goal_autopilot_max_seconds: 14400
goal_autopilot_no_progress_turns: 2
tools:
  # bash_default_timeout_seconds: 300
  # bash_max_timeout_seconds: 1200
  mcp:
    connect_timeout: 60
    execute_timeout: 120
    sse_read_timeout: 180
```

Tool limits are omitted by default so runtime upgrades can supply updated
defaults from `box_agent/config.py`. Add only deliberate overrides under
`tool_limits:`; inspect the current effective values with
`box-agent config --json`.

```bash
box-agent config                    # show current config summary
box-agent config --get model        # print one config value
box-agent config --set max_steps 300
box-agent config --set goal_autopilot_max_turns 5
box-agent config --set tool_limits.external_skill.max_tool_calls 160
box-agent config --set tool_limits.external_skill.max_delegated_tool_calls 512
box-agent config --set tools.bash_default_timeout_seconds 450
box-agent config --json             # machine-readable config summary
box-agent config --edit             # open in editor
box-agent doctor                    # check environment & API connectivity
box-agent doctor --json             # machine-readable health check
```

## CLI Entry Points

All CLI modes use the configuration created by `box-agent setup`. Pass
`--workspace` to choose the directory the Agent may work in; otherwise the
current directory is used.

### 1. Interactive CLI

```bash
box-agent
box-agent --workspace /path/to/project
box-agent --no-sandbox           # disable Jupyter sandbox
```

In-session commands: `/help`, `/clear`, `/clear_all`, `/history`, `/stats`,
`/sandbox_status`, `/log`, `/goal`, `/memory review`, `/exit`.

### 2. One-shot CLI for scripts and CI

`--task` runs one request and exits. Add `--json` when a caller needs the
machine-readable execution summary in addition to normal output.

```bash
box-agent --task "analyze data.csv and create a report"
box-agent --task "analyze data.csv" --json          # append execution summary JSON
box-agent --task "local file task" --no-verify-api  # skip startup API probe
box-agent --task "create a PPT" --force-plan-start  # publish a plan before work
box-agent --task "create a PPT" --no-completion-gate
box-agent --goal "Ship CLI parity" --task "finish tests"
box-agent --goal "Ship CLI parity" --task "finish tests" --no-goal-autopilot
box-agent --deep-think --task "review this repo"    # enable thinking mode when supported
```

Use `--goal "<objective>"` to keep a durable workspace objective attached to
later turns. Box Agent stores goals under `~/.box-agent/goals/`. In one-shot
CLI and ACP sessions, an active goal can continue automatically within the
configured turn, time, and no-progress limits; pass `--no-goal-autopilot` for
one run to disable that behavior.

Manage the goal interactively with `/goal pause`, `/goal resume`, `/goal block
<reason>`, `/goal complete <evidence>`, or `/goal clear`; scripts can use
`box-agent goal ...`.

### 3. Setup, health, and maintenance commands

```bash
box-agent setup              # config wizard
box-agent config             # show/edit config
box-agent doctor             # health check
box-agent log                # open log directory
box-agent trace-viewer       # open the offline Agent Trace diagnostics page
box-agent goal status        # show persistent workspace goal
box-agent goal complete --evidence "tests passed"
box-agent install-browser   # install Chromium for Playwright MCP (~200MB)
box-agent install-node      # install managed Node.js runtime for skills (macOS)
```

### Agent Trace diagnostics

Run `box-agent trace-viewer` to open the packaged, offline developer viewer. Open `~/.box-agent/log/sessions/` for a newest-first overview of every trace, then select one run to inspect per-turn metrics, LLM/tool waterfalls, raw events, and the complete system → user → assistant/tool → final-response chain. You can still open one `.jsonl` file directly.

If an embedded browser does not expose the native file picker, run the loopback-only service and enter the trace directory path in the page:

```bash
uv run python -m box_agent.trace_viewer.server --port 8766
```

The offline page reads files in the browser. Service mode reads only top-level `.jsonl` files from the directory you enter, checks their metadata once per second, and refreshes the ledger when files are added or changed; trace bodies are transferred over `127.0.0.1` only when that metadata changes. The service rejects requests whose `Host` or `Origin` is not its exact loopback authority, preventing a rebinding site from reading local traces. Neither mode makes external network requests. Chromium and Edge can keep following appended records after you grant a file handle; drag/drop and ordinary file inputs load a snapshot. Session traces may contain prompts, tool arguments, outputs, and business data—handle them as sensitive diagnostic artifacts.

### Browser automation (optional)

Box-Agent ships with a disabled [`@playwright/mcp`](https://github.com/microsoft/playwright-mcp) entry. To enable browser tools locally:

```bash
box-agent install-browser   # downloads Chromium and flips the entry to enabled
```

Requires Node.js ≥ 18 on `PATH`. Chromium lands in `~/.box-agent/browsers/` (shared by CLI and ACP runtime) and `mcpServers.playwright.disabled` in `~/.box-agent/config/mcp.json` is set to `false`.

**ACP embedders**: no env-var plumbing required — `box-agent-acp` defaults `PLAYWRIGHT_BROWSERS_PATH` to the same `~/.box-agent/browsers/` path. To point at a different cache, export `PLAYWRIGHT_BROWSERS_PATH=<your path>` before spawning `box-agent-acp` (our setdefault won't override it).

### Session trace retention

ACP session traces keep their existing `~/.box-agent/log/sessions/<session-id>.jsonl`
name and `box-agent-session-trace/v1` record format. Retention removes only whole,
inactive session files: files older than 7 days are eligible, and the directory
has a soft 512 MiB cap. The current append target, files modified within 24
hours, and the newest two sessions are protected. Cleanup runs best-effort at
most once every 6 hours; cleanup failures never interrupt agent execution.
Operators can override the defaults with `BOX_AGENT_SESSION_TRACE_RETENTION_DAYS`,
`BOX_AGENT_SESSION_TRACE_MAX_TOTAL_BYTES`, and
`BOX_AGENT_SESSION_TRACE_CLEANUP_INTERVAL_SECONDS`, or disable cleanup with
`BOX_AGENT_SESSION_TRACE_RETENTION_ENABLED=0`.

## ACP, SDK, and Packaged Runtime Entry Points

### 4. ACP server for editors and applications

Box Agent supports the [Agent Communication Protocol](https://github.com/nichochar/agent-client-protocol) for embedding in editors and apps.

The host must spawn `box-agent-acp` and communicate with it using ACP JSON-RPC
over stdio. stdin/stdout are protocol-only; diagnostic logs go to stderr. Use
the absolute executable path returned by `which box-agent-acp` on macOS/Linux
or `where box-agent-acp` on Windows when the host does not inherit your shell
`PATH`.

**Zed Editor** — add to `settings.json`:

```json
{
  "agent_servers": {
    "box-agent": {
      "command": "/path/to/box-agent-acp"
    }
  }
}
```

From a source checkout, configure the host command as `uv` with arguments
`["run", "box-agent-acp"]`, or point it at the environment's generated
`box-agent-acp` executable. Do not send human-readable input directly to this
process.

### 5. Python SDK embedding

Install the dependency with `uv add box-agent` or `pip install box-agent`.
The SDK uses the same service and event contracts as ACP and
CLI; it does not construct another Agent loop:

```python
import asyncio
import os

from box_agent import LLMClient, build_kernel_service
from box_agent.adapters import SDKServiceAdapter


async def main() -> None:
    llm = LLMClient(api_key=os.environ["ANTHROPIC_API_KEY"])
    service = build_kernel_service(llm=llm, tools=[])
    result = await SDKServiceAdapter(service).run(
        {"session_id": "session-1", "message": "Explain this architecture"}
    )
    print(result)


asyncio.run(main())
```

For advanced integrations, register typed capability ports in `PluginHost` and
create the service with `KernelAgentService.from_plugin_host(host)`. Direct
Kernel construction is intended for tests or callers that have already bound
every run-scoped dependency. See the
[runtime capability matrix](docs/runtime-capability-matrix.md) for registry,
workflow, replay, and control examples.

### 6. Standalone ACP runtime

Use this entry point for Electron applications and other hosts that must not
depend on a system Python installation:

```bash
# Download pre-built binary (latest release; omit the tag to always get the newest)
gh release download --repo Raccoon-Office/Box-Agent --pattern "box-agent-runtime-*.tar.gz"

# Or build from source (current platform)
uv run box-agent-build-runtime

# Build macOS Intel/x64 runtime from Apple Silicon
# Requires a separate x86_64 venv because PyInstaller cannot bundle arm64 wheels into an x64 binary.
# One-time setup:
#   arch -x86_64 /bin/bash -c 'curl -LsSf https://astral.sh/uv/install.sh | INSTALLER_NO_MODIFY_PATH=1 UV_INSTALL_DIR="$HOME/.local/bin-x64" sh'
#   UV_PROJECT_ENVIRONMENT=.venv-x64 arch -x86_64 ~/.local/bin-x64/uv sync
# Build:
UV_PROJECT_ENVIRONMENT=.venv-x64 BOX_AGENT_RUNTIME_TARGET=darwin-x64 arch -x86_64 ~/.local/bin-x64/uv run box-agent-build-runtime
```

The builder reads repository-owned scripts, so run it from a source checkout
after `uv sync --group dev`. The archive contains
`box-agent-runtime/bin/box-agent-acp` (`.exe` on Windows); configure the host to
spawn that binary exactly as it would spawn the installed ACP command. The
runtime communicates via JSON-RPC over stdio: stdout is protocol-only and
stderr is for diagnostics.

macOS runtime archives include Box-Agent's pinned Node.js runtime for skills
under `box-agent-runtime/runtimes/node/`; npm cache/prefix state remains in
`~/.box-agent/runtimes/node/sandbox/`.

### 7. Web Extract MCP server

This entry point exposes the `web_extract` tool to MCP clients. A source or
Python-package installation provides `box-agent-web-extract-mcp`; add it to the
client's stdio MCP configuration and let the client spawn it. For example,
Box Agent's `~/.box-agent/config/mcp.json` format is:

```json
{
  "mcpServers": {
    "box-agent-web-extract": {
      "command": "/absolute/path/to/box-agent-web-extract-mcp",
      "args": [],
      "alwaysLoad": true,
      "disabled": false
    }
  }
}
```

The server fetches public HTTP(S) pages without executing JavaScript. It reads
the Box Agent LLM configuration when a long page needs summarization. In a
standalone runtime, the same server is already declared in `manifest.json` and
is launched as `bin/box-agent-acp --web-extract-mcp`; hosts should consume that
manifest instead of inventing a second packaged command. Other MCP clients may
use different field names around the same command and stdio transport.

## Testing

```bash
uv run pytest tests/ -v          # all tests
uv run pytest tests/test_agent_loop_kernel.py tests/test_context_engine.py tests/test_context_compaction_e2e.py -v
uv run pytest --cov              # with coverage
```

For the credential-free ACP end-to-end smoke and visual event report:

```bash
uv run python tests/e2e/run_acp_cases.py --report tests/e2e/report.json
uv run pytest tests/e2e -q
uv run python -m http.server 8765 --directory tests/e2e
```

Open `http://localhost:8765/report.html`; the report covers text, permissions,
Context/Memory plugins, workflow continuation, and durable resume. See the
[ACP E2E guide](docs/e2e/ACP_E2E_GUIDE.md).

## Troubleshooting

**SSL Certificate Error**: `pip install --upgrade certifi` or set `verify=False` for testing.

**Module Not Found**: Make sure you're in the project directory: `cd Box-Agent && uv run python -m box_agent.cli`

## Contributing

Issues and PRs welcome! See [Contributing Guide](CONTRIBUTING.md).

## License

[MIT](LICENSE)

## Links

- [Documentation](docs/README.md)
- [PyPI](https://pypi.org/project/box-agent/) · [GitHub](https://github.com/Raccoon-Office/Box-Agent) · [Releases](https://github.com/Raccoon-Office/Box-Agent/releases)
- [Anthropic API](https://docs.anthropic.com/claude/reference) · [MCP Servers](https://github.com/modelcontextprotocol/servers) · [ACP Protocol](https://github.com/nichochar/agent-client-protocol)

---

**If this project helps you, give it a ⭐!**
