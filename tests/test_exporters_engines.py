"""Export tests: the engine-agnostic data bundle + localization (CSV + XLIFF 1.2)."""

from __future__ import annotations

import csv
from pathlib import Path

from owcopilot.content.models import (
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueRef,
    DialogueTree,
    Entity,
    EntityType,
    LocalizedText,
    Quest,
    Reward,
)
from owcopilot.exporters import EngineTarget, export_content_bundle, load_export_manifest

_GENERIC_GOLDEN = Path(__file__).parent / "fixtures" / "generic_golden"


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


def test_generic_export_byte_identical_to_golden(tmp_path: Path) -> None:
    """The UNREAL dispatch must not perturb GENERIC by one byte — locked by frozen fixtures.

    ``manifest.json`` carries a ``generated_at`` timestamp, so it is compared structurally with
    that one field removed; the three deterministic artifacts are compared byte-for-byte."""
    import json

    export_content_bundle(_bundle(), tmp_path, target_engine=EngineTarget.GENERIC)
    for name in ("content_bundle.json", "localized_texts.csv", "localized_texts.xlf"):
        assert (tmp_path / name).read_bytes() == (_GENERIC_GOLDEN / name).read_bytes(), (
            f"{name} drifted from the frozen GENERIC golden fixture"
        )

    produced = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    golden = json.loads((_GENERIC_GOLDEN / "manifest.json").read_text(encoding="utf-8"))
    produced.pop("generated_at")
    golden.pop("generated_at")
    assert produced == golden

    assert not (tmp_path / "unreal").exists()  # GENERIC never grows an unreal/ projection
    assert not any(f["kind"].startswith("unreal") for f in produced["files"])


def test_unreal_target_is_a_superset_of_generic(tmp_path: Path) -> None:
    manifest = export_content_bundle(_bundle(), tmp_path, target_engine=EngineTarget.UNREAL)

    # The generic bundle underneath is still byte-identical to the golden fixture.
    assert (tmp_path / "content_bundle.json").read_bytes() == (
        _GENERIC_GOLDEN / "content_bundle.json"
    ).read_bytes()

    ue_files = [f for f in manifest.files if f.path.startswith("unreal/")]
    assert {f.kind for f in ue_files} >= {"datatable", "string_table", "row_struct_header"}
    for item in ue_files:
        assert (tmp_path / item.path).exists()
        assert len(item.sha256) == 64


def _bundle_with_tree() -> ContentBundle:
    bundle = _bundle()
    bundle.dialogue_trees["dt_meet"] = DialogueTree(
        id="dt_meet",
        title="初遇玛拉",
        quest_id="quest_patrol",
        root_node="n_open",
        nodes={
            "n_open": DialogueNode(
                id="n_open",
                speaker_id="npc_mara",
                text="你是谁？",
                choices=[DialogueChoice(text="我来帮你。", next_node="n_help")],
            ),
            "n_help": DialogueNode(id="n_help", text="那就跟我来。"),
        },
    )
    return bundle


def test_unity_target_is_a_superset_of_generic(tmp_path: Path) -> None:
    manifest = export_content_bundle(_bundle(), tmp_path, target_engine=EngineTarget.UNITY)

    # The generic bundle underneath is still byte-identical to the golden fixture — UNITY only adds.
    assert (tmp_path / "content_bundle.json").read_bytes() == (
        _GENERIC_GOLDEN / "content_bundle.json"
    ).read_bytes()

    unity_files = [f for f in manifest.files if f.path.startswith("unity/")]
    assert [f.kind for f in unity_files] == ["unity_import_manifest"]
    for item in unity_files:
        assert (tmp_path / item.path).exists()
        assert len(item.sha256) == 64

    # The manifest sha matches the file actually written (round-trip via the reloaded manifest).
    reloaded = load_export_manifest(tmp_path / "manifest.json")
    manifest_entry = next(f for f in reloaded.files if f.path == "unity/unity_import_manifest.json")
    import hashlib

    on_disk = hashlib.sha256(
        (tmp_path / "unity" / "unity_import_manifest.json").read_bytes()
    ).hexdigest()
    assert manifest_entry.sha256 == on_disk


def test_unity_manifest_carries_counts_paths_and_schema(tmp_path: Path) -> None:
    import json

    from owcopilot.content.hash import content_hash

    bundle = _bundle_with_tree()
    export_content_bundle(bundle, tmp_path, target_engine=EngineTarget.UNITY)
    payload = json.loads(
        (tmp_path / "unity" / "unity_import_manifest.json").read_text(encoding="utf-8")
    )

    assert payload["schema_version"] == 1
    assert payload["content_hash"] == content_hash(bundle)
    # Package-stable relative pointer back up to the generic bundle the importer reads.
    assert payload["bundle_path"] == "../content_bundle.json"
    assert (tmp_path / "unity" / payload["bundle_path"]).resolve() == (
        tmp_path / "content_bundle.json"
    ).resolve()
    # Counts mirror the Unity importer's ImportReport categories (dialogue_trees included).
    assert payload["content_counts"] == {
        "entities": 1,
        "quests": 1,
        "dialogues": 1,
        "dialogue_trees": 1,
        "relations": 0,
    }


