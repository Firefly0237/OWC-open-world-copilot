"""Engine-specific export tests: UE DataTable CSV and Unity per-quest JSON."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from owcopilot.content.models import (
    ContentBundle,
    DialogueRef,
    Entity,
    EntityType,
    LocalizedText,
    Quest,
    Reward,
)
from owcopilot.exporters import EngineTarget, export_content_bundle, load_export_manifest


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_mara": Entity(
                id="npc_mara", name="玛拉", type=EntityType.NPC, description="边境斥候。"
            )
        },
        quests={
            "quest_patrol": Quest(
                id="quest_patrol",
                title="巡逻边境",
                giver_npc="npc_mara",
                objective="在天黑前巡视边境线。",
                prerequisites=["quest_intro"],
                timeline_order=3,
                localization_keys=["quest.quest_patrol.objective"],
                rewards=[Reward(kind="gold", value="75", amount=75)],
            ),
            "quest_intro": Quest(
                id="quest_intro",
                title="新兵报到",
                objective="向玛拉报到。",
                localization_keys=["quest.quest_intro.objective"],
            ),
        },
        dialogues={
            "dlg_hello": DialogueRef(
                id="dlg_hello",
                text_key="dlg.hello",
                speaker_id="npc_mara",
                text="站住，报上名来！",
                locale="zh-CN",
                ui_max_len=40,
            )
        },
        localized_texts={
            "loc_hello_en": LocalizedText(
                id="loc_hello_en", text_key="dlg.hello", locale="en", text="Halt! Name yourself!"
            )
        },
    )


def test_unreal_export_writes_datatable_csv(tmp_path: Path) -> None:
    manifest = export_content_bundle(_bundle(), tmp_path, target_engine=EngineTarget.UNREAL)
    kinds = {item.kind for item in manifest.files}
    assert {"content_bundle", "ue_datatable_csv", "localization_csv"} <= kinds

    with (tmp_path / "quests_datatable.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["Name"] for row in rows} == {"Quest_quest_patrol", "Quest_quest_intro"}
    patrol = next(row for row in rows if row["Name"] == "Quest_quest_patrol")
    assert patrol["Title"] == "巡逻边境"
    assert patrol["GiverNPC"] == "npc_mara"
    assert json.loads(patrol["Prerequisites"]) == ["quest_intro"]
    assert patrol["TimelineOrder"] == "3"
    assert json.loads(patrol["Rewards"])[0]["kind"] == "gold"

    with (tmp_path / "localized_texts.csv").open(encoding="utf-8", newline="") as handle:
        loc_rows = list(csv.DictReader(handle))
    locales = {(row["Key"], row["Locale"]) for row in loc_rows}
    assert ("dlg.hello", "zh-CN") in locales and ("dlg.hello", "en") in locales


def test_unity_export_writes_per_quest_json(tmp_path: Path) -> None:
    manifest = export_content_bundle(_bundle(), tmp_path, target_engine=EngineTarget.UNITY)
    kinds = {item.kind for item in manifest.files}
    assert {"content_bundle", "unity_quest_json", "unity_index", "localization_csv"} <= kinds

    quest = json.loads((tmp_path / "quests" / "quest_patrol.json").read_text(encoding="utf-8"))
    assert quest["giverNpc"] == "npc_mara"  # camelCase for JsonUtility
    assert quest["timelineOrder"] == 3
    assert quest["reviewStatus"] == "approved"
    index = json.loads((tmp_path / "quests" / "index.json").read_text(encoding="utf-8"))
    assert set(index["quests"]) == {"quest_patrol.json", "quest_intro.json"}


def test_generic_export_stays_minimal(tmp_path: Path) -> None:
    manifest = export_content_bundle(_bundle(), tmp_path, target_engine=EngineTarget.GENERIC)
    assert [item.kind for item in manifest.files] == ["content_bundle"]
    assert not (tmp_path / "quests_datatable.csv").exists()


def test_manifest_hashes_cover_engine_files(tmp_path: Path) -> None:
    manifest = export_content_bundle(_bundle(), tmp_path, target_engine=EngineTarget.UNREAL)
    reloaded = load_export_manifest(tmp_path / "manifest.json")
    assert reloaded.content_hash == manifest.content_hash
    for item in reloaded.files:
        assert (tmp_path / item.path).exists()
        assert len(item.sha256) == 64
