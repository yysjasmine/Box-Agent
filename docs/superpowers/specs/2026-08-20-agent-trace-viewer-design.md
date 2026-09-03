# Agent Trace Viewer Design

Date: 2026-08-20
Status: Implemented on `syy/agent-trace-viewer`; pending review

## Goal

Add an offline developer diagnostics page for Box Agent session traces. The
viewer opens an existing `box-agent-session-trace/v1` JSONL file entirely in
the browser and helps developers answer three questions:

1. What did the agent see and do, from system prompt through final response?
2. Where did the turn spend time across LLM and tool calls?
3. Which request, response, error, token count, or raw payload explains a
   failure or performance regression?

The viewer is a local diagnostic tool, not an end-user execution UI.

## Confirmed product decisions

- The viewer is a standalone page, not embedded in an ACP host.
- Its static assets work directly from `file://`. An optional loopback-only
  HTTP service supports directory paths in embedded browsers whose native
  folder picker is unavailable. Neither mode has a database, message queue,
  telemetry exporter, or remote dependency.
- It supports a historical directory ledger, individual file import, and
  best-effort live following on browsers that implement the File System Access
  API. Loopback service mode refreshes a selected directory when trace metadata
  changes.
- Waterfall and conversation-chain views coexist. The conversation chain is
  ordered vertically as system/context, user, assistant response, nested tool
  calls/results, subsequent assistant responses, and final response.
- Existing trace records are the MVP data source. The MVP does not change
  `box_agent/core.py`, the agent event contract, or ACP wire behavior.

## Existing source contract

`box_agent/observability/session_trace.py` writes one append-only JSON record per line with:

- `schema_version`, `timestamp`, and `event`;
- `session_id`, `acp_session_id`, and `turn_id`;
- optional `step`, `llm_call_id`, and `tool_call_id` correlation fields;
- event-specific `data`.

ACP currently adds `session.start`, `turn.input`, `turn.output`, `turn.end`,
`turn.error`, and cancellation records. The LLM wrapper adds `llm.request`,
`llm.response`, and `llm.error`. The core adds `tool.request` and
`tool.response`. LLM request records contain the exact message snapshot and
tool schemas sent to the provider. Response records contain model content,
thinking, tool calls, finish reason, provider IDs, usage, and timing. Tool
records contain arguments, visible/model result content, policy decisions,
success/error state, and duration.

The trace graph is used as an index only. The design is based on the current
source at `0b138daa119c8174b8394364f4b03876e3db638c`; the committed knowledge
graph was generated from an older baseline.

## Scope

### MVP capabilities

- Open a `.jsonl` trace with drag-and-drop, a normal file input, or the
  Chromium/Edge file picker.
- Open a directory and show a newest-first ledger of every usable top-level
  `.jsonl` trace with aggregate duration, turn, call, token, and error metrics.
- In loopback service mode, accept an explicit local directory path and refresh
  the ledger when files are added or changed.
- Parse records incrementally by line, preserve file order, and report invalid
  lines without discarding valid records.
- Select a turn and show summary metrics: status, duration, LLM/tool counts,
  total tokens, error count, and slowest call.
- Show a waterfall of paired LLM and tool request/response spans.
- Show a vertical conversation chain:
  - initial system/developer context;
  - prior conversation context collapsed into a count by default;
  - the selected turn's user input;
  - each assistant response;
  - tool calls and results nested below the response that requested them;
  - the final response, deduplicated from the last LLM response when equal.
- Select any node/span and inspect formatted content plus the original record.
- Filter by event kind, success/error state, tool/model name, call ID, or text.
- Copy a record or visible content to the clipboard.
- Follow appended bytes when the browser grants a persistent file handle.
- Work without external network access and without sending trace contents
  beyond the browser or optional loopback service.

### Out of scope

- Automatic discovery or implicit selection of `~/.box-agent/log/sessions`;
  users must explicitly choose or enter a directory.
- Recursive directory traversal or aggregation across unrelated directories.
- Multi-user access, authentication, shared links, or cloud persistence.
- Editing, replaying, or resuming an agent run.
- OpenTelemetry export or compatibility with arbitrary third-party traces.
- Reliable nested sub-agent trees. Existing v1 records do not attach child LLM
  calls to a stable parent sub-agent span, so MVP shows those events in file
  order and labels the limitation.

## Architecture

The feature is isolated under `box_agent/trace_viewer/`:

```text
box_agent/trace_viewer/
  index.html          document shell and accessible regions
  styles.css          diagnostic-console layout and responsive rules
  trace_model.js      JSONL parsing, validation, normalization, correlation
  app.js              file/directory access, state, rendering, live refresh
  launcher.py         config-independent static-page launcher
  server.py           optional loopback-only directory service
```

