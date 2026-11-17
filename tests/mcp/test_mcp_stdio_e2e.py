"""Real MCP stdio subprocess proof for the game-narrative server.

The in-memory SDK tests prove protocol dispatch, but they do not exercise process launch or
newline-delimited stdio framing.  This test launches the same ``python -m`` entry an IDE/agent host
would use and completes an initialize/list/call/resource/prompt round trip over OS pipes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio
import pytest

pytest.importorskip("mcp", reason="MCP SDK optional extra ([mcp]) not installed")

from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

from mcp import ClientSession  # noqa: E402
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest  # noqa: E402
from owcopilot.content.store import ContentStore  # noqa: E402
from owcopilot.mcp_server.transport import CANON_RESOURCE_URI  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_game_project(content_root: Path) -> None:
    """Write a deliberately inconsistent narrative project for a meaningful audit call."""
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
            quests={"q1": Quest(id="q1", title="Missing giver", giver_npc="npc_missing")},
        )
    )


def test_real_stdio_subprocess_initialize_list_call_resource_and_prompt(tmp_path: Path) -> None:
    content_root = tmp_path / "content"
    _write_game_project(content_root)

    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath = str(REPO_ROOT / "src")
    if existing_pythonpath:
        pythonpath = pythonpath + os.pathsep + existing_pythonpath

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "owcopilot.mcp_server",
            "--content-root",
            str(content_root),
            "--sqlite-path",
            str(tmp_path / "runtime.sqlite"),
            "--transport",
            "stdio",
            "--name",
            "owcopilot-stdio-e2e",
        ],
        env={"PYTHONPATH": pythonpath},
        cwd=REPO_ROOT,
    )
    child_stderr_path = tmp_path / "child.stderr.log"

    async def _run(child_stderr) -> None:
        with anyio.fail_after(20):
            async with stdio_client(params, errlog=child_stderr) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as client:
                    initialized = await client.initialize()
                    assert initialized.serverInfo.name == "owcopilot-stdio-e2e"

                    tools = await client.list_tools()
                    names = {tool.name for tool in tools.tools}
                    assert {"audit_project", "list_issues", "ask_lore"} <= names

                    audit = await client.call_tool(
                        "audit_project", {"content_root": str(content_root)}
                    )
                    assert audit.isError is False
                    assert json.loads(audit.content[0].text)["open_errors"] >= 1

                    resources = await client.list_resources()
                    assert CANON_RESOURCE_URI in {str(item.uri) for item in resources.resources}
                    canon = await client.read_resource(CANON_RESOURCE_URI)  # type: ignore[arg-type]
                    assert "npc_aldric" in json.loads(canon.contents[0].text)["bundle"]["entities"]

                    prompts = await client.list_prompts()
                    assert "consistency_review" in {item.name for item in prompts.prompts}
                    prompt = await client.get_prompt(
                        "consistency_review", {"focus": "quest giver consistency"}
                    )
                    assert "quest giver consistency" in prompt.messages[0].content.text

    # Windows subprocess creation requires a real OS file descriptor for stderr; StringIO is not
    # sufficient. Keeping the log beside the test project also makes launch failures diagnosable.
    with child_stderr_path.open("w+", encoding="utf-8") as child_stderr:
        anyio.run(_run, child_stderr)
