# Host adapters

Adapters translate protocols; they never execute Agent policy.

```text
box_agent/adapters/
├── acp/                  ACP callback facade
├── cli/                  terminal rendering, permission and memory negotiation
├── acp_kernel.py         ACP payload → stable RunRequest
├── acp_metadata.py       metadata normalization and secret filtering
├── acp_projection.py     stable AgentEvent → ACP updates
├── capabilities.py       existing providers → stable capability ports
├── plugin_host.py        built-in PluginHost composition
├── hosts.py              ACP/CLI/SDK service adapters
├── service.py            host payload conversion
└── extensions.py         external control-route registration
```

All hosts call `KernelAgentService`. `run.events` and `run.wait` attach to a
run without taking ownership; only explicit `resume`/control paths acquire a
worker lease. ACP and non-interactive CLI use the same Kernel and workflow
registries. The retired pre-Kernel loop has no runtime selector or fallback.

The root `box_agent.cli_permissions` and `box_agent.cli_memory_proposal`
modules are stable import shims over `adapters/cli/`. Root `Agent`, `Core`, and
`Runtime` modules likewise preserve old imports but execute through the
Kernel-backed compatibility adapter.

Third-party hosts should compose capabilities through `PluginHost` and create
the service with `KernelAgentService.from_plugin_host(host)`. They should not
construct transport-specific loops.
