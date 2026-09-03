# Agent Trace Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline static developer page that opens Box Agent v1 session-trace JSONL files and renders performance waterfalls, raw events, and a top-to-bottom system/user/assistant/tool conversation chain.

**Architecture:** Add dependency-free classic-script assets under `box_agent/trace_viewer/`. A pure JavaScript model layer parses and correlates records; a DOM application layer owns file access, filtering, live follow, and accessible rendering. A thin CLI launcher opens the packaged page before Box Agent configuration is initialized.

**Tech Stack:** HTML5, CSS, dependency-free JavaScript, Node built-in test runner, Python 3.10+, pytest, argparse, setuptools package data.

**Spec:** `docs/superpowers/specs/2026-08-20-agent-trace-viewer-design.md`

## Global Constraints

- Do not add a non-loopback HTTP server, database, remote dependency, CDN resource, analytics request, or JavaScript package manager. Any optional loopback service must reject requests whose `Host` or `Origin` does not match its exact authority.
- Keep trace contents in browser memory; do not persist them to localStorage or IndexedDB.
- Treat every trace value as untrusted text and render it with `textContent`/text nodes, never `innerHTML`.
- Preserve unknown events and fields; invalid lines produce warnings while valid later lines continue loading.
- Keep the Agent Kernel, `box_agent/observability/session_trace.py`, agent events, and ACP protocol unchanged for the MVP.
- Do not commit, stage, push, or publish without explicit user authority.

---

### Task 1: Trace parsing and normalized model

**Files:**
- Create: `box_agent/trace_viewer/trace_model.js`
- Create: `tests/js/trace_model.test.js`
- Create: `tests/fixtures/session_trace_viewer.jsonl`
- Create: `tests/test_trace_viewer.py`

**Interfaces:**
- Consumes: `box-agent-session-trace/v1` JSON objects, one per line.
- Produces: global/CommonJS `BoxTraceModel` with `parseJsonl(text)`, `buildSession(records)`, `buildTurn(records, turnId)`, and `formatDuration(ms)`.

- [x] **Step 1: Write failing parser/correlation tests**

```javascript
const test = require("node:test");
const assert = require("node:assert/strict");
const model = require("../../box_agent/trace_viewer/trace_model.js");

test("keeps valid records around malformed JSONL", () => {
  const result = model.parseJsonl('{"event":"turn.input"}\nnot-json\n{"event":"turn.end"}\n');
  assert.equal(result.records.length, 2);
  assert.deepEqual(result.warnings.map((item) => item.line), [2]);
});

test("pairs calls and nests tools under the requesting response", () => {
  const parsed = model.parseJsonl(fixtureText);
  const turn = model.buildTurn(parsed.records, "turn-1");
  assert.equal(turn.spans.filter((span) => span.kind === "llm").length, 2);
  assert.equal(turn.conversation.find((item) => item.role === "assistant").tools[0].callId, "tool-1");
  assert.equal(turn.summary.finalContent, "done");
});
```

- [x] **Step 2: Add a pytest wrapper and verify RED**

