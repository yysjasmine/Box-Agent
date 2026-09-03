# ACP end-to-end acceptance and visual report

These tests enter the single Agent Kernel through the ACP adapter and use deterministic
fake plugins. They need no network, API key, or real model. The assertions target
public protocol facts rather than private implementation details.

## Run

```powershell
python tests/e2e/run_acp_cases.py --report tests/e2e/report.json
python -m pytest tests/e2e -q
python -m http.server 8765 --directory tests/e2e
# open http://localhost:8765/report.html
```

The viewer reads `report.json` beside the runner. If the file is missing or
invalid, it renders an actionable empty state. When opening `report.html` via
`file://`, use the **Load report.json** picker.
The runner accepts only a `.json` `--report` target, so test data cannot
accidentally overwrite `report.html`, its stylesheet, or its script.

## Cases

| Case | Purpose | Acceptance fact |
| --- | --- | --- |
| `text` | Minimal ACP → Kernel round trip | `run.started`, model events, and `run.completed` are ordered and the final text is stable |
| `tool_permission` | Tool and permission boundary | `permission.requested` precedes the executor; denial has no side effect |
| `context_memory` | Context and memory plugin wiring | `memory.recalled`, `context.assembled`, and a recoverable manifest are emitted |
| `workflow_continuation` | WorkflowPolicy continuation | One budget-bounded next turn follows `workflow.continuation.requested` |
| `resume` | Recovery from a durable boundary | A new Service restores SQLite state and reproduces the original event sequence and terminal state |

`test_acp_stdio_smoke.py` performs a real child-process stdio check for
`initialize → session/new`; the deterministic cases do not invoke a real LLM.

## Report shape

Each case has this shape:

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

The zero-dependency static page provides case filtering, event timelines,
expandable payloads, failed-assertion details, and JSON copy. It never connects
to a production service.

## Troubleshooting

- `permission-before-executor`: verify that the Tool implements `preflight` and is registered through `tools.engine.RegistryToolEngine`.
- `context-manifest`: verify that the Context Engine returns `ContextBuildResult`; the Kernel creates a deterministic hash when the manifest is omitted.
- `continuation-budget`: verify a stable `WorkflowContinuation` from `next_continuation` and the `RunOptions.max_steps` bound.
- `resume-events`: verify that the SQLite path is writable and the plugin lock matches; a plugin-version change intentionally fails closed.

## Parity evidence

ACP E2E proves the public protocol-to-Kernel boundary. Complex workflow
behavior is verified separately by deterministic fixtures in `tests/parity/`.
`migration_status.json` records Goal, Plan, PPT, Skill, Completion Gate, and
Autopilot as `parity_passed` and the pre-Kernel loop as retired.
