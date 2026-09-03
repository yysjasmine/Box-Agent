# Main Divergence Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the user-visible, safety, provider, recovery, Skill, and PPTX improvements made on `origin/main` after `e4167a9` into the single-Kernel architecture without restoring legacy execution owners.

**Architecture:** Treat main tests and commits as behavior specifications. Shared behavior lands in LLM, Tool, Persistence, or Workflow Plugin modules; `KernelAgentService` remains the lifecycle owner and adapters only translate host protocols. Main's JSONL Session Log and Workflow deletion are not copied because the refactor's durable event/service model supersedes those ownership choices.

**Tech Stack:** Python 3.10+, asyncio, Pydantic, pytest/pytest-asyncio, SQLite durable stores, ACP/MCP stdio protocols, JavaScript PPTX contract scripts.

**Spec:** `docs/superpowers/specs/2026-09-03-main-divergence-migration-design.md`

## Global Constraints

- Preserve the stable `box_agent.api` contracts and single `AgentLoopKernel` execution owner.
- Keep CLI/ACP/SDK adapters policy-free; provider, tool, recovery, and workflow rules belong to their shared modules.
- Add one observable regression test before each production behavior change and verify the expected RED failure.
- Never copy credentials into session metadata, events, traces, fixtures, or committed configuration.
- Regenerate `box_agent/skills/_manifest.json` only with the repository generator.
- Do not edit or stage `scripts/build_runtime.py`, `scripts/build_win_runtime.py`, or `tests/test_build_runtime.py`.
- Do not copy main's Workflow deletions, legacy core loop ownership, or Session Log persistence class.

---

### Task 1: Immutable session model profiles

**Files:**
- Create: `box_agent/llm/model_profiles.py`
- Create: `tests/test_model_profiles.py`
- Modify: `box_agent/llm/binding.py`
- Modify: `box_agent/adapters/capabilities.py`
- Modify: `box_agent/llm/__init__.py`
- Test: `tests/test_capability_adapters.py`

**Interfaces:**
- Consumes: `normalize_llm_binding(meta)`, `LLMClient.for_model(...)`, `LLMClientPort.for_run(...)`.
- Produces: `load_model_profile_revision(revision) -> dict`, `client_for_model_profile(binding, fallback_client) -> LLMClient`, and normalized v2 profile bindings containing `profileId`, `profileRevision`, and `routingMode`.

- [x] Add main's three profile registry tests plus a capability-port test proving a `source=profile` run resolves the selected provider/endpoint and leaves fallback state unchanged.
- [x] Run `pytest tests/test_model_profiles.py tests/test_capability_adapters.py -q` and confirm failure because the profile resolver and v2 binding are absent.
- [x] Implement strict registry/version/identity/positive-budget validation in `model_profiles.py` and extend `normalize_llm_binding` for `source in {builtin, profile}`.
- [x] Resolve profile clients only inside shared LLM binding/capability code; normalized request metadata retains identifiers but no `apiKey` or refresh token.
- [x] Re-run the focused tests and confirm all pass.

### Task 2: Hosted auth refresh and provider compatibility

**Files:**
- Modify: `box_agent/auth.py`
- Modify: `box_agent/llm/base.py`
- Modify: `box_agent/llm/anthropic_client.py`
- Modify: `box_agent/llm/openai_client.py`
- Modify: `box_agent/llm/llm_wrapper.py`
- Modify: `box_agent/llm/error_messages.py`
- Modify: `box_agent/config.py`
- Modify: `box_agent/config/config-example.yaml`
- Modify: `tests/test_auth.py`
- Modify: `tests/test_error_messages.py`
- Modify: `tests/test_llm_timeout.py`
- Modify: `tests/test_thinking.py`

**Interfaces:**
- Produces: `refresh_hosted_auth_token_if_needed(api_base, auth_file) -> str`, per-auth-file async refresh deduplication, dynamic MCP reads of the same rotated auth file, 1200-second main-Agent timeout, actionable SenseNova context errors, and GLM 5.3 `high`/`low` reasoning mapping.

- [x] Add the five hosted-refresh tests from main and focused assertions for timeout, context-error classification, and GLM 5.3 wire parameters.
- [x] Run the four focused test modules and confirm the new assertions fail for missing refresh/default/mapping behavior.
- [x] Implement JWT-expiry detection, refresh endpoint invocation, atomic auth-file replacement, and loop-scoped refresh locks in `auth.py`; invoke refresh immediately before hosted provider requests while MCP dynamically reads the same auth file per request.
- [x] Update main-Agent defaults to 1200 seconds while preserving the 600-second lite default; port only structured error and GLM dialect changes.
- [x] Re-run the focused tests and confirm all pass.

