from __future__ import annotations

from owcopilot.app import (
    build_content_inventory,
    build_context_pack_preview,
    build_export_summary,
    build_issue_summary,
    build_project_overview,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.exporters import export_content_bundle
from owcopilot.pipeline.audit import run_full_audit
from owcopilot.pipeline.project import ProjectContext


def _bundle() -> ContentBundle:
    return ContentBundle(
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


def _write_project(content_root) -> None:
    ContentStore(content_root).save(_bundle())


def _persist_audit(content_root) -> None:
    sqlite_path = content_root / ".owcopilot" / "runtime.sqlite"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    project = ProjectContext.open(content_root, sqlite_path=sqlite_path)
    try:
        run_full_audit(project)
    finally:
        project.close()


def test_build_project_overview_counts_content_and_graph(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)

    overview = build_project_overview(content_root)

    assert overview["counts"]["entities"] == 1
    assert overview["counts"]["quests"] == 1
    assert overview["graph"]["nodes"] >= 2
    assert len(overview["content_hash"]) == 64


def test_build_issue_summary_groups_persisted_issues(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)
    _persist_audit(content_root)

    summary = build_issue_summary(content_root)

    assert summary["count"] >= 1
    assert summary["by_rule"]["UNKNOWN_ENTITY_REF"] == 1
    assert summary["by_status"]["open"] >= 1
    assert summary["cost_budget"]["used_usd"] == 0.0


def test_build_context_pack_preview_returns_refs(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)

    preview = build_context_pack_preview(content_root, query="Aldric caravan")

    assert "entity:npc_aldric" in preview["refs"]
    assert preview["hits"]
    assert preview["cost_budget"]["used_usd"] == 0.0


def test_build_export_summary_reports_manifest_state(tmp_path) -> None:
    output_root = tmp_path / "exports"

    missing = build_export_summary(output_dir=output_root, target_engine="generic")
    assert missing["manifest_exists"] is False

    export_content_bundle(_bundle(), output_root / "generic", target_engine="generic")
    summary = build_export_summary(output_dir=output_root, target_engine="generic")

    assert summary["manifest_exists"] is True
    assert summary["manifest"]["target_engine"] == "generic"
    assert summary["cost_budget"]["used_usd"] == 0.0


def test_build_content_inventory_lists_rows_and_graph_refs(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)

    inventory = build_content_inventory(content_root)

    assert [row["id"] for row in inventory["entities"]] == ["npc_aldric"]
    assert inventory["entities"][0]["type"] == "npc"
    assert inventory["entities"][0]["origin"] == "human"
    assert [row["id"] for row in inventory["quests"]] == ["q1"]
    assert inventory["quests"][0]["title"] == "Q1"
    assert "entity:npc_aldric" in inventory["graph_refs"]
    assert inventory["cost_budget"]["used_usd"] == 0.0