def test_generic_and_unreal_never_grow_a_unity_dir(tmp_path: Path) -> None:
    export_content_bundle(_bundle(), tmp_path / "g", target_engine=EngineTarget.GENERIC)
    export_content_bundle(_bundle(), tmp_path / "u", target_engine=EngineTarget.UNREAL)
    assert not (tmp_path / "g" / "unity").exists()
    assert not (tmp_path / "u" / "unity").exists()


# --- Unity Localization String Table CSV (opt-in) ---------------------------------------------
def _localization_bundle() -> ContentBundle:
    """LocalizedText across two locales, a key present only in one locale (forces an empty cell),
    and a value carrying a comma, a double-quote and a newline (forces CSV escaping)."""
    return ContentBundle(
        localized_texts={
            "greeting_en": LocalizedText(
                id="greeting_en", text_key="greeting", locale="en", text="Hello, world"
            ),
            "greeting_zh": LocalizedText(
                id="greeting_zh", text_key="greeting", locale="zh-CN", text="你好，世界"
            ),
            # 'farewell' exists only in en -> its zh-CN cell must be empty (never default-filled).
            "farewell_en": LocalizedText(
                id="farewell_en", text_key="farewell", locale="en", text="Bye"
            ),
            "tricky_en": LocalizedText(
                id="tricky_en", text_key="tricky", locale="en", text='He said "hi", then\nleft'
            ),
        }
    )


def _read_string_table(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as handle:  # newline="" so embedded \n survives
        return list(csv.reader(handle))


def test_unity_localization_off_by_default_leaves_projection_untouched(tmp_path: Path) -> None:
    import json

    manifest = export_content_bundle(
        _localization_bundle(), tmp_path, target_engine=EngineTarget.UNITY
    )
    # No localization dir, and the sidecar manifest carries no localization block (byte-unchanged).
    assert not (tmp_path / "unity" / "localization").exists()
    assert not any(f.kind.startswith("unity_localization") for f in manifest.files)
    payload = json.loads(
        (tmp_path / "unity" / "unity_import_manifest.json").read_text(encoding="utf-8")
    )
    assert "localization" not in payload


def test_unity_localization_optin_writes_official_csv(tmp_path: Path) -> None:
    export_content_bundle(
        _localization_bundle(),
        tmp_path,
        target_engine=EngineTarget.UNITY,
        unity_localization=True,
    )
    csv_path = tmp_path / "unity" / "localization" / "owcopilot_strings.csv"
    assert csv_path.exists()
    rows = _read_string_table(csv_path)

    # Header row: Key first, then one column per locale in Unity's `LocaleName(code)` shape.
    assert rows[0] == ["Key", "English(en)", "Chinese (Simplified)(zh-CN)"]

    by_key = {row[0]: row[1:] for row in rows[1:]}
    # Key set is exactly the LocalizedText text_key set.
    assert set(by_key) == {"greeting", "farewell", "tricky"}
    # Present translations land in the right column; the missing zh-CN cell is empty (not filled).
    assert by_key["greeting"] == ["Hello, world", "你好，世界"]
    assert by_key["farewell"] == ["Bye", ""]
    # Comma + double-quote + newline survive the CSV round-trip intact.
    assert by_key["tricky"] == ['He said "hi", then\nleft', ""]


def test_unity_localization_optin_records_files_and_manifest_block(tmp_path: Path) -> None:
    import json

    manifest = export_content_bundle(
        _localization_bundle(),
        tmp_path,
        target_engine=EngineTarget.UNITY,
        unity_localization=True,
    )
    # Both new artifacts are in the export manifest with real checksums pointing at real files.
    loc_files = {f.kind: f for f in manifest.files if f.kind.startswith("unity_localization")}
    assert set(loc_files) == {"unity_localization_csv", "unity_localization_readme"}
    assert loc_files["unity_localization_csv"].path == "unity/localization/owcopilot_strings.csv"
    for item in loc_files.values():
        on_disk = tmp_path / item.path
        assert on_disk.exists()
        assert __import__("hashlib").sha256(on_disk.read_bytes()).hexdigest() == item.sha256

    # The sidecar manifest grows a localization cross-check block mirroring content_counts.
    payload = json.loads(
        (tmp_path / "unity" / "unity_import_manifest.json").read_text(encoding="utf-8")
    )
    assert payload["localization"] == {
        "collection_name": "owcopilot_strings",
        "csv_path": "localization/owcopilot_strings.csv",
        "locales": ["en", "zh-CN"],
        "string_count": 3,
    }
    # content_counts is untouched by the localization opt-in.
    assert payload["content_counts"]["quests"] == 0


def test_unity_localization_unknown_locale_falls_back_to_bare_code(tmp_path: Path) -> None:
    bundle = ContentBundle(
        localized_texts={"x": LocalizedText(id="x", text_key="k", locale="xx-custom", text="v")}
    )
    export_content_bundle(
        bundle, tmp_path, target_engine=EngineTarget.UNITY, unity_localization=True
    )
    rows = _read_string_table(tmp_path / "unity" / "localization" / "owcopilot_strings.csv")
    assert rows[0] == ["Key", "xx-custom"]  # no display name known -> bare code header