### Task 3: Shared tool and host-boundary fixes

**Files:**
- Modify: `box_agent/context/environment.py` (`box_agent/acp/env_context.py` remains a compatibility facade)
- Modify: `box_agent/tools/bash_tool.py`
- Modify: `box_agent/tools/file_tools.py`
- Modify: `box_agent/tools/mcp_loader.py`
- Modify: `box_agent/tools/setup.py`
- Modify: `box_agent/tools/skill_execution_env.py`
- Modify: `tests/test_env_context.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_file_tool_size_guard.py`
- Modify: `tests/test_mcp.py`
- Modify: `tests/test_skill_runtime.py`

**Interfaces:**
- Produces: `_public_mcp_tool_name(server_name, remote_name)`, atomic chunk acceptance/rollback, quoted-Lark command classification, and deterministic attachment search guidance.

- [x] Add focused regression tests for quoted absolute `lark-cli` OAuth paths, two colliding invalid MCP names, remote-name execution, short/failed chunk writes, and workspace-based attachment recovery instructions.
- [x] Run those exact tests and confirm behavior-specific failures.
- [x] Implement provider-safe MCP aliases while retaining `remote_name`, fsync and rollback each accepted chunk, normalize only the closing executable quote for Lark classification, and centralize attachment search guidance in shared tool setup.
- [x] Re-run the focused tests and the existing `tests/test_tools.py` safety subset.

### Task 4: SkillHub as plugin-composed tools

**Files:**
- Create: `box_agent/tools/skillhub_search_tool.py`
- Create: `box_agent/tools/skillhub_install_tool.py`
- Create: `box_agent/tools/skillhub_contributor.py`
- Modify: `box_agent/tools/__init__.py`
- Modify: `box_agent/plugins/builtins.py`
- Modify: `box_agent/adapters/acp_kernel.py`
- Modify: `box_agent/acp/kernel_runtime.py`
- Create: `tests/test_skillhub_search_tool.py`
- Create: `tests/test_skillhub_install_tool.py`
- Modify: `tests/test_acp_kernel_adapter.py`

**Interfaces:**
- Produces: read-only `search_skillhub`, confirmed `install_skillhub_skill`, and a late-bound host bridge for outbound `session/skillhub_search` / `session/skillhub_install` calls without Kernel policy changes.

- [x] Add main's search/install behavior tests and an adapter test proving the tools are present only when the host advertises matching capabilities.
- [x] Run the new tests and confirm failure because tools and the contributor bridge are absent.
- [x] Implement bounded non-sensitive queries, one-search-per-Run state, candidate identity binding, one-shot permission confirmation, host install delegation, and catalog refresh.
- [x] Register through session tool contributors, a Context contributor, and a late-bound host bridge; do not import SkillHub from `kernel/loop.py` or stable API contracts.
- [x] Re-run SkillHub and ACP adapter tests.

### Task 5: Packaged Midu marketplace Skills and manifest structure

**Files:**
- Create: `box_agent/skills/_midu_shared/` sources from main
- Create: `box_agent/skills/midu-pinyin/`, `midu-proofread-pinyin/`, `midu-proofread/`, `midu-st-convert/`, and `midu-writing/`
- Modify: `box_agent/skills/zhihu/SKILL.md`
- Modify: `box_agent/skills/zhihu/references/cli.md`
- Modify: `scripts/generate_skills_manifest.py`
- Regenerate: `box_agent/skills/_manifest.json`
- Create: `tests/test_midu_builtin_skills.py`
- Modify: `tests/test_generate_skills_manifest.py`
- Modify: `tests/test_zhihu_builtin_skill.py`

**Interfaces:**
- Produces: five packaged marketplace Skills with shared user-auth handling and an explicit 12-Skill builtin manifest allowlist.

- [x] Add matcher, auth-error, and generated-manifest tests from main; verify RED before adding Skill sources.
- [x] Add the Skill/shared source files with their exact command and error contracts.
- [x] Update the manifest generator and run it with the existing verification environment (`uv run --frozen` was blocked by the pre-existing `.venv/lib64` permission state).
- [x] Inspect the manifest diff for the intended core allowlist and run the focused Skill/manifest modules.

### Task 6: Completion, controlled presentation, and PPTX safety

