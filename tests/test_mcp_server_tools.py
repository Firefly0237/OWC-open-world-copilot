from __future__ import annotations

import pytest

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.mcp_server import (
    ask_lore,
    audit_project,
    build_context_pack,
    export_project,
    list_issues,
)


def _write_project(content_root) -> None:
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


def test_mcp_audit_project_persists_and_lists_issues(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)

    audit = audit_project(content_root=str(content_root))
    issues = list_issues(
        content_root=str(content_root),
        rule_code="UNKNOWN_ENTITY_REF",
        status="open",
    )

    assert audit["open_errors"] >= 1
    assert "UNKNOWN_ENTITY_REF" in {issue["rule_code"] for issue in audit["issues"]}
    assert audit["cost_budget"]["used_usd"] == 0.0
    assert issues["count"] == 1
    assert issues["issues"][0]["target_ref"] == "quest:q1"
    assert issues["cost_budget"]["used_usd"] == 0.0


def test_mcp_context_pack_and_ask_lore(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)

    pack = build_context_pack(content_root=str(content_root), query="Aldric caravan")
    answer = ask_lore(content_root=str(content_root), query="Who is Aldric?", max_cost_usd=0.0)

    assert "entity:npc_aldric" in pack["refs"]
    assert pack["cost_budget"]["used_usd"] == 0.0
    assert answer["answer"]["citations"][0]["ref"] == "entity:npc_aldric"
    assert answer["telemetry"]["calls"] == 1
    assert answer["cost_budget"]["over_budget"] is True


def test_mcp_export_project_writes_engine_scoped_bundle(tmp_path) -> None:
    content_root = tmp_path / "content"
    output_root = tmp_path / "exports"
    _write_project(content_root)

    result = export_project(
        content_root=str(content_root),
        output_dir=str(output_root),
        target_engine="unreal",
    )

    export_dir = output_root / "unreal"
    assert result["output_dir"] == str(export_dir)
    assert (export_dir / "content_bundle.json").exists()
    assert (export_dir / "manifest.json").exists()
    assert result["manifest"]["target_engine"] == "unreal"
    assert result["cost_budget"]["used_usd"] == 0.0


def test_mcp_tools_reject_missing_content_root(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="content root does not exist"):
        audit_project(content_root=str(tmp_path / "missing"))
