# 真 SDK 端到端冒烟：真 client ↔ 真 FastMCP server，走真协议(不 mock 传输层)。
"""Real MCP SDK smoke tests — the A1 proof that the protocol actually runs end to end.

Before this file the only transport test (``test_mcp_server_transport.py``) fed FastMCP a *fake*
module and asserted registration order; the real SDK had never been exercised, and it had already
bitten once (a ``func_metadata`` SchemaError from an arbitrary-typed parameter). These tests wire a
**real** ``mcp.client.ClientSession`` to a **real** FastMCP server over the SDK's in-memory stream
transport (``create_connected_server_and_client_session``), so the full path runs unmocked:
``initialize`` → ``list_tools`` → ``call_tool`` → ``list_resources`` → ``read_resource`` →
``list_prompts`` → ``get_prompt``. The server is the exact one ``run_server`` would run
(:func:`build_serving_server`), including the shared-project lifespan (C-5).

Transport note (A5): the in-memory stream transport exercises the same message-dispatch path as
stdio/HTTP (the low-level ``Server.run`` receive loop and the FastMCP lifespan) without a subprocess
or a socket, so it is deterministic and offline. stdio/HTTP framing itself (subprocess pipes, SSE)
is not covered here — that is a wire-format concern layered under the identical protocol tested."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

pytest.importorskip("mcp", reason="MCP SDK optional extra ([mcp]) not installed")

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.mcp_server import build_serving_server, tools  # noqa: E402
from owcopilot.mcp_server.transport import (  # noqa: E402
    CANON_RESOURCE_URI,
    OPEN_ISSUES_RESOURCE_URI,
)


def _write_dirty_project(content_root: Path) -> None:
    """A world with a dangling quest giver, so an audit reports >=1 open error."""
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Caravan master",
                )
            },
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")},
        )
    )


def _serving_server(tmp_path: Path):
    root = tmp_path / "content"
    _write_dirty_project(root)
    return root, build_serving_server(root, sqlite_path=str(tmp_path / "runtime.sqlite"))


def test_real_sdk_smoke_list_and_call_tools(tmp_path: Path) -> None:
    """A1 core: a real client lists the tools (with annotations) and calls two of them over the real
    protocol. ``audit_project`` populates issues; the read-only ``list_issues`` reads them back —
    proving both the round trip AND the shared-project write-then-visible through the transport."""
    root, server = _serving_server(tmp_path)
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def _body() -> None:
        async with connect(server) as client:
            listed = await client.list_tools()
            names = [t.name for t in listed.tools]
            assert names == [
                "audit_project",
                "list_issues",
                "build_context_pack",
                "search_references",
                "ask_lore",
                "flag_xref",
                "impact_of",
                "propose_fix",
                "quality_harness",
                "export_project",
            ]
            # The human write path (apply/accept) must never be exposed over MCP.
            assert "apply" not in " ".join(names)
            # ToolAnnotations survive the round trip: risk hints reach the client.
            by_name = {t.name: t for t in listed.tools}
            assert by_name["list_issues"].annotations.readOnlyHint is True
            assert by_name["search_references"].annotations.readOnlyHint is True
            assert by_name["flag_xref"].annotations.readOnlyHint is True
            assert by_name["ask_lore"].annotations.openWorldHint is True
            assert by_name["audit_project"].annotations.readOnlyHint is False
            assert by_name["audit_project"].annotations.destructiveHint is True
            assert by_name["audit_project"].annotations.idempotentHint is False
            assert by_name["audit_project"].annotations.openWorldHint is False

            audit = await client.call_tool("audit_project", {"content_root": str(root)})
            assert audit.isError is False
            audit_payload = json.loads(audit.content[0].text)
            assert audit_payload["open_errors"] >= 1

            listed_issues = await client.call_tool("list_issues", {"content_root": str(root)})
            assert listed_issues.isError is False
            issues_payload = json.loads(listed_issues.content[0].text)
            # The read-only tool sees exactly what audit just persisted (shared ctx, same store).
            assert issues_payload["count"] == audit_payload["open_errors"]

    anyio.run(_body)


def test_real_sdk_smoke_reads_resources(tmp_path: Path) -> None:
    """MCP Resources primitive over the real protocol: canon (content_hash + bundle) and the
    open-issues view both read the serve-bound project."""
    root, server = _serving_server(tmp_path)
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def _body() -> None:
        async with connect(server) as client:
            resources = await client.list_resources()
            uris = {str(r.uri) for r in resources.resources}
            assert uris == {CANON_RESOURCE_URI, OPEN_ISSUES_RESOURCE_URI}

            canon = await client.read_resource(CANON_RESOURCE_URI)  # type: ignore[arg-type]
            canon_payload = json.loads(canon.contents[0].text)
            assert canon_payload["content_hash"]
            assert "npc_aldric" in canon_payload["bundle"]["entities"]

            # Populate issues via a tool, then confirm the open-issues resource reflects them.
            await client.call_tool("audit_project", {"content_root": str(root)})
            open_issues = await client.read_resource(OPEN_ISSUES_RESOURCE_URI)  # type: ignore[arg-type]
            issues_payload = json.loads(open_issues.contents[0].text)
            assert issues_payload["count"] >= 1

    anyio.run(_body)


def test_real_sdk_smoke_gets_prompt(tmp_path: Path) -> None:
    """MCP Prompts primitive over the real protocol: the consistency-review template lists and
    renders, threading the optional ``focus`` argument into the returned message."""
    _root, server = _serving_server(tmp_path)
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def _body() -> None:
        async with connect(server) as client:
            prompts = await client.list_prompts()
            assert "consistency_review" in {p.name for p in prompts.prompts}

            rendered = await client.get_prompt("consistency_review", {"focus": "quest givers"})
            assert rendered.messages[0].role == "user"
            text = rendered.messages[0].content.text
            assert "quality_harness" in text  # the workflow the template steers the agent through
            assert "quest givers" in text  # the focus argument threaded in

    anyio.run(_body)


def test_serve_lifespan_binds_and_releases_shared_project(tmp_path: Path) -> None:
    """C-5 proof: the serve lifespan binds ONE shared project for the connection and releases it on
    shutdown.

    While bound, a tool called with a bogus ``content_root`` still succeeds because the served
    project is used and the argument is ignored (the documented single-project boundary). Were the
    lifespan not binding, ``list_issues`` would raise FileNotFoundError on the missing root. After
    the connection closes, ``_shared_project`` is reset — no leak into later calls."""
    _root, server = _serving_server(tmp_path)
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    assert tools._shared_project.get() is None  # unbound before serving

    async def _body() -> None:
        async with connect(server) as client:
            result = await client.call_tool(
                "list_issues", {"content_root": str(tmp_path / "does-not-exist")}
            )
            # Bound → bogus root ignored → no FileNotFoundError, a clean (empty) result.
            assert result.isError is False
            assert json.loads(result.content[0].text)["count"] == 0

    anyio.run(_body)

    assert tools._shared_project.get() is None  # lifespan reset on shutdown — no leak


def test_build_serving_server_registers_lifespan(tmp_path: Path) -> None:
    """The server ``run_server`` would run carries the shared-project lifespan (not the SDK
    default), so a real deployment gets the C-5 binding — asserted structurally, no transport."""
    root = tmp_path / "content"
    _write_dirty_project(root)
    server = build_serving_server(root, sqlite_path=str(tmp_path / "runtime.sqlite"))
    # FastMCP stores the user lifespan on its settings; a bound server must not use the default.
    assert server.settings.lifespan is not None