Classic scripts are used instead of ES modules so the viewer works directly
from `file://` without a build step or local web server. The optional service
serves those same packaged assets and exposes only health and explicit
directory-read endpoints. There are no third-party JavaScript packages or CDN
assets.

`box-agent trace-viewer` is an additive CLI adapter that opens the packaged
`index.html` in the default browser. The subcommand is handled before config
initialization, so diagnostics remain available when API configuration is
missing or broken. Users can also open `index.html` directly.

Embedded browsers that do not expose a native folder picker can start the
optional service explicitly:

```text
uv run python -m box_agent.trace_viewer.server --port 8766
```

The service binds only to `127.0.0.1`/`localhost`. It accepts a directory path
from the local page, reads only non-symlink top-level `.jsonl` files, limits an
individual trace to 50 MiB and a directory response to 200 MiB, and never
writes to the selected directory.

The assets are added to setuptools package data. Frozen-runtime inclusion and
host installation remain a separate deployment boundary and must be verified
by a runtime build/probe before packaged behavior is claimed.

## Normalized browser model

`trace_model.js` exposes pure functions and produces the following logical
model:

```text
TraceSession
  metadata
  warnings[]
  records[]                 original parsed objects plus source line number
  turns[]
    summary
    spans[]                 llm | tool, request/terminal pair
    conversation[]          system | context | user | assistant | tool | final
    errors[]

TraceCatalog
  directory
  skipped[]
  entries[]
    file metadata
    TraceSession
    aggregate summary
```

### Parsing rules

- Empty lines are ignored.
- Each non-empty line is parsed independently. An invalid line produces a
  warning with its 1-based line number; later records still load.
- Records stay in file order. Timestamps are parsed for metrics but never used
  to reorder concurrent events.
- Unknown event names remain available in the event table and raw inspector.
- Unknown schema versions produce a prominent compatibility warning rather
  than a hard failure when the base envelope is still readable.
- A configurable 50 MiB soft limit warns before loading. The user may continue
  explicitly; the viewer never silently truncates a trace.

### Span correlation

- LLM spans pair `llm.request` with the first terminal `llm.response` or
  `llm.error` sharing `llm_call_id`.
- Tool spans pair `tool.request` with the first terminal `tool.response`
  sharing `tool_call_id`.
- Missing request or terminal records produce an incomplete span instead of
  disappearing.
- Duration prefers the terminal record's explicit timing/duration. Otherwise
  it is derived from timestamps. Negative or invalid durations are displayed
  as unavailable and reported as warnings.
- Turn duration prefers `turn.end.data.duration_ms`, falling back to its first
  and last valid timestamps.

### Conversation reconstruction

The first `llm.request` in a selected turn is the authoritative prompt
snapshot. Its messages are classified by role and displayed in their original
order. System/developer messages are shown first and collapsed when long.
Messages that predate the selected `turn.input` are grouped as prior context by
default but remain expandable.

Each `llm.response` becomes an assistant node. Tool calls declared on that
response are matched by call ID and rendered as children containing request
arguments, result/error, policy information, and duration. Subsequent LLM
responses follow those tool nodes. If `turn.output.data.content` equals the
last assistant content, the existing assistant node receives a `final` marker;
otherwise a separate final node is added.

The UI must distinguish three different payloads when present:

- user-visible tool result content;
- model-visible tool result content;
- raw structured output.

No trace text is inserted with `innerHTML`; all untrusted values are rendered
with DOM text nodes or `textContent`.

## User interface

### Shell

- Header: file name, import action, follow status, compatibility warnings.
- Summary strip: duration, LLM calls/time, tool calls/time, tokens, stop reason.
- Left rail: turns, search, event-type and status filters.
- Main region tabs: `Waterfall`, `Conversation`, and `Events`.
- Right inspector: formatted content, metadata, raw JSON, copy action.

### Waterfall

Each span occupies one row on a shared turn time axis. LLM, tool, error, and
incomplete spans use distinct colors and text labels, not color alone. The
viewer identifies the slowest LLM/tool call and reports time shares without
claiming a distributed critical path that v1 trace data cannot prove.

### Conversation

The main axis runs top to bottom. Role cards use explicit headings (`SYSTEM`,
`USER`, `ASSISTANT`, `TOOL`, `FINAL`) and accessible nested structure. Long
system prompts, tool schemas, thinking, and tool results are collapsed by
default. Tool nodes are indented beneath the assistant response that emitted
their call ID. Clicking a node opens the corresponding source record(s) in the
inspector.

### Events

A compact, paginated table provides the lossless escape hatch for unknown or
uncorrelated records. It shows source line, timestamp, event, turn, step, call
ID, and status.