```python
def test_trace_model_node_suite() -> None:
    node = os.environ.get("BOX_AGENT_NODE") or shutil.which("node")
    if node is None:
        pytest.skip("node is required for trace viewer model tests")
    result = subprocess.run(
        [node, "--test", "tests/js/trace_model.test.js"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

Run: `uv run pytest tests/test_trace_viewer.py::test_trace_model_node_suite -v`

Expected: FAIL because `trace_model.js` does not exist.

- [x] **Step 3: Implement line parsing, span pairing, metrics, and conversation reconstruction**

```javascript
function parseJsonl(text) {
  const records = [];
  const warnings = [];
  text.split(/\r?\n/).forEach((line, index) => {
    if (!line.trim()) return;
    try {
      const value = JSON.parse(line);
      if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("record must be an object");
      records.push({ ...value, __line: index + 1, __index: records.length });
    } catch (error) {
      warnings.push({ line: index + 1, message: String(error.message || error) });
    }
  });
  return { records, warnings };
}
```

Pair requests and terminals by `llm_call_id`/`tool_call_id`, retain incomplete spans, prefer explicit durations, group records by `turn_id`, use the first LLM request message list as exact context, and mark rather than duplicate an equal `turn.output`.

- [x] **Step 4: Run model tests and focused existing trace tests**

Run: `uv run pytest tests/test_trace_viewer.py tests/test_session_trace.py -q`

Expected: all tests pass.

- [x] **Step 5: Review checkpoint**

Inspect `git diff -- box_agent/trace_viewer/trace_model.js tests/js/trace_model.test.js tests/test_trace_viewer.py tests/fixtures/session_trace_viewer.jsonl` and do not commit without user authorization.

---

### Task 2: Static diagnostic shell and three views

**Files:**
- Create: `box_agent/trace_viewer/index.html`
- Create: `box_agent/trace_viewer/styles.css`
- Create: `box_agent/trace_viewer/app.js`
- Modify: `tests/test_trace_viewer.py`

**Interfaces:**
- Consumes: `window.BoxTraceModel` from Task 1 and a browser `File` snapshot.
- Produces: `window.BoxTraceViewer` test hooks plus the Summary, Waterfall, Conversation, Events, and Inspector regions.

- [x] **Step 1: Write failing static-contract tests**

```python
def test_viewer_is_offline_and_uses_packaged_assets() -> None:
    html = VIEWER_INDEX.read_text(encoding="utf-8")
    assert 'Content-Security-Policy' in html
    assert 'connect-src \'none\'' in html
    assert 'src="trace_model.js"' in html
    assert 'src="app.js"' in html
    assert "https://" not in html
    assert "http://" not in html

def test_viewer_declares_diagnostic_regions() -> None:
    html = VIEWER_INDEX.read_text(encoding="utf-8")
    for region in ("summary-strip", "turn-list", "waterfall-view", "conversation-view", "events-view", "inspector"):
        assert f'id="{region}"' in html
```

- [x] **Step 2: Run contract tests and verify RED**

Run: `uv run pytest tests/test_trace_viewer.py -k 'offline or diagnostic_regions' -v`

Expected: FAIL because the assets do not exist.

- [x] **Step 3: Implement the accessible shell and diagnostic visual system**

Use semantic buttons, tabs with `aria-selected`, explicit role labels, and keyboard-focus styles. Use CSS custom properties for role/status colors and a desktop three-column layout. Provide an empty drop-zone state and a persistent privacy reminder.

```html
<nav class="view-tabs" aria-label="Trace views">
  <button role="tab" data-view="waterfall" aria-selected="true">Waterfall</button>
  <button role="tab" data-view="conversation" aria-selected="false">Conversation</button>
  <button role="tab" data-view="events" aria-selected="false">Events</button>
