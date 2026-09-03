# Main Divergence Migration Design

## Problem

`refactor-agent-architecture` and `origin/main` diverged at
`e4167a98734ec2abf466510ad94f414dc2b17113`. The refactor branch introduced
stable API contracts, one `AgentLoopKernel`, `KernelAgentService`, typed plugin
registries, capability packages, and thin CLI/ACP/SDK adapters. Main continued
shipping behavior on the pre-refactor structure and later removed the Workflow
layer while making a JSONL Session Log the sole recovery source.

Merging final files or cherry-picking commits would restore policy to
`core.py`/ACP, delete the new Workflow Plugin boundary, and create a second
recovery owner. The migration therefore ports observable behavior and tests,
not main's ownership decisions.

## Architectural invariants

1. `AgentLoopKernel` is the only component that advances an Agent run.
2. `KernelAgentService` owns Session/Run lifecycle, replay, leases, idempotency,
   cancellation, and durable resume.
3. The stable `box_agent.api` contracts remain host-neutral and serializable.
4. LLM/provider compatibility belongs in `box_agent/llm/`; a session selects
   it through run metadata and a capability port.
5. Reusable completion, presentation, goal, plan, skill, and autopilot behavior
   remains a Workflow Plugin. Main's Workflow deletions are not migrated.
6. CLI and ACP translate host payloads and render events; they do not own model,
   tool, recovery, or workflow policy.
7. Tool safety is enforced in tool implementations or the shared tool engine,
   not only in prompts.
8. Durable `AgentEvent`/checkpoint/effect/session stores remain the canonical
   recovery source. Main's `SessionLog` class is treated as a behavior oracle,
   not copied as another persistence system.
9. Secrets are resolved at provider-call time and never copied into session
   metadata, traces, or events.
10. Generated Skill manifests are regenerated from source and reviewed with
    their Skill/tool changes.

## Migration inventory and disposition

| Main change family | Disposition in the refactor branch |
| --- | --- |
| JSONL Session Log and session-only recovery | Re-express missing guarantees through `KernelAgentService`, the event log, session store, leases, checkpoints, and effect ledger; do not add `box_agent/session_log.py` |
| Immutable model profile revisions | Add an LLM-layer profile resolver and extend shared binding normalization; select profiles in `LLMClientPort.for_run` so every host gets identical behavior |
| Hosted token refresh | Port to `box_agent/auth.py` and provider/MCP call boundaries; keep tokens outside metadata/events |
| LLM timeout, GLM 5.3, and error mapping fixes | Port directly to config/provider/error modules with focused wire tests |
| Lark quoted executable, MCP tool-name, attachment lookup, and chunk-write fixes | Port to shared environment/tool modules; ACP receives the behavior only through normal composition |
| SkillHub search/install | Add shared tools and register them through session tool contributors plus ACP extension callbacks; do not hardcode them into the Agent loop |
| Midu and PPTX Skill assets | Copy source assets, regenerate `_manifest.json`, and run matcher/script contract tests |
| Completion/PPTX workflow fixes | Port intent and checkpoint behavior into Workflow Plugins; keep final safety guards in tools even when no workflow is active |
| Prompt/config tuning | Port values and concise contract language only after code defaults and tests agree |
| v0.9.7 release metadata | Do not claim or recreate an already published artifact from this refactor branch; reconcile version surfaces only as part of a separate release |
| Main documents that describe removed Workflow ownership | Do not copy; update the refactor documentation with migrated behavior and retained ownership |

## Acceptance criteria

- A profile binding with `source=profile`, `version=2`, `profileId`, and
  `profileRevision` resolves one immutable local profile without exposing
  credentials in normalized metadata.
- Near-expiry hosted credentials refresh once across concurrent requests,
  persist atomically, and fail with a structured authentication error when the
  refresh login is invalid.
- Provider timeout/error/thinking behavior matches current main contracts.
- Quoted Lark OAuth commands, provider-safe MCP aliases, attachment lookup
  guidance, and chunk-write rollback have direct regression coverage.
- SkillHub and new built-in Skills are composed through Plugin/tool registries,
  and generated manifests match their source.
- Completion and controlled-presentation fixes run through Workflow Plugins;
  direct tool bypasses remain blocked independently.
- Durable restart/replay tests prove the new event-backed service already
  covers or explicitly implements every applicable Session Log guarantee.
- Focused suites pass before broader core/ACP/tool/Skill/PPTX suites and the
  full test suite are attempted.

## Workspace constraint

The pre-existing uncommitted files `scripts/build_runtime.py`,
`scripts/build_win_runtime.py`, and `tests/test_build_runtime.py` are owned by
another workstream. This migration will not overwrite, stage, or claim them.
Runtime packaging validation must report that boundary until those changes are
reconciled by their owner.
