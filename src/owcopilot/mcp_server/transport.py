# 可选的 MCP 传输工厂：把 SDK-free 的工具处理器 + 只读 Resources + Prompt 模板绑到 FastMCP。
"""Optional MCP transport factory.

The core tool handlers live in `tools.py` and have no MCP SDK dependency. This module is the thin
boundary that binds those handlers — plus a minimal set of read-only Resources and Prompt templates
— to an installed MCP transport when available. Actually running the server (opening the shared
project for the transport's lifetime) lives in `serve.py`.
"""

from __future__ import annotations

import json
from typing import Any

from .tools import (
    TOOL_SAFETY,
    ToolSafety,
    _shared_project,
    ask_lore,
    audit_project,
    build_context_pack,
    export_project,
    impact_of,
    list_issues,
    propose_fix,
    quality_harness,
)

#: URIs of the read-only Resources this server publishes (kept here so tests/docs have one source).
CANON_RESOURCE_URI = "owcopilot://canon"
OPEN_ISSUES_RESOURCE_URI = "owcopilot://issues/open"


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


def _require_bound_project() -> Any:
    """Return the serve-bound :class:`ProjectContext`, or raise a guided error if none is bound.

    Resources read the *currently served* project (the one the serve lifespan opened and published
    on ``_shared_project``). A resource read with no project bound — e.g. someone constructing the
    raw factory server without a lifespan — is a caller error, so it fails with a message pointing
    at the serve entry rather than a bare ``AttributeError`` on ``None``."""
    project = _shared_project.get()
    if project is None:
        raise RuntimeError(
            "no project is bound to this MCP server; resources read the served project. "
            "Start the server via `owcopilot mcp-serve --content-root <root>` "
            "(or `python -m owcopilot.mcp_server`), which binds one project for its lifetime."
        )
    return project


def _canon_resource() -> str:
    """The bound project's canon as read-only JSON: ``content_hash`` + the full content bundle.

    This is the Resources-primitive counterpart to the ``build_context_pack`` tool — a client that
    just wants to *read* canon no longer spends a tool-call round trip. Reading is O(serialize) on
    the already-loaded bundle; no graph/vector work."""
    from ..content.hash import content_hash

    project = _require_bound_project()
    return json.dumps(
        {
            "content_hash": content_hash(project.bundle),
            "bundle": project.bundle.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _open_issues_resource() -> str:
    """The bound project's currently-open audit issues as read-only JSON.

    Mirrors the ``list_issues`` tool's open-status view as a Resource so a client can poll canon
    health without a tool call. Reads only the ``issues`` table on the shared store."""
    project = _require_bound_project()
    issues = project.sqlite_store.list_issues(status="open")
    return json.dumps(
        {
            "count": len(issues),
            "issues": [issue.model_dump(mode="json") for issue in issues],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _consistency_review_prompt(focus: str = "") -> str:
    """A minimal MCP Prompt template: the canon-consistency review workflow an external agent should
    follow before editing or exporting.

    Deliberately self-contained (no dependency on the internal ``prompts`` registry, which is the
    T8 versioning surface owned elsewhere): this exposes the Prompts *primitive* with one broadly
    useful template. Deeper integration — publishing the versioned internal prompt assets as MCP
    Prompts — is tracked as future work (see the module report / helloagents 10-6)."""
    scope = f" Focus on: {focus}." if focus.strip() else ""
    return (
        "You are reviewing an open-world narrative project for canon consistency using the "
        "owcopilot MCP tools." + scope + "\n\n"
        "Follow this loop:\n"
        "1. Call `quality_harness` to get the current audit, readiness gates and suggested fixes.\n"
        "2. For each open error, call `list_issues` to inspect it and `impact_of` to see what a "
        "change would touch.\n"
        "3. Call `propose_fix` for issues you intend to resolve; review the shadow-validated "
        "candidates (do NOT apply them — applying canon writes stays in the human CLI/UI path).\n"
        "4. Re-run `quality_harness` until `export_ready` is true, then stop and summarise.\n\n"
        "Read `owcopilot://canon` for the current world and `owcopilot://issues/open` for the live "
        "issue list. Never invent entity ids — cite only refs that appear in canon."
    )


def create_mcp_server(name: str = "owcopilot", lifespan: Any = None) -> Any:
    """Create a FastMCP server and register the owcopilot tools, plus read-only Resources and a
    Prompt template.

    Each tool carries MCP ToolAnnotations (readOnly/destructive/idempotent/openWorld) derived from
    :data:`TOOL_SAFETY` so external clients can do risk-based approval and parallelism. Write-style
    actions (patch apply, review accept) are intentionally not registered: the human-in-the-loop
    write path lives only in the CLI/UI.

    ``lifespan`` (optional) is passed straight to FastMCP; :func:`serve.shared_project_lifespan`
    supplies one that binds a shared :class:`ProjectContext` for the server's lifetime (C-5). The
    Resources read that bound project, so they are meaningful only under such a lifespan.
    """
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
    except ImportError as e:
        raise MCPTransportUnavailable(
            "MCP transport is optional. Install the MCP SDK, for example with "
            "`pip install -e .[mcp]`, to run the server transport."
        ) from e

    server = FastMCP(name, lifespan=lifespan) if lifespan is not None else FastMCP(name)
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

    # Read-only Resources: canon + open issues of the serve-bound project (MCP Resources primitive).
    server.resource(
        CANON_RESOURCE_URI,
        name="canon",
        description="The served project's canon (content_hash + full content bundle) as JSON.",
        mime_type="application/json",
    )(_canon_resource)
    server.resource(
        OPEN_ISSUES_RESOURCE_URI,
        name="open_issues",
        description="The served project's currently-open audit issues as JSON.",
        mime_type="application/json",
    )(_open_issues_resource)

    # Prompt template: the consistency-review workflow (MCP Prompts primitive).
    server.prompt(
        name="consistency_review",
        description="Guided canon-consistency review loop over the owcopilot MCP tools.",
    )(_consistency_review_prompt)

    return server
