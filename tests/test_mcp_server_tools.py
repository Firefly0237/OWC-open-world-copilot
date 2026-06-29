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
    quality_harness,
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


def _write_clean_project(content_root) -> None:
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
            quests={
                "q1": Quest(
                    id="q1",
                    title="Q1",
                    giver_npc="npc_aldric",
                    objective="Help Aldric.",
                    localization_keys=["quest.q1.objective"],
                )
            },
        )
    )


def _write_overview_project(content_root) -> None:
    ContentStore(content_root).save(
        ContentBundle.model_validate(
            {
                "entities": {
                    "fac_iron": {
                        "id": "fac_iron",
                        "name": "铁盟",
                        "type": "faction",
                        "description": "铁盟是一个势力。",
                    },
                    "fac_salt": {
                        "id": "fac_salt",
                        "name": "盐会",
                        "type": "faction",
                        "description": "盐会是一个势力。",
                    },
                    "fac_mist": {
                        "id": "fac_mist",
                        "name": "雾党",
                        "type": "faction",
                        "description": "雾党是一个势力。",
                    },
                    "npc_a": {
                        "id": "npc_a",
                        "name": "阿尔",
                        "type": "npc",
                        "description": "阿尔是一名角色。",
                    },
                    "npc_b": {
                        "id": "npc_b",
                        "name": "贝拉",
                        "type": "npc",
                        "description": "贝拉是一名角色。",
                    },
                    "npc_c": {
                        "id": "npc_c",
                        "name": "卡尔",
                        "type": "npc",
                        "description": "卡尔是一名角色。",
                    },
                    "npc_d": {
                        "id": "npc_d",
                        "name": "黛西",
                        "type": "npc",
                        "description": "黛西是一名角色。",
                    },
                    "npc_e": {
                        "id": "npc_e",
                        "name": "俄岚",
                        "type": "npc",
                        "description": "俄岚是一名角色。",
                    },
                },
                "relations": [
                    {"source": "npc_a", "target": "fac_iron", "kind": "member_of"},
                    {"source": "npc_b", "target": "fac_iron", "kind": "member_of"},
                    {"source": "npc_c", "target": "fac_salt", "kind": "member_of"},
                    {"source": "npc_d", "target": "fac_salt", "kind": "member_of"},
                    {"source": "npc_e", "target": "fac_mist", "kind": "member_of"},
                    {"source": "fac_iron", "target": "fac_salt", "kind": "enemy_of"},
                    {"source": "fac_salt", "target": "fac_mist", "kind": "rival_of"},
                ],
            }
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


def test_mcp_list_issues_treats_empty_filter_as_no_filter(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)
    audit_project(content_root=str(content_root))  # persist the issues first

    # A tool-calling model commonly passes "" to mean "unset" (real DeepSeek did this); an empty
    # filter must NOT match zero rows.
    issues = list_issues(content_root=str(content_root), severity="", rule_code="", status="")
    assert issues["count"] >= 1


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


def test_mcp_ask_lore_uses_qa_overview_reports(tmp_path) -> None:
    from owcopilot.app.actions import run_build_overview_action

    content_root = tmp_path / "content"
    sqlite_path = str(tmp_path / "runtime.sqlite")
    _write_overview_project(content_root)
    run_build_overview_action(content_root, sqlite_path=sqlite_path, llm_mode="offline")

    answer = ask_lore(
        content_root=str(content_root),
        sqlite_path=sqlite_path,
        query="列出这个世界的主要势力以及它们之间的关系",
        budget_tokens=800,
    )

    assert answer["answer"]["refused"] is False
    assert any(
        citation["ref"].startswith("community:")
        for citation in answer["answer"]["citations"]
    )


def test_mcp_quality_harness_returns_loop_state_and_proposals(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)

    result = quality_harness(content_root=str(content_root), max_issues=3)

    assert result["phase"] == "repair"
    assert result["export_ready"] is False
    assert result["top_issues"]
    assert result["patch_proposals"]
    assert result["patch_proposals"][0]["candidates"]
    assert result["next_tool_calls"][0]["tool"] == "list_issues"
    assert "audit_project" in result["tool_trace"]
    assert result["cost_budget"]["used_usd"] == 0.0


def test_mcp_quality_harness_reports_export_ready_for_clean_project(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_clean_project(content_root)

    result = quality_harness(content_root=str(content_root), propose_fixes=False)

    assert result["phase"] in {"complete_design", "ready_to_export"}
    assert result["export_ready"] is True
    assert result["export_blockers"] == []


def test_mcp_export_project_writes_engine_scoped_bundle(tmp_path) -> None:
    content_root = tmp_path / "content"
    output_root = tmp_path / "exports"
    _write_clean_project(content_root)

    result = export_project(
        content_root=str(content_root),
        output_dir=str(output_root),
        target_engine="generic",
    )

    export_dir = output_root / "generic"
    assert result["output_dir"] == str(export_dir)
    assert (export_dir / "content_bundle.json").exists()
    assert (export_dir / "manifest.json").exists()
    assert result["manifest"]["target_engine"] == "generic"
    assert result["cost_budget"]["used_usd"] == 0.0


def test_mcp_export_project_blocks_open_errors(tmp_path) -> None:
    content_root = tmp_path / "content"
    output_root = tmp_path / "exports"
    _write_project(content_root)

    with pytest.raises(ValueError, match="导出被发布门阻断"):
        export_project(
            content_root=str(content_root),
            output_dir=str(output_root),
            target_engine="generic",
        )


def test_mcp_tools_reject_missing_content_root(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="content root does not exist"):
        audit_project(content_root=str(tmp_path / "missing"))