</nav>
```

- [x] **Step 4: Render summary, waterfall, conversation chain, events, and inspector**

```javascript
function appendText(parent, tagName, text, className) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  element.textContent = text == null ? "" : String(text);
  parent.appendChild(element);
  return element;
}
```

All payload rendering must flow through text-node helpers. Nest tool cards under the matched assistant node. Collapse long prompt/thinking/result bodies by default. Paginate event rows and expose source-line numbers.

- [x] **Step 5: Run static contracts and model tests**

Run: `uv run pytest tests/test_trace_viewer.py -q`

Expected: all tests pass.

- [x] **Step 6: Review checkpoint**

Inspect the asset diff for accidental external URLs, `innerHTML`, inline event handlers, and inaccessible color-only status indicators. Do not commit without user authorization.

---

### Task 3: File import, filtering, and live follow

**Files:**
- Modify: `box_agent/trace_viewer/app.js`
- Modify: `box_agent/trace_viewer/index.html`
- Modify: `box_agent/trace_viewer/styles.css`
- Modify: `tests/js/trace_model.test.js`
- Modify: `tests/test_trace_viewer.py`

**Interfaces:**
- Consumes: browser `File`, optional File System Access `FileSystemFileHandle`, search/filter controls.
- Produces: `loadSnapshot(file)`, `startFollowing(handle)`, `stopFollowing()`, and deterministic filter predicates.

- [x] **Step 1: Add failing tests for append parsing and safe rendering contracts**

```javascript
test("retains an unfinished append line until completed", () => {
  const first = model.parseAppend("", '{"event":"turn.input"}\n{"event":');
  assert.equal(first.records.length, 1);
  assert.equal(first.tail, '{"event":');
  const second = model.parseAppend(first.tail, '"turn.end"}\n');
  assert.equal(second.records[0].event, "turn.end");
  assert.equal(second.tail, "");
});
```

Add Python assertions that assets contain no `innerHTML`, `insertAdjacentHTML`, `eval`, `fetch(`, `XMLHttpRequest`, `WebSocket`, or external scheme.

- [x] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_trace_viewer.py -v`

Expected: FAIL because append parsing and file actions are absent.

- [x] **Step 3: Implement snapshot import and 50 MiB confirmation**

Use drag/drop and `<input type="file" accept=".jsonl,application/json">`. Read only after size confirmation and leave the previous loaded trace untouched if the user cancels or parsing throws.

- [x] **Step 4: Implement Chromium/Edge live follow**

```javascript
async function pollFileHandle() {
  const nextFile = await state.fileHandle.getFile();
  if (nextFile.size < state.byteOffset) return requestFullReload(nextFile);
  if (nextFile.size === state.byteOffset) return;
  const chunk = await nextFile.slice(state.byteOffset).text();
  state.byteOffset = nextFile.size;
  ingestAppend(chunk);
}
```

Poll once per second while visible, pause on `visibilitychange`, preserve an unfinished line tail, and label normal input/drop imports as snapshots.

- [x] **Step 5: Implement filters and copy/error feedback**

Search event names, IDs, tool/model names, and serialized payload text. Use `navigator.clipboard.writeText` with a non-blocking error message on denial. Keep unknown events available in Events.

- [x] **Step 6: Run focused automated checks**

Run: `uv run pytest tests/test_trace_viewer.py tests/test_session_trace.py -q`

Expected: all tests pass.

- [x] **Step 7: Review checkpoint**

Inspect live-follow rotation/truncation behavior and confirm snapshot fallback is labeled accurately. Do not commit without user authorization.

---

### Task 4: CLI launcher and package inclusion

**Files:**
- Create: `box_agent/trace_viewer/__init__.py`
- Create: `box_agent/trace_viewer/launcher.py`
- Modify: `box_agent/cli.py`
- Modify: `pyproject.toml`
- Modify: `MANIFEST.in`
- Modify: `tests/test_trace_viewer.py`
- Modify: `tests/test_cli_config.py`

**Interfaces:**
- Consumes: `box-agent trace-viewer` CLI invocation.
- Produces: `viewer_path() -> Path` and `launch_trace_viewer(*, open_browser: bool = True) -> Path`.

- [x] **Step 1: Write failing launcher and CLI tests**

```python
def test_launcher_opens_packaged_index(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda uri: opened.append(uri) or True)
    path = launch_trace_viewer()
    assert path.name == "index.html"
    assert opened == [path.resolve().as_uri()]

def test_main_dispatches_trace_viewer_before_config(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["box-agent", "trace-viewer"])
    monkeypatch.setattr(cli.Config, "_ensure_user_config", lambda: (_ for _ in ()).throw(AssertionError("config touched")))
    monkeypatch.setattr(cli, "launch_trace_viewer", lambda: Path("index.html"))
    assert cli.main() == 0
```

- [x] **Step 2: Run launcher tests and verify RED**

Run: `uv run pytest tests/test_trace_viewer.py tests/test_cli_config.py -k trace_viewer -v`

Expected: FAIL because launcher and subcommand do not exist.

- [x] **Step 3: Implement the launcher and early CLI dispatch**

```python
def viewer_path() -> Path:
    path = Path(__file__).with_name("index.html")
    if not path.is_file():
        raise FileNotFoundError(f"Trace viewer asset not found: {path}")
    return path

def launch_trace_viewer(*, open_browser: bool = True) -> Path:
    path = viewer_path().resolve()
    if open_browser and not webbrowser.open(path.as_uri()):
        raise RuntimeError(f"Could not open browser; open this file manually: {path}")
    return path
```

Add the argparse subcommand and handle it in `main()` before `Config._ensure_user_config()`.

- [x] **Step 4: Include all viewer assets in source and wheel packages**

Add `trace_viewer/*` to `[tool.setuptools.package-data]` and `recursive-include box_agent/trace_viewer *` to `MANIFEST.in`.

- [x] **Step 5: Run CLI/package tests**

Run: `uv run pytest tests/test_trace_viewer.py tests/test_cli_config.py -q`

Expected: all tests pass.

Run: `uv build`

Expected: wheel and sdist contain `index.html`, `styles.css`, `trace_model.js`, and `app.js`.

- [x] **Step 6: Review checkpoint**

Inspect built archives and CLI captured output. Do not install or publish without separate authority.

---

### Task 5: Documentation, browser probe, and regression verification

**Files:**
- Modify: `README.md`
- Modify: `README_CN.md`
- Modify: `docs/superpowers/plans/2026-08-20-agent-trace-viewer.md`

**Interfaces:**
- Consumes: the completed CLI and static assets.
- Produces: developer usage documentation and recorded proof.

- [x] **Step 1: Document launch and privacy behavior**

Add English and Chinese examples for `box-agent trace-viewer`, default trace location `~/.box-agent/log/sessions/*.jsonl`, snapshot versus live-follow browser behavior, and the warning that traces may contain prompts/business data.

- [ ] **Step 2: Run focused and broader automated checks**

Run: `uv run pytest tests/test_trace_viewer.py tests/test_session_trace.py tests/test_cli_config.py -q`

Run: `uv run pytest tests/test_cli_runtime.py tests/test_architecture_boundaries.py -q`

Run: `git diff --check`

Expected: all checks pass.

Observed: focused viewer/trace/CLI checks and architecture boundaries pass. `tests/test_cli_runtime.py` has two failures outside this diff involving `HOME` directory resolution and colon-splitting a `C:` drive in `NODE_PATH`. The full suite reports `174 failed, 2488 passed, 65 skipped`; failures span ACP, PPTX, Windows path/file-lock, optional runtime, and other unchanged subsystems, so the repository cannot be reported as globally green from this worktree.

- [ ] **Step 3: Perform a local browser probe**

Run: `uv run python -m box_agent.cli trace-viewer`

Open `tests/fixtures/session_trace_viewer.jsonl`. Verify summary, Waterfall, Conversation, Events, inspector, filtering, copy, literal rendering of HTML-like payloads, and zero remote network requests. Append a complete record and then a partial line to verify follow behavior.

Blocked: the available in-app browser rejects local `file://` navigation, and its control policy prohibits using an alternate browser or localhost workaround after that rejection.

- [x] **Step 4: Inspect final scope and package diff**

Run: `git status --short --branch`

Run: `git diff --stat`

Run: `git diff -- box_agent/kernel box_agent/observability/session_trace.py box_agent/acp`

Expected: no stable-kernel, trace-writer, or ACP changes.

- [x] **Step 5: Final review checkpoint**

Report exact source/test/build/browser boundaries, unverified packaged-runtime boundaries, and remaining v1 sub-agent nesting limitation. Do not commit, push, install, or publish without explicit user authority.
