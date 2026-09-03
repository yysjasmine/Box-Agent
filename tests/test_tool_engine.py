"""Contract tests for registry-backed Tool and Permission execution."""

from __future__ import annotations

import asyncio

import pytest

from box_agent.api import (
    ErrorCode,
    Message,
    PermissionDecision,
    PermissionRequest,
    ToolCallRequest,
    ToolCallResult,
    RunRequest,
)
from box_agent.plugins import PluginHost
from box_agent.permissions import DenyAllPermissionGateway
from box_agent.tools.engine import RegistryToolEngine
from box_agent.tools.workspace import SessionScopedToolEngineFactory
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.file_tools import WriteTool
from box_agent.tools.bash_tool import BashTool
from box_agent.tools.permissions import (
    CapabilityPolicy,
    GrantStore,
    PermissionDecision as CapabilityPermissionDecision,
    PermissionEngine,
)
from box_agent.persistence import SQLiteEffectLedger


class EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Echo text"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    async def execute(self, *, text: str) -> ToolResult:
        return ToolResult(success=True, content=text)


class PermissionTool(EchoTool):
    @property
    def name(self) -> str:
        return "danger"

    async def execute(self, *, text: str) -> ToolResult:
        return ToolResult(
            success=False,
            permission_request={"scope": "safety", "reason": "test"},
        )


class DuckTypedTool:
    async def invoke(self, arguments, *, context=None):
        return ToolResult(success=True, content=arguments["text"])


class SyncDuckTypedTool:
    def invoke(self, arguments, *, context=None):
        return ToolResult(success=True, content=arguments["text"])


class RequestExecutor:
    """Minimal implementation of the public ToolExecutor port."""

    name = "request_executor"
    description = "request-style executor"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, call, *, context=None):
        assert call.tool_name == self.name
        assert context is not None
        return ToolCallResult(
            call_id=call.call_id,
            status="succeeded",
            content="request-style output",
        )


class ApprovalAwareTool(PermissionTool):
    def __init__(self) -> None:
        self.approved = False

    def approve_permission_request(self, permission_request: dict) -> None:
        self.approved = True

    async def execute(self, *, text: str) -> ToolResult:
        if not self.approved:
            return await super().execute(text=text)
        return ToolResult(success=True, content=f"approved:{text}")


class ApprovingGateway:
    async def decide(self, request: dict) -> PermissionDecision:
        return PermissionDecision(granted=True, reason="test approval")


class BrokenGateway:
    async def decide(self, request):
        raise RuntimeError("approval service down")


@pytest.mark.asyncio
async def test_prepare_call_preserves_plugin_registration_provenance() -> None:
    host = PluginHost()
    host.registries["tools"].register(
        "echo",
        EchoTool(),
        source="mcp.server:research",
        version="2.0.0",
    )
    engine = RegistryToolEngine(host.registries["tools"])

    prepared, early_result = await engine.prepare_call(
        ToolCallRequest(
            call_id="mcp-call",
            tool_name="echo",
            arguments={"text": "hello"},
        )
    )

    assert early_result is None
    assert prepared.metadata["registration_source"] == "mcp.server:research"
    assert prepared.metadata["registration_version"] == "2.0.0"
    assert prepared.metadata["mcp_server"] == "research"


@pytest.mark.asyncio
async def test_tool_hooks_intercept_after_permission_and_before_executor() -> None:
    host = PluginHost()
    tool = EchoTool()
    host.registries["tools"].register("echo", tool, source="test")
    order: list[str] = []

    class Hook:
        async def before_tool(self, call, context=None):
            order.append("before")
            return ToolCallRequest(
                call_id=call.call_id,
                tool_name=call.tool_name,
                arguments={"text": "rewritten"},
                metadata=call.metadata,
            )

        async def after_tool(self, call, result, context=None):
            order.append("after")
            return result

    result = await RegistryToolEngine(
        host.registries["tools"], hooks=[Hook()]
    ).execute(
        ToolCallRequest(call_id="hook-call", tool_name="echo", arguments={"text": "raw"})
    )

    assert result.status == "succeeded"
    assert result.content == "rewritten"
    assert order == ["before", "after"]


