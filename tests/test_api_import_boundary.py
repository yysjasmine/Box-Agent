"""Import boundaries for the stable public protocol package."""

from __future__ import annotations

import subprocess
import sys


def test_api_import_does_not_load_application_adapters() -> None:
    code = (
        "import sys; import box_agent.api; "
        "assert 'box_agent.acp' not in sys.modules; "
        "assert 'box_agent.cli' not in sys.modules; "
        "assert 'box_agent.mcp_servers' not in sys.modules; "
        "assert 'box_agent.mcp_loader' not in sys.modules; "
        "assert 'box_agent.tools.setup' not in sys.modules; "
        "assert 'box_agent.context.in_memory' not in sys.modules; "
        "assert 'box_agent.memory_engine.in_memory' not in sys.modules; "
        "assert 'mcp' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_kernel_import_does_not_load_application_adapters() -> None:
    code = (
        "import sys; import box_agent.kernel; "
        "assert 'box_agent.core' not in sys.modules; "
        "assert 'box_agent.acp' not in sys.modules; "
        "assert 'box_agent.cli' not in sys.modules; "
        "assert 'box_agent.mcp_servers' not in sys.modules; "
        "assert 'box_agent.mcp_loader' not in sys.modules; "
        "assert 'box_agent.tools.setup' not in sys.modules; "
        "assert 'box_agent.context.in_memory' not in sys.modules; "
        "assert 'box_agent.memory_engine.in_memory' not in sys.modules; "
        "assert 'mcp' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_tool_contract_and_setup_imports_do_not_require_mcp_sdk() -> None:
    code = (
        "import sys; import box_agent.tools; "
        "from box_agent.tools import ReadTool, WriteTool; "
        "from box_agent.tools.setup import add_workspace_tools; "
        "assert ReadTool and WriteTool and add_workspace_tools; "
        "assert 'box_agent.tools.mcp_loader' not in sys.modules; "
        "assert 'mcp' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_llm_facade_does_not_load_provider_sdks_before_client_creation() -> None:
    code = (
        "import sys; import box_agent.llm; "
        "from box_agent.llm import LLMClient, SessionBoundLLM; "
        "assert LLMClient and SessionBoundLLM; "
        "assert 'anthropic' not in sys.modules; "
        "assert 'openai' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
