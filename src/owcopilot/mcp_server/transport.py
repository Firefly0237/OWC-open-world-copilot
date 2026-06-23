"""Optional MCP transport factory.

The core tool handlers live in `tools.py` and have no MCP SDK dependency. This module is the thin
boundary that binds those handlers to an installed MCP transport when available.
"""

from __future__ import annotations

from typing import Any

from .tools import (
    ask_lore,
    audit_project,
    build_context_pack,
    export_project,
    impact_of,
    list_issues,
    propose_fix,
    quality_harness,
)


class MCPTransportUnavailable(RuntimeError):
    """Raised when the optional MCP SDK is not installed."""


def create_mcp_server(name: str = "owcopilot") -> Any:
    """Create a FastMCP server and register the owcopilot tools.

    Write-style actions (patch apply, review accept) are intentionally not registered: the
    human-in-the-loop write path lives only in the CLI/UI.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise MCPTransportUnavailable(
            "MCP transport is optional. Install the MCP SDK, for example with "
            "`pip install -e .[mcp]`, to run the server transport."
        ) from e

    server = FastMCP(name)
    server.tool()(audit_project)
    server.tool()(list_issues)
    server.tool()(build_context_pack)
    server.tool()(ask_lore)
    server.tool()(impact_of)
    server.tool()(propose_fix)
    server.tool()(quality_harness)
    server.tool()(export_project)
    return server
