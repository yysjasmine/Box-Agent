# Runtime Capability Coverage

The runtime has one execution owner: `AgentLoopKernel`. ACP, CLI, SDK, and the
historical `Agent.run_events()` API submit `box_agent.api` contracts through
`KernelAgentService` and only render or translate `AgentEvent` values. There is
no runtime selector or pre-Kernel fallback. Goal, Plan, PPT, Skill, Completion
Gate, and Autopilot are native workflow plugins with deterministic parity
fixtures.

An explicit third-party `workflow_id` is resolved through the workflow
registry. If the key is not registered, composition fails closed with
`KernelCompositionError` instead of silently running without its policy.

## Runtime directory map

```text
box_agent/
├─ api/              stable DTOs, events, commands, errors, ports, handles, host protocols
├─ kernel/           AgentLoopKernel, workflow composition, and per-run DI
├─ services/         runtime service lifecycle, replay, control and leases
│  ├─ kernel.py      KernelAgentService implementation
│  └─ delegation.py  service-owned child-run delegation
├─ plugins/          manifests, scoped registries, activation/disposal
├─ context/          ContextEngine SPI and deterministic compaction reference
├─ tools/engine.py   registry-backed schema/permission/execute boundary
├─ tools/runtime_context.py
│                    per-invocation session/run identity for stateful tools
├─ memory_engine/    MemoryEngine SPI, store, maintenance, and composition
├─ permissions/      fail-closed permission gateway reference
├─ persistence/      Session/Event/Checkpoint/Effect/Lease durable stores
├─ adapters/         ACP/CLI/SDK protocol façades; CLI implementation in cli/app.py
├─ compat/           stable historical call-shape/import façades over Kernel
└─ workflows/        composable Goal/Plan/PPT/skill workflow policies
```

### File responsibilities

