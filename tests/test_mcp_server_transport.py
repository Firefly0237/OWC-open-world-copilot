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
            self.annotations: dict[str, Any] = {}

        def tool(self, **kwargs: Any):
            def decorator(func):
                self.tools.append(func.__name__)
                self.annotations[func.__name__] = kwargs.get("annotations")
                return func

            return decorator

    class FakeToolAnnotations:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    types_module = types.ModuleType("mcp.types")
    types_module.ToolAnnotations = FakeToolAnnotations
    fastmcp_module.FastMCP = FakeFastMCP
    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    monkeypatch.setitem(sys.modules, "mcp.server", types.ModuleType("mcp.server"))
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", fastmcp_module)
    monkeypatch.setitem(sys.modules, "mcp.types", types_module)

    server = create_mcp_server("owcopilot-test")

    assert server.name == "owcopilot-test"
    assert server.tools == [
        "audit_project",
        "list_issues",
        "build_context_pack",
        "ask_lore",
        "impact_of",
        "propose_fix",
        "quality_harness",
        "export_project",
    ]
    # The human write path must never be exposed over MCP.
    assert "apply" not in " ".join(server.tools)
    # Every tool carries annotations mapped from TOOL_SAFETY; read vs write hint is correct.
    assert server.annotations["list_issues"].readOnlyHint is True
    assert server.annotations["ask_lore"].readOnlyHint is True
    assert server.annotations["ask_lore"].openWorldHint is True
    assert server.annotations["audit_project"].readOnlyHint is False
    assert server.annotations["export_project"].readOnlyHint is False
    assert server.annotations["export_project"].destructiveHint is False
    assert server.annotations["export_project"].idempotentHint is False