### Responsive behavior

Desktop widths use the three-column diagnostic layout. Below 1000 px the
inspector becomes a drawer. Below 720 px the turn rail becomes a selector and
the waterfall horizontally scrolls. Desktop remains the primary target.

## Live-follow behavior

When `showOpenFilePicker` is available, the viewer retains the granted file
handle for the page lifetime. Once per second it obtains a fresh `File`
snapshot. Appended bytes are read from the last byte offset, with an unfinished
line retained as a tail buffer. A smaller file size signals rotation or
truncation and triggers a confirmed full reload.

The ordinary `<input type="file">` and drag/drop paths load immutable browser
snapshots. The UI labels these as `snapshot`; it does not promise live updates.
Polling stops when the page is hidden and resumes when visible.

In loopback service mode, the page polls top-level file name, size, and modified
time once per second. It requests full trace bodies only when that metadata
revision changes, then rebuilds the ledger. This is directory-level live
refresh, distinct from byte-offset following of an individual file handle.

## Security and privacy

- The page has no analytics, external fonts, remote scripts, or external
  network requests. Service mode communicates only with its same-origin
  loopback server.
- A restrictive Content Security Policy allows only packaged scripts/styles
  and local blob/data resources required by the viewer.
- Trace values are treated as untrusted text. They cannot inject HTML, script,
  URLs, or CSS.
- The page never persists trace content to localStorage or IndexedDB. Only
  harmless UI preferences may be stored.
- Existing writer-side credential-key redaction remains the primary producer
  safeguard. The viewer displays a visible reminder that prompts and tool
  outputs may still contain sensitive business data.
- The service rejects non-loopback bind addresses and requests whose `Host` or
  `Origin` does not match its exact loopback authority. It ignores symlinks and
  nested files, applies request/file/directory size bounds, and exposes no
  write, replay, delete, or arbitrary-file endpoint.

## Error handling

- Invalid JSONL lines: retain valid records and list line-specific warnings.
- Unsupported/partial records: render raw data and mark derived fields unknown.
- Missing call pair: show an incomplete span.
- Clipboard or file-handle denial: show a non-blocking local message and keep
  the loaded snapshot usable.
- Large input: warn before parsing and keep the current trace unchanged if the
  user cancels.
- Render failure: isolate the failed panel, keep raw events accessible, and
  surface the error without logging trace contents to a remote destination.

## Verification

### Automated

- Node-based unit tests for pure `trace_model.js` behavior, using only Node's
  built-in test/assert modules:
  - valid and malformed JSONL;
  - unknown schema/event preservation;
  - LLM/tool pairing and incomplete pairs;
  - duration and token aggregation;
  - conversation ordering and final-response deduplication;
  - nested tool association;
  - HTML-like payloads retained as plain strings.
- Pytest checks for CLI parsing/dispatch, config-independent launch, package
  data presence, the no-external-resource/CSP contract, loopback binding,
  directory filtering and limits, malformed requests, and metadata-only live
  refresh.
- Existing focused session-trace and CLI tests remain green.

Node behavior tests run when a Node executable is available. The repository's
packaged runtime already manages Node for several artifact workflows, but lack
of Node must be reported explicitly rather than described as a passing browser
behavior check.

### Manual browser probe

- Open a representative trace in Chromium/Edge.
- Open a representative directory and verify newest-first aggregate rows,
  drill-down, inspector dismissal, and automatic discovery of an added trace.
- Verify waterfall, conversation ordering, filters, inspector, and raw JSON.
- Append complete and partial JSONL lines while follow mode is enabled.
- Verify a malicious-looking payload such as `<img onerror=...>` is displayed
  literally and never executed.
- In static mode, verify the Network panel shows no requests after local assets
  load. In service mode, verify requests stay on the same loopback origin and
  that non-loopback `Host` and `Origin` values receive HTTP 403.
- Repeat snapshot import in a non-File-System-Access browser when feasible.

## Documentation and compatibility

README usage will document:

```text
box-agent trace-viewer
```

and the default trace location:

```text
~/.box-agent/log/sessions/*.jsonl
```

The feature is additive. It reads `box-agent-session-trace/v1` without changing
the writer contract. Unknown future fields are preserved. The loopback service
adds a local developer entry point but no ACP or provider protocol. Any later
schema change for nested sub-agent relationships requires its own compatibility
design and producer/consumer tests.

## Implementation boundary

Expected implementation changes are limited to the new viewer assets, a thin
CLI launch path, the optional loopback directory service, package-data
configuration, focused tests, and user-facing documentation. The agent loop,
trace writer, providers, ACP translation, and stable kernel remain unchanged
unless implementation discovers a demonstrated v1 data gap that blocks an
approved MVP requirement.
