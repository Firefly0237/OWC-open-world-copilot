"""E3 · export gate covers engine targets + controlled back-flow with three-way conflicts.

Tests assemble inputs and assert on production code (assert_export_ready / plan_engine_sync_back /
sync_back_action / the sync-back CLI) — no business logic copied.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from owcopilot.app.actions import sync_back_action
from owcopilot.app.engine_sync import plan_engine_sync_back
from owcopilot.cli.main import main
from owcopilot.content.models import ContentBundle, Quest
from owcopilot.content.store import ContentStore


def _quest_row(qid: str, title: str, objective: str = "") -> dict:
    return {"id": qid, "title": title, "objective": objective}


def _bundle(*quests: Quest) -> ContentBundle:
    return ContentBundle(quests={q.id: q for q in quests})


# --- three-way classification -------------------------------------------------------------------
def test_engine_only_change_is_safe_changed() -> None:
    baseline = [_quest_row("q1", "旧标题")]
    engine = [_quest_row("q1", "引擎改的新标题")]
    library = _bundle(Quest(id="q1", title="旧标题"))  # library == baseline (didn't move)
    plan = plan_engine_sync_back(engine, library, baseline=baseline)
    assert plan["changed"] == ["q1"] and plan["conflicts"] == []
    assert "q1" in plan["_quests"]


def test_both_sides_moved_is_conflict_and_never_staged() -> None:
    baseline = [_quest_row("q1", "旧标题")]
    engine = [_quest_row("q1", "引擎改法")]
    library = _bundle(Quest(id="q1", title="库里改法"))  # library moved too
    plan = plan_engine_sync_back(engine, library, baseline=baseline)
    assert plan["conflicts"] == ["q1"] and plan["changed"] == []
    assert "q1" not in plan["_quests"]  # a conflict must never stage
    assert any("冲突" in w for w in plan["warnings"])


def test_library_ahead_engine_stale_is_skipped() -> None:
    baseline = [_quest_row("q1", "旧标题")]
    engine = [_quest_row("q1", "旧标题")]  # engine untouched since export
    library = _bundle(Quest(id="q1", title="库里的新标题"))
    plan = plan_engine_sync_back(engine, library, baseline=baseline)
    assert plan["library_ahead"] == ["q1"]
    assert plan["changed"] == [] and "q1" not in plan["_quests"]  # stale engine must not revert


def test_no_baseline_falls_back_two_way_with_honest_warning() -> None:
    engine = [_quest_row("q1", "引擎改法")]
    library = _bundle(Quest(id="q1", title="库里改法"))
    plan = plan_engine_sync_back(engine, library, baseline=None)
    assert plan["changed"] == ["q1"] and plan["conflicts"] == []
    assert plan["baseline_provided"] is False
    assert any("无法判定双侧冲突" in w for w in plan["warnings"])


def test_engine_side_deletion_is_surfaced_never_auto_deleted() -> None:
    baseline = [_quest_row("q1", "旧标题"), _quest_row("q2", "会被引擎删掉")]
    engine = [_quest_row("q1", "旧标题")]  # q2 vanished on the engine side
    library = _bundle(Quest(id="q1", title="旧标题"), Quest(id="q2", title="会被引擎删掉"))
    plan = plan_engine_sync_back(engine, library, baseline=baseline)
    assert plan["engine_missing"] == ["q2"]
    assert any("缺失" in w for w in plan["warnings"])
    assert "q2" not in plan["_quests"]  # surfaced only — zero authoritative deletes


def test_library_deleted_quest_is_not_resurrected() -> None:
    baseline = [_quest_row("q1", "旧标题")]
    engine_stale = [_quest_row("q1", "旧标题")]  # engine untouched; library deleted q1
    library = _bundle()
    plan = plan_engine_sync_back(engine_stale, library, baseline=baseline)
    assert plan["new"] == [] and "q1" not in plan["_quests"]  # deletion is newer — no resurrection
    assert plan["library_ahead"] == ["q1"]
    engine_edited = [_quest_row("q1", "引擎侧还改了")]  # deleted in library AND edited on engine
    plan2 = plan_engine_sync_back(engine_edited, library, baseline=baseline)
    assert plan2["conflicts"] == ["q1"] and "q1" not in plan2["_quests"]


def test_rest_import_path_shares_the_sync_back_classifier() -> None:
    from owcopilot.app.engine_sync import plan_engine_import

    engine = [_quest_row("q1", "引擎改法"), _quest_row("q2", "全新")]
    library = _bundle(Quest(id="q1", title="库里改法"))
    plan = plan_engine_import(engine, library)
    # historical keys intact, plus the honest superset (warnings/conflicts) now reaches REST too
    assert plan["new"] == ["q2"] and plan["changed"] == ["q1"]
    assert plan["baseline_provided"] is False
    assert any("无法判定双侧冲突" in w for w in plan["warnings"])


def test_new_and_unchanged_quests_classify() -> None:
    baseline = [_quest_row("q1", "同一标题")]
    engine = [_quest_row("q1", "同一标题"), _quest_row("q2", "全新任务")]
    library = _bundle(Quest(id="q1", title="同一标题"))
    plan = plan_engine_sync_back(engine, library, baseline=baseline)
    assert plan["new"] == ["q2"] and plan["unchanged"] == ["q1"]


# --- the action: review-queue routing, zero authoritative writes --------------------------------
def _seed_root(tmp_path: Path) -> Path:
    root = tmp_path / "content"
    ContentStore(root).save(_bundle(Quest(id="q1", title="旧标题")))
    return root


def test_sync_back_action_stages_to_review_and_writes_nothing(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    engine_path = tmp_path / "engine.json"
    engine_path.write_text(
        json.dumps([_quest_row("q1", "引擎改的新标题")], ensure_ascii=False), encoding="utf-8"
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps([_quest_row("q1", "旧标题")], ensure_ascii=False), encoding="utf-8"
    )

    result = sync_back_action(
        root, input_path=str(engine_path), baseline_path=str(baseline_path), apply=True
    )
    assert result["applied"] is True and result["review_item_id"]
    assert result["changed"] == ["q1"] and result["conflicts"] == []
    # zero authoritative writes: the library still holds the OLD title until a human approves
    assert ContentStore(root).load().quests["q1"].title == "旧标题"


def test_sync_back_action_rejects_non_array_input(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text('{"id": "q1"}', encoding="utf-8")
    with pytest.raises(ValueError, match="行数组"):
        sync_back_action(root, input_path=str(bad))


# --- CLI ------------------------------------------------------------------------------------------
def test_cli_sync_back_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed_root(tmp_path)
    engine_path = tmp_path / "engine.json"
    engine_path.write_text(
        json.dumps([_quest_row("q1", "引擎改的新标题")], ensure_ascii=False), encoding="utf-8"
    )
    code = main(["sync-back", "--content-root", str(root), "--input", str(engine_path)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["changed"] == ["q1"] and payload["applied"] is False


# --- export gate covers the engine target (guard-extension proof) --------------------------------
def test_export_gate_blocks_unreal_target_on_audit_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "content"
    # A quest whose giver references a missing entity -> deterministic ERROR issue
    ContentStore(root).save(
        ContentBundle(quests={"q1": Quest(id="q1", title="坏引用", giver_npc="npc_ghost")})
    )
    code = main(
        [
            "export",
            "--content-root",
            str(root),
            "--output-dir",
            str(tmp_path / "out"),
            "--target-engine",
            "unreal",
        ]
    )
    assert code == 2  # guided JSON error on stderr, no traceback
    err = capsys.readouterr().err
    assert "导出被发布门阻断" in err
    assert not (tmp_path / "out" / "unreal").exists()  # nothing was written past the gate
