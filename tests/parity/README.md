# Workflow parity fixtures

Fixtures in this directory preserve the behavior oracle used to promote every
complex workflow to the Agent Kernel. A workflow is marked `parity_passed`
only after its event stream, tool payloads, completion semantics, and recovery
state match the fixture. Keep fixtures deterministic:
timestamps and continuation limits are explicit, and no provider/network
calls belong here.

`migration_status.json` is the promotion record. Every workflow named in
`kernel_promotion_requires` has a fixture and `parity_passed` status;
`legacy_loop_retired` records that the pre-Kernel execution owner is gone.

`workflow_gap_matrix.json` records what the native policy already covers and
the remaining observable gaps for each workflow. It is a planning/evidence
map only; `migration_status.json` remains the machine-readable promotion
evidence.

`acp_host_gap_matrix.json` maps every retired ACP-monolith behavior category to
specific tests at its new owning boundary. The gate parses those test files and
fails if an evidence test disappears. Intentional semantic changes, such as
freezing the MCP tool graph for an active Run, must be stated in the matrix.
