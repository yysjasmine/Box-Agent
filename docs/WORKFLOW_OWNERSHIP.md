# Workflow Ownership and Third-party Skills

Box-Agent may run built-in workflows and arbitrary installed Skills in the same
session workspace. Artifact filenames are therefore evidence inside a selected
workflow, not a safe way to select the workflow itself.

## Ownership selection

For each stable upstream session, ACP selects at most one recoverable workflow:

1. An explicitly invoked installed Skill uses the generic `external_skill`
   lifecycle unless it is bound to a trusted built-in workflow adapter.
2. A host-selected built-in provider may select its registered workflow.
3. The selected kind and bounded options are persisted by the runtime under
   `~/.box-agent/sessions/<session>/workflow-owner.json`.
4. A later ACP session resumes that owner before attempting filesystem-based
   legacy recovery.

The owner file is runtime state outside the user artifact root. Skill-authored
files and Skill metadata cannot register executable checkpoint code or grant
tool authority.

## Third-party Skill boundary

Unknown or user-installed Skills default to `external_skill`. The generic
policy may infer expected output globs from static Skill metadata, but it does
not run format-specific validators or finalizers. The Skill owns its authoring
and export sequence; Box-Agent owns budgets, tool permissions, resumable user
decisions, and artifact handoff.

A future format-specific integration must be implemented as a registered
runtime adapter with explicit artifact schema, checkpoint, validation,
finalization, and completion contracts. A file named `deck.json` is not an
adapter registration.

## Controlled-presentation legacy recovery

When no runtime owner or durable registered checkpoint exists, backward-
compatible controlled-presentation recovery is intentionally narrow:

- only canonical `output/outline.json` or `output/deck.json` is considered;
- an outline must contain non-empty structured slide narratives;
- a deck must identify `schema_version: 1`, a theme, and slides with
  `id + layout_id + props`;
- recursively discovered or foreign deck schemas are logged and ignored.

Once a controlled workflow has selected a nested artifact root through its own
checkpoint, generated validator/finalizer commands use absolute artifact paths
so `BOX_AGENT_OUTPUT_DIR` cannot redirect them to another root.

## Lifecycle

The owner is written when a recoverable workflow is selected, survives user
decision/input pauses and new ACP session handles, and is removed after the
delivery gate is satisfied or explicitly cancelled.

Primary proof lives in:

- `tests/test_workflow_owner_store.py`
- `tests/test_workflow_checkpoint_store.py`
- `tests/test_acp_kernel_adapter.py`
- `tests/test_acp_projection.py`
- `tests/test_completion_gate.py`