| Path | Responsibility |
| --- | --- |
| `api/contracts.py` | Serializable `Message`, `RunRequest`, `RunOptions`, `RunResult`, usage and artifact DTOs |
| `api/plan.py` | Single host-facing Plan start/approval payload protocol shared by compatibility callers, native WorkflowPolicy, and ACP |
| `api/events.py` / `api/controls.py` / `api/errors.py` | Ordered event envelope, external control command/ACK, stable error codes; `workflow.plan.snapshot`, `workflow.continuation.requested`, `workflow.control.applied`, and terminal `metadata` are durable workflow boundary facts |
| `api/ports.py` / `api/workflows.py` / `api/handles.py` | The single definitions of Context/Tool/Permission/Memory/LLM/Hook/Workflow SPIs, workflow action/checkpoint DTOs, and service/loop handles |
| `plugins/api.py` / `plugins/registry.py` / `plugins/host.py` | Manifest, discovery, scoped registration, dependency activation, conflict handling, disposal and plugin lock snapshot; historical registry names are object aliases of canonical registries, never dual-written stores |
| `plugins/builtins.py` | Shared built-in workflow graph and registrations used by ACP and CLI; adapters do not reconstruct Goal/Plan/PPT/Skill policy graphs |
| `kernel/loop.py` | The only execution state machine: context → model stream → bounded tools → events/result, cancellation, recovery, and optional plugin-owned continuation turns |
| `kernel/composer.py` | Resolves typed registries and builds one Kernel; no host protocol logic |
| `kernel/workflow_composite.py` | Fans the stable WorkflowPolicy contract out to independent registered policies without importing a concrete workflow |
| `services/kernel.py` | Session/Run lifecycle, request idempotency, event replay, leases, control routing and persisted ACKs |
| `context/api.py` / `context/in_memory.py` / `context/composite.py` | Context item/build contracts, provider adapter, and provider→compactor pipeline |
| `context/task.py` | Canonical session/task/turn identity and validation; root `task_context.py` remains a compatibility shim |
| `context/model_history.py` | Model-history placeholder and instruction-retention classification; root `model_history.py` remains a compatibility shim |
| `context/experts.py` / `evidence.py` | Expert metadata Context plugin, typed host projection, and search-evidence normalization |
| `memory_engine/api.py` / `in_memory.py` / `composite.py` | Memory recall/write/flush contracts, reference backend, provider adapters, and split provider/store/writer composition |
| `memory_engine/store.py` / `maintenance.py` | Canonical long-term memory storage, extraction helpers, consolidation, and maintenance jobs; root modules are import facades |
| `tools/engine.py` | Tool schema lookup, side-effect-free validation, preflight permission boundary, Hook interceptors, normalized results and two-phase effect-ledger fencing (`prepare_call` before `tool.call.requested`, terminal transition before `tool.call.completed`); completed events retain canonical `tool_name`/`arguments` for workflow replay |
| `tools/runtime_context.py` | Binds session/run identity for one invocation without adding hidden schema arguments |
| `permissions/gateway.py` | Fail-closed default permission policy |
| `persistence/sqlite.py` | Shared SQLite schema for events, requests, sessions, effects, leases and control ACKs |
| `persistence/event_log.py` / `sessions.py` / `effects.py` / `leases.py` | Concrete durable stores and recovery/session/side-effect/fencing operations |
| `persistence/session_continuation.py` | Bounded, validated host-provided continuation snapshots; root `session_continuation.py` remains a compatibility shim |
| `persistence/task_registry.py` | Durable task/artifact lineage records; root `task_registry.py` remains a compatibility shim |
| `persistence/workspace_registry.py` | Atomic workspace profile configuration shared by hosts; root `workspace_registry.py` remains a compatibility shim |
| `persistence/workflow_checkpoint_store.py` / `workflow_owner_store.py` | Trusted workflow pause state and runtime owner persistence; root modules remain compatibility shims |
| `persistence/artifacts.py` / `roadmap_artifacts.py` | Artifact envelopes, naming/scanning, and roadmap metadata validation; root modules are import facades |
| `adapters/service.py` / `adapters/hosts.py` | Protocol-neutral ACP/CLI/SDK transport façades; all only render or collect `AgentEvent` values. `run.events`/`run.wait` observe via `attach`; takeover is explicit through `resume`/control |
| `acp/bootstrap.py` / `adapters/acp_kernel.py` / `acp/kernel_runtime.py` | Lightweight stdio handshake, protocol projection, and transport-independent PluginHost/Service assembly; no loop policy is duplicated in ACP |
| `adapters/cli/app.py` | Canonical CLI implementation; root `cli.py` is the executable/import facade |
| `compat/` | Preserves historical `Agent`, Core helper, runtime, event, CLI, and ACP imports without creating a second execution owner; `compat/runtime.py` translates them into stable requests/events and `compat/goal.py` preserves mutable `Agent.goal` identity over `GoalStore` |
| `observability/logger.py` / `session_trace.py` / `cache_fingerprint.py` | Human logs, durable JSONL diagnostics, redaction, and deterministic request fingerprints |
| `workflows/composite.py` | Compatibility alias of the Kernel-owned generic workflow compositor |
| `workflows/contract.py` | Compatibility export of the canonical `api/ports.py` WorkflowPolicy and `api/workflows.py` DTOs, plus the natural-end helper; it defines no second SPI |
| `workflows/hooks.py` | Optional event hook that forwards ordered Kernel events to workflow plugins |
| `workflows/goal.py` | Canonical session-scoped Goal state machine, read/write tools, host-neutral control protocol, checkpoint policy, typed Autopilot continuation, and shared ACP/CLI terminal-message formatter; hosts do not own duplicate Goal transitions, and ACP persists between-run Goal controls directly in the Session Store |
| `workflows/plan.py` | Session-scoped Plan store and legacy-compatible plan tools/checkpoint policy; optional approval preflight blocks non-plan tools until an approved run option is supplied, emits a native `workflow.plan.snapshot` start fact using `api/plan.py`, injects the legacy-equivalent forced-plan guidance through Context, restores the start snapshot after an interruption, decorates pending approval through the shared protocol, and can pause durably after `plan_write` until approval |
| `workflows/external_skill.py` | Native external-Skill lifecycle policy: resolves the selected Skill from the injected loader, contributes its pinned instructions plus content hash to Context, applies a bounded tool/artifact completion gate, pauses at user-decision tools, and checkpoints the artifact baseline for restart-safe replay |
| `workflows/controlled_presentation.py` | Native controlled-PPT policy: applies per-run research/image options, exposes bounded stage/evidence checkpoint state, contributes the registered `pptx` Skill context, enforces the delivery budget, and publishes pause/terminal lifecycle facts |
| `workflows/completion.py` / `guards.py` / `delivery.py` / `turn_policy.py` | Completion composition, pure guard decisions, deliverable intent, and turn classification |

The rest of `box_agent/` is capability code: providers, MCP, skills, workspace
tools, workflows, artifacts, trace viewer, and host entrypoints. None owns an
independent Agent loop.

## Current coverage

