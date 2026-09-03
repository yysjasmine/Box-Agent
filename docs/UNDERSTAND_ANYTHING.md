# Understand Anything Code Map

Box-Agent uses Understand Anything as a versioned architecture index for code
discovery, ownership tracing, dependency inspection, and guided onboarding. The
graph accelerates navigation, but the source tree, focused tests, logs, and
runtime probes remain the source of truth.

## Install and repository directory

In Claude Code, install the official plugin with:

```text
/plugin marketplace add Egonex-AI/Understand-Anything
/plugin install understand-anything@understand-anything
```

Reload the client if the slash commands are not available immediately. Current
Understand Anything releases use `.ua/` for a newly initialized repository,
but continue to use the legacy `.understand-anything/` directory when it is
already present. Box-Agent intentionally keeps that existing directory so
contributors and CI share one versioned baseline. Do not create a parallel
`.ua/` baseline or rename the directory as part of an ordinary refresh.

## Repository scope

The shared scope is defined by
[`../.understand-anything/.understandignore`](../.understand-anything/.understandignore).
The current configuration includes the core runtime, ACP/CLI/SDK adapters,
tools, configuration, documentation, and the controlled PPTX compiler under
`box_agent/skills/document-skills/pptx/`. Other bundled skill assets, tests,
workspaces, virtual environments, generated output, and vendored PPTX runtime
payloads remain excluded so the graph stays focused on product architecture.

Only these shared files belong in Git:

- `.understand-anything/.understandignore`
- `.understand-anything/config.json`
- [`.understand-anything/knowledge-graph.json`](../.understand-anything/knowledge-graph.json)
- `.understand-anything/meta.json`
- `.understand-anything/fingerprints.json`

The versioned `knowledge-graph.json` lets contributors explore the same
architecture snapshot immediately after cloning the repository. `meta.json`
provides its refresh timestamp, analyzed source commit, format version, and file
count. `fingerprints.json` gives the plugin a structural comparison baseline so
later runs can distinguish unchanged, cosmetic, and structural changes without
reanalyzing the entire repository.

Keep these three generated files together as one refresh baseline. Do not
hand-edit them or update only `meta.json`: the plugin requires fingerprints to
support reliable incremental updates. Do not commit `last-run-summary.json`,
intermediate files, trash directories, dashboard tokens, or caches. Those
files remain local refresh state and are ignored by `.gitignore`.

## Explore the shared graph

After cloning the repository, run `/understand-dashboard` from a client with the
Understand Anything plugin. The dashboard reads the committed
`.understand-anything/knowledge-graph.json`; open the tokenized URL printed by
the plugin. The JSON file can also be inspected directly by other graph-aware
tools without running a refresh first.

For a quick freshness check, compare `meta.json.gitCommitHash` with the graph's
`project.gitCommitHash`, then inspect source changes after that baseline. When
running `/understand`, the plugin uses `fingerprints.json` automatically; it is
not intended to be queried for architecture answers.

## Refresh workflow

1. Review `.understand-anything/.understandignore` before changing graph scope.
2. Run `/understand --full --language zh` from a client with the Understand
   Anything plugin when the graph is missing, materially stale, or the scope has
   changed. Use `/understand` for a normal incremental refresh.
3. Confirm that validation reports no critical issues and that every analyzed
   file-level node belongs to exactly one architecture layer.
4. After validation succeeds, update `knowledge-graph.json`, `meta.json`, and
   `fingerprints.json` together in the same reviewable change. Do not hand-edit
   these generated files.
5. Preserve `intermediate/scan-result.json` locally so incremental runs can
   reuse the deterministic inventory. Keep it—and all other intermediate
   files, summaries, trash, dashboard tokens, and caches—out of Git; they are
   refresh state, not part of the shared baseline.
6. If the dashboard is launched, open the tokenized URL emitted by the plugin;
   the bare local server URL is not sufficient.

Refresh the graph after changes to entry points, subsystem boundaries, shared
tools, ACP contracts, runtime packaging, or documentation that should appear in
the guided tour. Small source changes that do not affect graph structure or the
guided tour do not require an immediate graph refresh.

## Verification and review

Use the graph to identify likely files and relationships, then verify important
conclusions with direct file reads, `rg`, focused `uv run pytest` commands, logs,
or runtime probes. Compare the graph's analyzed baseline with later source
changes; describe the graph as stale when those changes affect its architecture
or guided tour.

The graph's `project.gitCommitHash` identifies the analyzed source baseline. A
dedicated graph commit normally follows that baseline commit, so this hash can
legitimately point to the graph commit's parent. For graph updates, include
validation statistics and remaining warnings in the pull request's Task, Proof,
and Risk sections. If shared scope or language settings change, include the
relevant configuration diff and explain the intended coverage.
