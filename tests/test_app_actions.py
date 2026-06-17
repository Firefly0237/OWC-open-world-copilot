from __future__ import annotations

from owcopilot.app import run_project_audit_action, run_project_export_action
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore


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


def test_run_project_audit_action_persists_issues(tmp_path) -> None:
    content_root = tmp_path / "content"
    _write_project(content_root)

    result = run_project_audit_action(content_root)

    assert result["open_errors"] >= 1
    assert "UNKNOWN_ENTITY_REF" in {issue["rule_code"] for issue in result["issues"]}
    assert (content_root / ".owcopilot" / "runtime.sqlite").exists()
    assert result["cost_budget"]["used_usd"] == 0.0


def test_run_project_export_action_writes_target_engine_bundle(tmp_path) -> None:
    content_root = tmp_path / "content"
    output_root = tmp_path / "exports"
    _write_project(content_root)

    result = run_project_export_action(
        content_root, output_dir=output_root, target_engine="generic"
    )

    export_dir = output_root / "generic"
    assert result["output_dir"] == str(export_dir)
    assert result["manifest"]["target_engine"] == "generic"
    assert (export_dir / "content_bundle.json").exists()
    assert (export_dir / "manifest.json").exists()
    assert result["cost_budget"]["used_usd"] == 0.0