@pytest.mark.asyncio
async def test_legacy_hook_method_names_remain_supported() -> None:
    host = PluginHost()
    host.registries["tools"].register("echo", EchoTool(), source="test")

    class Hook:
        async def on_tool_start(self, *, tool_call_id, tool_name, arguments):
            del tool_call_id, tool_name
            return {"text": "legacy"}

        async def on_tool_result(self, *, tool_call_id, tool_name, success, content, error):
            del tool_call_id, tool_name, success, error
            return content + " hook", None

    result = await RegistryToolEngine(
        host.registries["tools"], hooks=[Hook()]
    ).execute(
        ToolCallRequest(call_id="legacy-hook", tool_name="echo", arguments={"text": "raw"})
    )
    assert result.content == "legacy hook"


class PreflightPermissionTool(EchoTool):
    """Tool whose permission intent is available without executing it."""

    def __init__(self) -> None:
        self.execute_calls = 0
        self.preflight_calls = 0
        self.approved = False

    @property
    def name(self) -> str:
        return "preflight_danger"

    async def preflight(self, arguments, *, context=None):
        del arguments, context
        self.preflight_calls += 1
        if self.approved:
            return None
        return ToolResult(
            success=False,
            permission_request={"scope": "safety", "reason": "preflight test"},
        )

    def approve_permission_request(self, permission_request: dict) -> None:
        del permission_request
        self.approved = True

    async def execute(self, *, text: str) -> ToolResult:
        self.execute_calls += 1
        return ToolResult(success=True, content=f"executed:{text}")


class OrderingGateway:
    def __init__(self, tool: PreflightPermissionTool) -> None:
        self.tool = tool
        self.execute_calls_at_decision: list[int] = []

    async def decide(self, request: PermissionRequest) -> PermissionDecision:
        del request
        self.execute_calls_at_decision.append(self.tool.execute_calls)
        return PermissionDecision(granted=True, reason="preflight approved")


class RecordingWriteTool(WriteTool):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executor_entered = False

    async def execute(self, *args, **kwargs):
        self.executor_entered = True
        return await super().execute(*args, **kwargs)


class RecordingBashTool(BashTool):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executor_entered = False

    async def execute(self, *args, **kwargs):
        self.executor_entered = True
        return await super().execute(*args, **kwargs)


@pytest.mark.asyncio
async def test_registry_tool_engine_executes_registered_tool() -> None:
    host = PluginHost()
    host.registries["tools"].register("echo", EchoTool(), source="test")
    engine = RegistryToolEngine(host.registries["tools"])

    result = await engine.execute(
        ToolCallRequest(call_id="call-1", tool_name="echo", arguments={"text": "hello"})
    )

    assert result.status == "succeeded"
    assert result.content == "hello"