| Capability | Runtime owner | Status |
| --- | --- | --- |
| LLM provider streaming, usage, thinking, provider IDs | `LLMPort`, `LLMClientPort`, `ModelChunk` | Available |
| Context assembly, compaction, pinned items, resource metadata | `ContextEngine`, `InMemoryContextEngine` | Available |
| Tool schemas, validation, execution, unknown-tool errors | `ToolExecutor`, `RegistryToolEngine` | Available |
| Permission approval/deny | `PermissionPolicy`, `PermissionNegotiatorGateway`, `RegistryToolEngine` | Available |
| Memory recall/write/flush | `MemoryEngine` provider/store/writer plugins | Available |
| MCP, skills, file tools, Jupyter, web tools, sub-agent tools | registered as ordinary Tool plugins | Available through tool registration |
| Hooks and workflow policies | `Hook`/`WorkflowPolicy` registries and Kernel hooks/checkpoint event | Available |
| Artifact descriptors | `Artifact`, `artifact.created` event | Available when returned by a tool |
| Session/run identity and external controls | `KernelAgentService`, `ControlCommand`, `control.routes` registry, `control_commands` ledger | Available; custom commands receive idempotent ACKs across process restarts, `control.received` facts, and optional `WorkflowPolicy.handle_control()` application at the next safe boundary |
| ACP/CLI/SDK transport rendering | `ACPServiceAdapter`, `KernelACPAgent`, `CLIServiceAdapter`, `SDKServiceAdapter` | Thin adapters over the single Kernel runtime |
| Durable replay and active continuation | `SQLiteEventLog`, `RecoveryBundle`, `RunCheckpoint`, `kernel_factory(request, bundle)`, `PluginHost.lock_snapshot()` | Service appends ordered facts and auto-checkpoints transition events with the plugin lock and event digest; event/checkpoint/outbox share one SQLite transaction, and both initial `running` and terminal effect transitions are included in the same commit on the native `RegistryToolEngine` path. `attach()` can observe a registered Run even before its first event. A new Kernel rebuilds model/tool context and counters from facts and refuses replay under a different active plugin lock. New turns rehydrate prior Session facts without consuming the new Run budget; workflow checkpoints carry structured state and terminal metadata for plugin rehydration. Non-transactional custom store/Tool Engine implementations use an explicit compatibility path |
| External side-effect idempotency | `SQLiteEffectLedger` + deterministic Kernel-generated `effect_id`/`idempotency_key` metadata | Every model tool call gets stable effect identity; completed effects are replayed; `running`/`unknown` effects fail closed until reconciled |
| Long-running worker fencing | `SQLiteLeaseStore` + `KernelAgentService` lease heartbeat | Lease is renewed during execution and released after the terminal event; loss cancels the worker before more effects |
| Deadline and bounded tool concurrency | `RunOptions.deadline_ms`, `max_parallel_tools` | Enforced by the new Kernel |
| Plan gates, retries, parallel execution, memory extraction, MCP lifecycle | Kernel + Workflow/Hook/Tool/Memory plugins | Available |
| Goal / autopilot state and turn continuation | `GoalWorkflowPolicy.next_continuation()` + `WorkflowContinuation` | Goal and Autopilot parity are passed. Native Kernel, CLI, and ACP share session-scoped tools/state, durable ACP controls, cancellation/artifact terminal boundaries, literal continuation events, and checkpointed continuation/no-progress counters; Goal/Autopilot prompts no longer trigger Legacy promotion |
| Goal / Plan state plugin seam | `workflows/goal.py`, `workflows/plan.py`, `CompositeWorkflowPolicy` | Goal and Plan parity are passed. Kernel/CLI/ACP register session-aware tools and replay-aware checkpoints; Plan now preserves the literal start→model→tool→checkpoint→pause event order, legacy `rawOutput` plan-card shape, approval/rejection/restart behavior, and default text-triggered pause without host-only hints |
| Completion Gate | `CompletionGateWorkflowPolicy`, `completion_gate_from_payload()` | Parity is passed. Third-party ACP/SDK callers can configure the gate as data through `workflow_options.completion_gate`; native ACP registers and selects it directly, while event order, evidence/artifact checks, budgets, replay, recoverable pause, and CLI/ACP wrap-up rendering are fixture-verified |
| Completion gates, plans, controlled presentation / external skill workflows | `CompletionGateWorkflowPolicy`, `PlanWorkflowPolicy`, `ControlledPresentationPolicy`, `ExternalSkillRunPolicy` | Available; all parity fixtures passed |
| Provider stale recovery, truncation repair, retry budgets | `kernel/model_recovery.py`, `kernel/model_stream.py`, `ResponseContinuationWorkflowPolicy` | Available |
| Workspace safety, DWS policy, permission prompts and grants | Tool plugins + `PermissionPolicy` SPI | Available; execution fails closed without a gateway |
| Artifact detection, delivery metadata and trace/event viewer | Workflow hooks + `Artifact`/`artifact.created` | Available |
| User input/decision tools, todos, plans, sub-agents and Jupyter sandbox | ordinary Tool/Workflow plugins | Available |

