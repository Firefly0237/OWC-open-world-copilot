from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from owcopilot.mcp_server import MCPTransportUnavailable, create_mcp_server


def test_create_mcp_server_reports_missing_optional_sdk(monkeypatch) -> None:
    real_import = __import__

    def blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("mcp"):
            raise ImportError("blocked mcp import for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", blocked_import)

    with pytest.raises(MCPTransportUnavailable, match="MCP transport is optional"):
        create_mcp_server()


def test_create_mcp_server_registers_tools_when_fastmcp_is_available(monkeypatch) -> None:
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")

    class FakeFastMCP:
        def __init__(self, name: str) -> None:
            self.name = name
            self.tools: list[str] = []

        def tool(self):
            def decorator(func):
                self.tools.append(func.__name__)
                return func

            return decorator

    fastmcp_module.FastMCP = FakeFastMCP
    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", types.ModuleType("mcp.server"))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)

    server = create_mcp_server("owcopilot-test")

    assert server.name == "owcopilot-test"
    assert server.tools == [
        "audit_project",
        "list_issues",
        "build_context_pack",
        "ask_lore",
        "impact_of",
        "propose_fix",
        "export_project",
    ]
    # The human write path must never be exposed over MCP.
    assert "apply" not in " ".join(server.tools)