@pytest.mark.asyncio
async def test_unknown_tool_fails_closed_with_stable_error() -> None:
    engine = RegistryToolEngine(PluginHost().registries["tools"])

    result = await engine.execute(
        ToolCallRequest(call_id="call-1", tool_name="missing", arguments={})
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "TOOL_NOT_FOUND"


@pytest.mark.asyncio
async def test_session_scoped_tool_factory_binds_workspace_without_cross_session_leakage() -> None:
    builds: list[tuple[str, str]] = []

    class WorkspaceProbe(EchoTool):
        def __init__(self, workspace: str) -> None:
            self.workspace = workspace

        @property
        def name(self) -> str:
            return "workspace_probe"

        async def execute(self, *, text: str) -> ToolResult:
            return ToolResult(success=True, content=f"{self.workspace}:{text}")

    def build_workspace_tools(request):
        workspace = str(request.metadata["workspace_dir"])
        builds.append((request.session_id, workspace))
        return (WorkspaceProbe(workspace),)

    factory = SessionScopedToolEngineFactory(
        base_tools_provider=lambda: (EchoTool(),),
        workspace_tools_builder=build_workspace_tools,
    )
    request_a = RunRequest(
        request_id="request-a",
        session_id="session-a",
        turn_id="turn-a",
        user_input=Message.user("a"),
        metadata={"workspace_dir": "D:/workspace/a"},
    )
    request_b = RunRequest(
        request_id="request-b",
        session_id="session-b",
        turn_id="turn-b",
        user_input=Message.user("b"),
        metadata={"workspace_dir": "D:/workspace/b"},
    )

    engine_a = factory.for_run(request_a)
    engine_a_again = factory.for_run(request_a)
    engine_b = factory.for_run(request_b)
    result_a = await engine_a.execute(
        ToolCallRequest(
            call_id="call-a",
            tool_name="workspace_probe",
            arguments={"text": "ok"},
        )
    )
    result_b = await engine_b.execute(
        ToolCallRequest(
            call_id="call-b",
            tool_name="workspace_probe",
            arguments={"text": "ok"},
        )
    )

    assert result_a.content == "D:/workspace/a:ok"
    assert result_b.content == "D:/workspace/b:ok"
    assert engine_a is not engine_a_again
    assert builds == [
        ("session-a", "D:/workspace/a"),
        ("session-b", "D:/workspace/b"),
    ]

    changed_workspace = RunRequest(
        request_id="request-a-2",
        session_id="session-a",
        turn_id="turn-a-2",
        user_input=Message.user("a2"),
        metadata={"workspace_dir": "D:/workspace/other"},
    )
    with pytest.raises(ValueError, match="workspace is immutable"):
        factory.for_run(changed_workspace)


@pytest.mark.asyncio
async def test_session_tool_contributor_adds_one_session_local_tool_generation() -> None:
    contributions: list[str] = []

    class Contributor:
        def provide_tools(self, request):
            contributions.append(request.session_id)
            return (EchoTool(),)

    factory = SessionScopedToolEngineFactory(
        base_tools_provider=lambda: (),
        workspace_tools_builder=lambda request: (),
        session_tool_contributors_provider=lambda: (Contributor(),),
    )
    request = RunRequest(
        request_id="request-a",
        session_id="session-a",
        turn_id="turn-a",
        user_input=Message.user("hello"),
        metadata={"workspace_dir": "D:/workspace/a"},
    )

    first = factory.for_run(request)
    second = factory.for_run(request)
    result = await second.execute(
        ToolCallRequest(
            call_id="call-a",
            tool_name="echo",
            arguments={"text": "from contributor"},
        )
    )

    assert first.schemas() == second.schemas()
    assert result.content == "from contributor"
    assert contributions == ["session-a"]


@pytest.mark.asyncio
async def test_permission_request_does_not_execute_without_gateway() -> None:
    host = PluginHost()
    host.registries["tools"].register("danger", PermissionTool(), source="test")
    engine = RegistryToolEngine(host.registries["tools"])

    result = await engine.execute(
        ToolCallRequest(call_id="call-1", tool_name="danger", arguments={"text": "x"})
    )

    assert result.status == "permission_required"
    assert result.permission_request == {"scope": "safety", "reason": "test"}


@pytest.mark.asyncio
async def test_third_party_duck_typed_tool_can_be_registered() -> None:
    host = PluginHost()
    host.registries["tools"].register("third_party", DuckTypedTool(), source="vendor")
    engine = RegistryToolEngine(host.registries["tools"])

    result = await engine.execute(
        ToolCallRequest(
            call_id="call-1",
            tool_name="third_party",
            arguments={"text": "vendor output"},
        )
    )

    assert result.status == "succeeded"
    assert result.content == "vendor output"


@pytest.mark.asyncio
async def test_tool_descriptors_can_be_registered_separately_from_executors() -> None:
    host = PluginHost()
    host.registries["tools.executors"].register(
        "echo", EchoTool(), source="vendor.executor"
    )
    host.registries["tools.descriptors"].register(
        "echo",
        {
            "name": "echo",
            "description": "vendor descriptor",
            "input_schema": {"type": "object", "properties": {}},
        },
        source="vendor.descriptor",
    )
    engine = RegistryToolEngine(
        host.registries["tools.executors"],
        descriptor_registry=host.registries["tools.descriptors"],
    )

    assert engine.schemas() == (
        {
            "name": "echo",
            "description": "vendor descriptor",
            "input_schema": {"type": "object", "properties": {}},
        },
    )
    result = await engine.execute(
        ToolCallRequest(call_id="call-1", tool_name="echo", arguments={"text": "ok"})
    )
    assert result.status == "succeeded"


@pytest.mark.asyncio
async def test_sync_third_party_tool_can_be_registered() -> None:
    host = PluginHost()
    host.registries["tools"].register("sync", SyncDuckTypedTool(), source="vendor")
    result = await RegistryToolEngine(host.registries["tools"]).execute(
        ToolCallRequest(call_id="call-1", tool_name="sync", arguments={"text": "ok"})
    )

    assert result.status == "succeeded"
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_request_style_tool_executor_can_be_registered() -> None:
    host = PluginHost()
    executor = RequestExecutor()
    host.registries["tools.executors"].register(
        executor.name, executor, source="vendor.executor"
    )
    engine = RegistryToolEngine(host.registries["tools.executors"])

    result = await engine.execute(
        ToolCallRequest(call_id="call-request-style", tool_name=executor.name),
        context={"request": "context"},
    )

    assert result.status == "succeeded"
    assert result.content == "request-style output"


@pytest.mark.asyncio
async def test_tool_engine_resolves_model_name_when_registry_key_is_namespaced() -> None:
    host = PluginHost()
    host.registries["tools.executors"].register(
        "vendor.search.v1", EchoTool(), source="vendor"
    )
    result = await RegistryToolEngine(host.registries["tools.executors"]).execute(
        ToolCallRequest(
            call_id="call-namespaced",
            tool_name="echo",
            arguments={"text": "resolved"},
        )
    )

    assert result.status == "succeeded"
    assert result.content == "resolved"


@pytest.mark.asyncio
async def test_permission_gateway_approves_then_retries_tool() -> None:
    host = PluginHost()
    tool = ApprovalAwareTool()
    host.registries["tools"].register("danger", tool, source="test")
    engine = RegistryToolEngine(
        host.registries["tools"], permission_gateway=ApprovingGateway()
    )

    result = await engine.execute(
        ToolCallRequest(call_id="call-1", tool_name="danger", arguments={"text": "x"})
    )

    assert result.status == "succeeded"
    assert result.content == "approved:x"


def test_permission_preflight_runs_before_executor() -> None:
    async def scenario() -> None:
        host = PluginHost()
        tool = PreflightPermissionTool()
        gateway = OrderingGateway(tool)
        host.registries["tools"].register(
            "preflight_danger", tool, source="test"
        )
        result = await RegistryToolEngine(
            host.registries["tools"], permission_gateway=gateway
        ).execute(
            ToolCallRequest(
                call_id="call-preflight",
                tool_name="preflight_danger",
                arguments={"text": "x"},
            )
        )

        assert result.status == "succeeded"
        assert result.content == "executed:x"
        assert tool.preflight_calls == 1
        assert tool.execute_calls == 1
        assert gateway.execute_calls_at_decision == [0]

    asyncio.run(scenario())


def test_invalid_arguments_skip_permission_preflight() -> None:
    async def scenario() -> None:
        host = PluginHost()
        tool = PreflightPermissionTool()
        host.registries["tools"].register(
            "preflight_danger", tool, source="test"
        )
        result = await RegistryToolEngine(
            host.registries["tools"], permission_gateway=OrderingGateway(tool)
        ).execute(
            ToolCallRequest(
                call_id="call-invalid",
                tool_name="preflight_danger",
                arguments={},
            )
        )

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "INVALID_TOOL_ARGUMENTS"
        assert tool.preflight_calls == 0
        assert tool.execute_calls == 0

    asyncio.run(scenario())


def test_builtin_write_permission_is_checked_before_executor(tmp_path) -> None:
    async def scenario() -> None:
        class DenyingCapability:
            def check(self, capability, resource, tool_name=None):
                return CapabilityPermissionDecision(
                    allowed=False,
                    reason="write needs approval",
                    permission_request={
                        "type": "permission_request",
                        "scope": "filesystem",
                        "requested_scope": "user_home",
                        "path": resource["path"],
                        "reason": "outside workspace",
                    },
                )

        tool = RecordingWriteTool(
            workspace_dir=str(tmp_path),
            relative_root_dir=str(tmp_path),
            permission_engine=DenyingCapability(),
        )
        host = PluginHost()
        host.registries["tools"].register("write_file", tool, source="test")
        result = await RegistryToolEngine(
            host.registries["tools"], permission_gateway=DenyAllPermissionGateway()
        ).execute(
            ToolCallRequest(
                call_id="call-write",
                tool_name="write_file",
                arguments={"path": "artifact.txt", "content": "nope"},
            )
        )

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "PERMISSION_DENIED"
        assert tool.executor_entered is False
        assert not (tmp_path / "artifact.txt").exists()

    asyncio.run(scenario())


def test_builtin_bash_permission_is_checked_before_executor(tmp_path) -> None:
    async def scenario() -> None:
        class DenyingCapability:
            def check(self, capability, resource, tool_name=None):
                return CapabilityPermissionDecision(
                    allowed=False,
                    reason="shell path needs approval",
                    permission_request={
                        "type": "permission_request",
                        "scope": "filesystem",
                        "requested_scope": "user_home",
                        "path": resource["path"],
                        "reason": "outside workspace",
                    },
                )

        tool = RecordingBashTool(
            workspace_dir=str(tmp_path),
            scope_root_dir=str(tmp_path),
            permission_engine=DenyingCapability(),
        )
        host = PluginHost()
        host.registries["tools"].register("bash", tool, source="test")
        result = await RegistryToolEngine(
            host.registries["tools"], permission_gateway=DenyAllPermissionGateway()
        ).execute(
            ToolCallRequest(
                call_id="call-bash",
                tool_name="bash",
                arguments={"command": f"cat {tmp_path.parent / 'outside.txt'}"},
            )
        )

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "PERMISSION_DENIED"
        assert tool.executor_entered is False

    asyncio.run(scenario())


def test_invalid_tool_call_does_not_prepare_effect_ledger(tmp_path) -> None:
    class InvalidTool(Tool):
        @property
        def name(self) -> str:
            return "invalid"

        @property
        def description(self) -> str:
            return "invalid"

        @property
        def parameters(self) -> dict:
            return {
                "type": "object",
                "properties": {"required": {"type": "string"}},
                "required": ["required"],
            }

        async def execute(self, **arguments):
            raise AssertionError("invalid arguments must never execute")

    async def scenario() -> None:
        ledger = SQLiteEffectLedger(tmp_path / "invalid-effects.sqlite3")
        host = PluginHost()
        host.registries["tools"].register("invalid", InvalidTool(), source="test")
        engine = RegistryToolEngine(host.registries["tools"], effect_ledger=ledger)
        result = await engine.execute(
            ToolCallRequest(
                call_id="invalid-call",
                tool_name="invalid",
                arguments={},
                metadata={
                    "run_id": "run-invalid",
                    "effect_id": "effect-invalid",
                    "idempotency_key": "idem-invalid",
                },
            )
        )

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "INVALID_TOOL_ARGUMENTS"
        assert await ledger.reconcile("effect-invalid") is None
        ledger.close()

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_default_permission_gateway_denies_closed() -> None:
    decision = await DenyAllPermissionGateway().decide(
        {"scope": "safety", "reason": "no approval"}
    )

    assert decision.granted is False


@pytest.mark.asyncio
async def test_permission_gateway_failure_is_fail_closed() -> None:
    host = PluginHost()
    host.registries["tools"].register("danger", PermissionTool(), source="test")
    engine = RegistryToolEngine(host.registries["tools"], permission_gateway=BrokenGateway())

    result = await engine.execute(
        ToolCallRequest(call_id="call-1", tool_name="danger", arguments={"text": "x"})
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "PERMISSION_TIMEOUT"


@pytest.mark.asyncio
async def test_effect_ledger_reuses_completed_side_effect_without_reexecution(tmp_path) -> None:
    class SideEffectTool:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke(self, arguments, *, context=None):
            self.calls += 1
            return ToolResult(success=True, content=f"value:{self.calls}")

    tool = SideEffectTool()
    host = PluginHost()
    host.registries["tools"].register("side_effect", tool, source="test")
    engine = RegistryToolEngine(
        host.registries["tools"],
        effect_ledger=SQLiteEffectLedger(tmp_path / "effects.sqlite3"),
    )
    request = ToolCallRequest(
        call_id="call-1",
        tool_name="side_effect",
        arguments={},
        metadata={"run_id": "run-1", "effect_id": "effect-1", "idempotency_key": "idem-1"},
    )

    first = await engine.execute(request)
    second = await engine.execute(request)

    assert first.content == "value:1"
    assert second.content == "value:1"
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_effect_ledger_blocks_unresolved_reexecution(tmp_path) -> None:
    ledger = SQLiteEffectLedger(tmp_path / "effects.sqlite3")
    await ledger.prepare(
        effect_id="effect-1",
        run_id="run-1",
        idempotency_key="idem-1",
        request_digest="digest",
    )
    await ledger.complete(effect_id="effect-1", status="unknown")
    host = PluginHost()
    host.registries["tools"].register("side_effect", SyncDuckTypedTool(), source="test")
    engine = RegistryToolEngine(host.registries["tools"], effect_ledger=ledger)

    result = await engine.execute(
        ToolCallRequest(
            call_id="call-1",
            tool_name="side_effect",
            arguments={"text": "x"},
            metadata={
                "run_id": "run-1",
                "effect_id": "effect-1",
                "idempotency_key": "idem-1",
                "request_digest": "digest",
            },
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "EFFECT_REQUIRES_RECONCILIATION"


@pytest.mark.asyncio
async def test_tool_engine_runs_each_tool_cleanup_once_at_run_boundary() -> None:
    class CleanupTool(EchoTool):
        def __init__(self) -> None:
            self.cleanup_calls = 0

        def end_run(self):
            self.cleanup_calls += 1

    tool = CleanupTool()
    host = PluginHost()
    host.registries["tools"].register("echo", tool, source="test")
    host.registries["tools"].register("echo_alias", tool, source="test")
    engine = RegistryToolEngine(host.registries["tools"])

    await engine.end_run(
        session_id="session-1", run_id="run-1", metadata={"key": "value"}
    )

    assert tool.cleanup_calls == 1


@pytest.mark.asyncio
async def test_tool_engine_applies_host_filesystem_grant_before_executor(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    grants = GrantStore()
    permissions = PermissionEngine(
        CapabilityPolicy(
            filesystem_scope="session_workspace",
            session_workspace_root=str(workspace),
        ),
        workspace,
        grant_store=grants,
    )
    tool = WriteTool(
        workspace_dir=str(workspace),
        relative_root_dir=str(workspace),
        allow_full_access=False,
        permission_engine=permissions,
    )

    class Gateway:
        async def decide(self, request):
            return PermissionDecision(
                True, "approved", metadata={"grant_scope": "prompt"}
            )

    host = PluginHost()
    host.registries["tools"].register(tool.name, tool, source="test")
    engine = RegistryToolEngine(host.registries["tools"], permission_gateway=Gateway())
    target = outside / "result.txt"

    result = await engine.execute(
        ToolCallRequest(
            call_id="write-outside",
            tool_name=tool.name,
            arguments={"path": str(target), "content": "approved"},
            metadata={"session_id": "session-1", "run_id": "run-1"},
        )
    )

    assert result.status == "succeeded"
    assert target.read_text(encoding="utf-8") == "approved"


def test_prompt_grants_rotate_by_run_identity() -> None:
    store = GrantStore()
    store.begin_run("run-1")
    store.add_grant("memory", "import", "prompt")
    assert store.has_grant("memory", "import")
    store.begin_run("run-1")
    assert store.has_grant("memory", "import")
    store.begin_run("run-2")
    assert not store.has_grant("memory", "import")