## Runtime invariant

There is exactly one execution state machine. Hosts register capabilities in
`PluginHost`; `PluginKernelComposer` binds a run-scoped Kernel; adapters never
fork policy or call `core.py` as an execution owner. Historical public APIs are
translation facades only.

## Third-party path

```python
host = PluginHost()
host.registries["llm"].register("default", my_llm, source="vendor.llm")
host.registries["context"].register("default", my_context, source="vendor.context")
host.registries["memory"].register("default", my_memory, source="vendor.memory")
host.registries["tools"].register("search", my_search_tool, source="vendor.search")
host.registries["control.routes"].register("run.pause", my_pause_handler, source="vendor.controls")

service = KernelAgentService.from_plugin_host(host)
```

第三方分发包可在 `pyproject.toml` 暴露 `box_agent.plugins` entry-point；宿主只需
发现并按依赖激活，核心代码无需改动：

```python
host = PluginHost()
await host.activate_many(PluginHost.discover())
service = KernelAgentService.from_plugin_host(host)
```

`discover()` 只加载并校验 manifest，不执行激活；`activate_many()` 再按依赖、scope
和生命周期完成注册，失败会回滚已激活节点。

For a side-effecting tool, pass `effect_id` and `idempotency_key` in
`ToolCallRequest.metadata` and provide an `EffectLedger` (register it as
`effects:default` or pass `effect_ledger=` to `from_plugin_host`).

The same service is then consumed by a CLI renderer, ACP transport, or SDK;
none of those hosts need to know how the components are implemented.

`AgentLoopKernel` is the state machine, not the third-party composition API.
When a workflow is selected, its policy must already be bound to the current
`RunRequest` (session identity, options, and recovery bundle). Third-party
hosts should therefore call `KernelAgentService.from_plugin_host(host)`; the
service/composer performs this binding for every Run. Direct kernel
construction is reserved for tests or callers that pass a run-bound policy.

For a run that selects one vendor implementation without changing the host
composition, set `RunOptions.component_keys`, for example
`{"context": "vendor.context", "tools": "vendor.tools"}`. The composer
resolves these keys only for that Run and still falls back to the registered
defaults. Tool schemas may be registered independently in
`tools.descriptors`; `tools.executors` is the registry for individual
executors, while a complete custom `ToolEngine` belongs in `tools.engines`
(`tools` remains a legacy alias). If a capability is registered directly (without a
`PluginManifest`), provide `version` and `state_schema` to `register()` so the
host's durable plugin lock can fence replay across restarts.

`context_provider`/`context_compactor` and
`memory_provider`/`memory_store`/`memory_writer` are accepted aliases when a
host splits those responsibilities across registries.

### Goal + Plan plugin composition

Goal and Plan can be registered like any other workflow plugins:

```python
from box_agent.workflows import (
    CompositeWorkflowPolicy, GoalStore, GoalWorkflowPolicy,
    SessionPlanStore, PlanWorkflowPolicy, WorkflowEventHook,
    build_goal_tools, build_plan_tools,
)

goal_store = GoalStore()
plan_store = SessionPlanStore()
for tool in (*build_goal_tools(goal_store), *build_plan_tools(plan_store)):
    host.registries["tools"].register(tool.name, tool, source="vendor.workflow")
workflow = CompositeWorkflowPolicy([
    GoalWorkflowPolicy(goal_store),
    PlanWorkflowPolicy(plan_store),
])
host.registries["workflows"].register(
    "default",
    workflow,
    source="vendor.workflow",
)
host.registries["hooks"].register(
    "workflow-state",
    WorkflowEventHook([workflow]),
    source="vendor.workflow",
)
service = KernelAgentService.from_plugin_host(host, hook_keys=("workflow-state",))
```

The Tool Engine binds `RunRequest.session_id` outside the public tool schema,
so concurrent sessions receive independent Goal/Plan snapshots.  Terminal
workflow checkpoint events also carry structured state; a run-local policy or
`WorkflowEventHook` can restore it from `RecoveryBundle` or prior session facts.
The machine-readable promotion record is
`tests/parity/migration_status.json`. Goal, Plan, PPT, Skill, Completion Gate,
and Autopilot are all `parity_passed`; `legacy_loop_retired` is true and no
runtime blocker remains.

## ACP E2E evidence

`docs/e2e/ACP_E2E_GUIDE_CN.md` defines the deterministic ACP cases and report
schema. Run `python tests/e2e/run_acp_cases.py --report tests/e2e/report.json`,
then open `tests/e2e/report.html`. The runner injects fake plugins through the
same `PluginHost` and `KernelAgentService` used by real hosts, so the evidence
covers protocol boundaries without network or provider credentials.