**Files:**
- Modify: `box_agent/workflows/completion.py`
- Modify: `box_agent/workflows/delivery.py`
- Modify: `box_agent/workflows/controlled_presentation.py`
- Modify: `box_agent/workflows/presentation_checkpoint.py`
- Modify: `box_agent/workflows/presentation_routing.py`
- Create: `box_agent/tools/pptx_safety.py`
- Modify: `box_agent/tools/bash_tool.py`
- Modify: `box_agent/tools/file_tools.py`
- Modify: `box_agent/tools/jupyter_tool.py`
- Modify: `tests/test_completion_gate.py`
- Modify: `tests/test_external_skill_workflow.py`
- Modify: `tests/test_pptx_controlled_deck.py`
- Modify: `tests/test_jupyter_runtime_python.py` only if the current guard is missing

**Interfaces:**
- Produces: clause-scoped deliverable intent, dynamic-tool preservation, recoverable pending chunk checkpoints, Windows-safe image-status synchronization, and tool-level PPTX bypass blocking.

- [x] Add main's informational-intent, dynamic-tool, pending-write, invalid-repair, and command-token tests against Workflow Plugin entry points; verify the existing Jupyter bypass tests already cover the migrated contract.
- [x] Run each focused test selection and confirm expected RED failures.
- [x] Port completion parsing and presentation state transitions into Workflow Plugins; never reintroduce completion logic in `core.py`.
- [x] Add shared tool guards so bypass attempts fail even without a selected presentation workflow.
- [x] Re-run workflow/tool/PPTX tests.

### Task 7: PPTX Skill contract and visual assets

**Files:**
- Modify: `box_agent/skills/document-skills/pptx/` files changed on main after the fork, excluding generated/vendor payloads
- Modify: `tests/test_pptx_controlled_deck.py`
- Modify: `tests/test_pptx_controlled_deck.py` with user-facing contract cases
- Regenerate: `box_agent/skills/_manifest.json`

**Interfaces:**
- Produces: latest deterministic layouts, theme selection, six-card grid preservation, toolbar/export visibility, image-generation contract, and self-check behavior.

- [x] Port the behavioral tests first and run them to expose the branch's old Skill assets.
- [x] Apply the corresponding Skill, layout, CSS, and JavaScript source updates from main while retaining refactor Workflow ownership.
- [x] Run the Node-backed PPTX contract tests; regenerate the global Skill manifest and run its tests during final verification.

### Task 8: Recovery parity on the event-backed service

**Files:**
- Modify: `box_agent/services/kernel.py` only if a tested service guarantee is missing
- Modify: `box_agent/persistence/event_log.py`, `sessions.py`, `leases.py`, or `effects.py` only when the failing test identifies that owner
- Modify: `tests/test_kernel_service.py`
- Modify: `tests/test_persistence_recovery.py`
- Modify: `tests/test_durable_boundaries.py`
- Modify: `tests/test_context_memory_permission_replay.py`

**Interfaces:**
- Produces: one event-backed recovery source with immutable session workspace, committed-prefix validation, idempotent state replay, unknown-effect reconciliation, and no competing writer lease.

- [x] Translate applicable Session Log scenarios into service-level tests: second writer rejection, workspace mismatch, interrupted tool reconciliation, compacted context replay, and goal/plan/todo/skill restoration.
- [x] Run each new test and distinguish already-green superseding behavior from true missing guarantees; do not add duplicate code for already-covered behavior.
- [x] Implement only the immutable-workspace gap in `KernelAgentService` and re-run restart/replay/lease/effect suites.
- [x] Assert no runtime import of `box_agent.session_log` and no second append-only recovery file.

### Task 9: Configuration, prompts, documentation, and verification

**Files:**
- Modify: `box_agent/config.py`
- Modify: `box_agent/config/config-example.yaml`
- Modify: `box_agent/config/system_prompt.md`
- Modify: `README.md`, `README_CN.md`, and relevant `docs/` ownership/behavior guides
- Modify: `.understand-anything/knowledge-graph.json`, `meta.json`, and `fingerprints.json` only through `/understand`

**Interfaces:**
- Produces: documented defaults and behavior consistent with migrated source, plus a refreshed shared architecture index.

- [x] Port long-running limits and compact prompt wording only where source behavior/tests require them.
- [x] Update documents without copying main's removed-Workflow architecture claims.
- [x] Run focused LLM, Tool, ACP, Persistence, Workflow, Skill, PPTX, documentation, and architecture-boundary suites.
- [x] Run `pytest tests/ -q` when feasible, then `git diff --check` and inspect explicit staged paths.
- [ ] Run `/understand --full --language zh`; review and commit graph, meta, and fingerprints together, excluding overlays/caches/intermediate files.
- [ ] Report source/build/install/probe/restart/live-task boundaries separately; runtime packaging remains unverified while the pre-existing builder worktree changes are outside this migration.
