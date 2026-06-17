"""Export tests: the engine-agnostic data bundle + localization (CSV + XLIFF 1.2)."""

from __future__ import annotations

import csv
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
                localization_keys=["quest.quest_patrol.objective"],
                rewards=[Reward(kind="gold", value="75", amount=75)],
            )
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


def test_generic_export_writes_bundle_and_localization(tmp_path: Path) -> None:
    manifest = export_content_bundle(_bundle(), tmp_path, target_engine=EngineTarget.GENERIC)
    kinds = {item.kind for item in manifest.files}
    assert kinds == {"content_bundle", "localization_csv", "localization_xliff"}

    with (tmp_path / "localized_texts.csv").open(encoding="utf-8", newline="") as handle:
        loc_rows = list(csv.DictReader(handle))
    locales = {(row["Key"], row["Locale"]) for row in loc_rows}
    assert ("dlg.hello", "zh-CN") in locales and ("dlg.hello", "en") in locales


def test_localization_xliff_carries_maxwidth(tmp_path: Path) -> None:
    import xml.etree.ElementTree as ET

    export_content_bundle(_bundle(), tmp_path, target_engine=EngineTarget.GENERIC)
    xlf = tmp_path / "localized_texts.xlf"
    root = ET.fromstring(xlf.read_text(encoding="utf-8"))  # must be well-formed XML
    assert root.tag.endswith("xliff")
    text = xlf.read_text(encoding="utf-8")
    assert 'source-language="zh-CN"' in text and 'source-language="en"' in text
    assert 'maxwidth="40" size-unit="char"' in text  # the dialogue line's UI cap
    assert "<source>站住，报上名来！</source>" in text


def test_xliff_escapes_xml_special_characters() -> None:
    import xml.etree.ElementTree as ET

    from owcopilot.exporters.xliff import render_xliff

    bundle = ContentBundle(
        localized_texts={
            "t": LocalizedText(id="t", text_key="k", locale="en", text='A < B & "C" > D')
        }
    )
    root = ET.fromstring(render_xliff(bundle))  # raises if escaping is wrong
    unit = root.find(".//{urn:oasis:names:tc:xliff:document:1.2}source")
    assert unit is not None and unit.text == 'A < B & "C" > D'


def test_export_without_localization_is_just_the_bundle(tmp_path: Path) -> None:
    bundle = ContentBundle(quests={"q": Quest(id="q", title="Q")})
    manifest = export_content_bundle(bundle, tmp_path, target_engine=EngineTarget.GENERIC)
    assert [item.kind for item in manifest.files] == ["content_bundle"]
    assert not (tmp_path / "localized_texts.xlf").exists()


def test_manifest_hashes_cover_every_file(tmp_path: Path) -> None:
    manifest = export_content_bundle(_bundle(), tmp_path, target_engine=EngineTarget.GENERIC)
    reloaded = load_export_manifest(tmp_path / "manifest.json")
    assert reloaded.content_hash == manifest.content_hash
    for item in reloaded.files:
        assert (tmp_path / item.path).exists()
        assert len(item.sha256) == 64
