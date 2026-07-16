# 可选的 MCP 传输工厂。
"""Optional MCP transport factory.

The core tool handlers live in `tools.py` and have no MCP SDK dependency. This module is the thin
boundary that binds those handlers to an installed MCP transport when available.
"""

from __future__ import annotations

from typing import Any

from .tools import (
    TOOL_SAFETY,
    ToolSafety,
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


def _annotations(safety: ToolSafety, tool_annotations_cls: Any) -> Any:
    """Map our SDK-free :class:`ToolSafety` to ``mcp.types.ToolAnnotations``.

    ``destructiveHint``/``idempotentHint`` are only meaningful when ``readOnlyHint`` is false, so
    they are omitted for read-only tools (MCP treats them as no-ops there)."""
    if safety.read_only:
        return tool_annotations_cls(readOnlyHint=True, openWorldHint=safety.open_world)
    return tool_annotations_cls(
        readOnlyHint=False,
        destructiveHint=safety.destructive,
        idempotentHint=safety.idempotent,
        openWorldHint=safety.open_world,
    )


def create_mcp_server(name: str = "owcopilot") -> Any:
    """Create a FastMCP server and register the owcopilot tools, each carrying MCP ToolAnnotations
    (readOnly/destructive/idempotent/openWorld) derived from :data:`TOOL_SAFETY` so external clients
    can do risk-based approval and parallelism.

    Write-style actions (patch apply, review accept) are intentionally not registered: the
    human-in-the-loop write path lives only in the CLI/UI.
    """
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError as e:
        raise MCPTransportUnavailable(
            "MCP transport is optional. Install the MCP SDK, for example with "
            "`pip install -e .[mcp]`, to run the server transport."
        ) from e

    server = FastMCP(name)
    for fn in (
        audit_project,
        list_issues,
        build_context_pack,
        ask_lore,
        impact_of,
        propose_fix,
        quality_harness,
        export_project,
    ):
        server.tool(annotations=_annotations(TOOL_SAFETY[fn.__name__], ToolAnnotations))(fn)
    return server
