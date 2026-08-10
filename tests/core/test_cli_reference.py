"""``reference`` 子命令 + 同名 skill/MCP 工具：素材库接入 agent 面（N5.1）。

测试组装内容、驱动真实 CLI / skill 注册表 / MCP 工具函数，检索与存储逻辑一律复用
``project.reference_store`` / ``project.reference_context_builder``（与
``app.actions.references`` 的 add/list/search_references_action 同一份调用），本文件不重写。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from owcopilot.cli.main import main
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.mcp_server import tools as mcp_tools
from owcopilot.skills import default_skill_registry


def _empty_project(root: Path) -> None:
    ContentStore(root).save(ContentBundle())


def test_cli_reference_add_then_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "content"
    _empty_project(root)

    code = main(
        [
            "reference",
            "add",
            "--content-root",
            str(root),
            "--title",
            "旅行笔记",
            "--text",
            "一段关于沙漠商队与绿洲的灵感素材，描写了驼队首领的犹豫。",
            "--allowed-use",
            "drafting",
        ]
    )
    assert code == 0
    added = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert added["source"]["title"] == "旅行笔记"
    assert added["source"]["allowed_uses"] == ["drafting"]
    assert added["cost_budget"]["used_usd"] == 0.0

    code = main(["reference", "list", "--content-root", str(root)])
    assert code == 0
    listed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert listed["count"] == 1
    assert listed["sources"][0]["id"] == added["source"]["id"]


def test_cli_reference_search_matches_skill_and_mcp_tool(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI / skill / MCP 工具三面必须看到同一份检索事实——照 flag_xref 的三面对齐惯例。"""
    root = tmp_path / "content"
    _empty_project(root)
    main(
        [
            "reference",
            "add",
            "--content-root",
            str(root),
            "--title",
            "旅行笔记",
            "--text",
            "一段关于沙漠商队与绿洲的灵感素材，描写了驼队首领的犹豫。",
        ]
    )
    capsys.readouterr()  # discard the `add` output

    code = main(["reference", "search", "--content-root", str(root), "--query", "沙漠商队"])
    assert code == 0
    from_cli = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert from_cli["hits"], "应至少检出一条素材片段"

    registry = default_skill_registry(content_root=str(root))
    from_skill = registry.run("search_references", {"query": "沙漠商队"})
    assert from_skill["hits"] == from_cli["hits"]

    from_mcp = mcp_tools.search_references(content_root=str(root), query="沙漠商队")
    assert from_mcp["hits"] == from_cli["hits"]


def test_cli_reference_add_without_title_gets_guided_chinese_error(tmp_path: Path) -> None:
    root = tmp_path / "content"
    _empty_project(root)
    code = main(["reference", "add", "--content-root", str(root), "--text", "只有正文"])
    assert code == 2


def test_cli_reference_search_without_query_gets_guided_chinese_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "content"
    _empty_project(root)
    code = main(["reference", "search", "--content-root", str(root)])
    assert code == 2
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert error["type"] == "ValueError"
    assert "--query" in error["error"]
