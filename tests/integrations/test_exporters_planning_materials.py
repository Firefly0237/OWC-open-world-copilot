"""Tests for the opt-in planning-materials export (N8.1 ``planning_workbook.xlsx`` / N8.2
``planning_notes.docx``): both default off, both add exactly one file to the manifest when turned
on, and neither perturbs the pre-existing generic/localization output by one byte.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl

from owcopilot.content.documents import docx_text
from owcopilot.content.models import (
    ContentBundle,
    DialogueRef,
    Entity,
    EntityType,
    Quest,
    QuestStage,
    Reward,
    Term,
)
from owcopilot.exporters import AuditFindingInput, EngineTarget, export_content_bundle
from owcopilot.exporters.docx_notes import render_planning_notes
from owcopilot.exporters.xlsx_workbook import render_planning_workbook


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_mara": Entity(
                id="npc_mara",
                name="玛拉",
                type=EntityType.NPC,
                description="边境斥候。",
                aliases=["斥候长"],
                tags=["边境"],
                status="active",
            ),
            "fac_guard": Entity(
                id="fac_guard",
                name="边境卫戍",
                type=EntityType.FACTION,
                description="守边势力。",
            ),
        },
        quests={
            "quest_patrol": Quest(
                id="quest_patrol",
                title="巡逻边境",
                giver_npc="npc_mara",
                location="border_camp",
                objective="在天黑前巡视边境线。",
                prerequisites=["quest_intro"],
                dialogue_refs=["dlg_hello"],
                localization_keys=["quest.quest_patrol.objective"],
                timeline_order=2,
                tags=["main", "border"],
                stages=[QuestStage(id="s1", summary="在营地集合")],
                rewards=[Reward(kind="gold", value="75", amount=75)],
            )
        },
        dialogues={
            "dlg_hello": DialogueRef(
                id="dlg_hello",
                text_key="dlg.hello",
                speaker_id="npc_mara",
                quest_id="quest_patrol",
                text="站住，报上名来！",
                locale="zh-CN",
                ui_max_len=40,
            )
        },
        terms={
            "term_border": Term(
                id="term_border",
                canonical="边境线",
                aliases=["边界"],
                forbidden=["国境线"],
                description="巡逻任务的活动范围。",
            )
        },
    )


# --- sentinel: both flags off (omitted or explicit False) leaves everything else unchanged -----


def test_planning_materials_off_by_default_write_no_new_files(tmp_path: Path) -> None:
    manifest = export_content_bundle(_bundle(), tmp_path, target_engine=EngineTarget.GENERIC)

    assert not (tmp_path / "planning_workbook.xlsx").exists()
    assert not (tmp_path / "planning_notes.docx").exists()
    kinds = {f.kind for f in manifest.files}
    assert "planning_workbook" not in kinds
    assert "planning_notes" not in kinds


def test_planning_materials_omitted_is_byte_identical_to_explicit_false(tmp_path: Path) -> None:
    """Omitting ``planning_workbook``/``planning_notes`` must produce EXACTLY the same bytes as
    passing them explicitly as False for every existing file — the whole point of an opt-in
    default is that adopting the parameter costs an unaware caller nothing."""
    omitted = tmp_path / "omitted"
    explicit = tmp_path / "explicit_false"
    export_content_bundle(_bundle(), omitted, target_engine=EngineTarget.GENERIC)
    export_content_bundle(
        _bundle(),
        explicit,
        target_engine=EngineTarget.GENERIC,
        planning_workbook=False,
        planning_notes=False,
    )
    produced_names = {p.name for p in omitted.iterdir()} - {"manifest.json"}
    assert produced_names == {"content_bundle.json", "localized_texts.csv", "localized_texts.xlf"}
    for name in produced_names:
        assert (omitted / name).read_bytes() == (explicit / name).read_bytes(), name


def test_planning_workbook_optin_does_not_change_the_other_files(tmp_path: Path) -> None:
    without = tmp_path / "without"
    with_workbook = tmp_path / "with"
    export_content_bundle(_bundle(), without, target_engine=EngineTarget.GENERIC)
    export_content_bundle(
        _bundle(), with_workbook, target_engine=EngineTarget.GENERIC, planning_workbook=True
    )
    for name in ("content_bundle.json", "localized_texts.csv", "localized_texts.xlf"):
        assert (without / name).read_bytes() == (with_workbook / name).read_bytes(), name


def test_planning_notes_optin_does_not_change_the_other_files(tmp_path: Path) -> None:
    without = tmp_path / "without"
    with_notes = tmp_path / "with"
    export_content_bundle(_bundle(), without, target_engine=EngineTarget.GENERIC)
    export_content_bundle(
        _bundle(), with_notes, target_engine=EngineTarget.GENERIC, planning_notes=True
    )
    for name in ("content_bundle.json", "localized_texts.csv", "localized_texts.xlf"):
        assert (without / name).read_bytes() == (with_notes / name).read_bytes(), name


# --- N8.1 planning_workbook.xlsx: manifest registration + cell-for-cell round trip --------------


def test_planning_workbook_registers_in_manifest_with_sha256(tmp_path: Path) -> None:
    import hashlib

    manifest = export_content_bundle(
        _bundle(), tmp_path, target_engine=EngineTarget.GENERIC, planning_workbook=True
    )
    entry = next(f for f in manifest.files if f.kind == "planning_workbook")
    assert entry.path == "planning_workbook.xlsx"
    path = tmp_path / entry.path
    assert path.exists()
    assert entry.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_planning_workbook_sheets_match_bundle_cell_for_cell(tmp_path: Path) -> None:
    bundle = _bundle()
    export_content_bundle(
        bundle, tmp_path, target_engine=EngineTarget.GENERIC, planning_workbook=True
    )
    workbook = openpyxl.load_workbook(tmp_path / "planning_workbook.xlsx")
    assert workbook.sheetnames == ["任务表", "台词表", "实体表", "术语表"]

    quest_sheet = workbook["任务表"]
    assert [c.value for c in quest_sheet[1]] == [
        "id",
        "title",
        "giver_npc",
        "location",
        "objective",
        "prerequisites",
        "dialogue_refs",
        "localization_keys",
        "timeline_order",
        "tags",
    ]
    quest = bundle.quests["quest_patrol"]
    assert [c.value for c in quest_sheet[2]] == [
        quest.id,
        quest.title,
        quest.giver_npc,
        quest.location,
        quest.objective,
        ";".join(quest.prerequisites),
        ";".join(quest.dialogue_refs),
        ";".join(quest.localization_keys),
        quest.timeline_order,
        ";".join(quest.tags),
    ]

    dialogue_sheet = workbook["台词表"]
    dialogue = bundle.dialogues["dlg_hello"]
    assert [c.value for c in dialogue_sheet[2]] == [
        dialogue.id,
        dialogue.text_key,
        dialogue.speaker_id,
        dialogue.quest_id,
        dialogue.text,
        dialogue.locale,
        dialogue.ui_max_len,
    ]

    entity_sheet = workbook["实体表"]
    # Rows are sorted by id: fac_guard < npc_mara.
    entity = bundle.entities["fac_guard"]
    # openpyxl reads an empty-string cell back as None (not ""), so the two no-list-value columns
    # (aliases/tags) and the unset version column round-trip as None here.
    assert [c.value for c in entity_sheet[2]] == [
        entity.id,
        entity.name,
        entity.type.value,
        entity.description,
        None,
        None,
        entity.status,
        None,
    ]

    term_sheet = workbook["术语表"]
    term = bundle.terms["term_border"]
    assert [c.value for c in term_sheet[2]] == [
        term.id,
        term.canonical,
        ";".join(term.aliases),
        ";".join(term.forbidden),
        term.description,
    ]


def test_planning_workbook_empty_bundle_writes_headers_only(tmp_path: Path) -> None:
    workbook_bytes = render_planning_workbook(ContentBundle())
    (tmp_path / "empty.xlsx").write_bytes(workbook_bytes)
    workbook = openpyxl.load_workbook(tmp_path / "empty.xlsx")
    for name in ("任务表", "台词表", "实体表", "术语表"):
        assert workbook[name].max_row == 1  # header row only, no data rows


# --- N8.2 planning_notes.docx: manifest registration + key-content round trip -------------------


def test_planning_notes_registers_in_manifest_with_sha256(tmp_path: Path) -> None:
    import hashlib

    manifest = export_content_bundle(
        _bundle(), tmp_path, target_engine=EngineTarget.GENERIC, planning_notes=True
    )
    entry = next(f for f in manifest.files if f.kind == "planning_notes")
    assert entry.path == "planning_notes.docx"
    path = tmp_path / entry.path
    assert path.exists()
    assert entry.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_planning_notes_key_content_round_trips_through_the_projects_own_reader(
    tmp_path: Path,
) -> None:
    """Verified with ``content/documents.py``'s own ``docx_text`` — the same reader the project
    uses for manuscript/reference ingestion — not a second ad-hoc docx parser."""
    bundle = _bundle()
    data = render_planning_notes(bundle)
    text = docx_text(data)

    assert "策划笔记" in text
    assert "世界概览" in text and "任务卡" in text and "人物设定卡" in text
    assert bundle.quests["quest_patrol"].title in text
    assert bundle.quests["quest_patrol"].objective in text
    assert bundle.quests["quest_patrol"].stages[0].summary in text
    assert bundle.entities["npc_mara"].name in text
    assert bundle.entities["npc_mara"].description in text
    # fac_guard is a FACTION, not an NPC — only NPC entities get a character card.
    assert bundle.entities["fac_guard"].description not in text
    assert "审查报告" not in text  # no findings supplied -> section omitted


def test_planning_notes_review_findings_section_lists_every_finding() -> None:
    findings = [
        AuditFindingInput(
            rule_code="DIALOGUE_TREE_MISSING_TEXT_KEY",
            severity="warning",
            target_ref="dialogue_tree:dt_demo",
            message="节点 n0 缺少本地化键。",
        ),
    ]
    text = docx_text(render_planning_notes(_bundle(), review_findings=findings))
    assert "审查报告" in text
    assert "DIALOGUE_TREE_MISSING_TEXT_KEY" in text
    assert "节点 n0 缺少本地化键。" in text


def test_planning_notes_empty_world_degrades_gracefully() -> None:
    text = docx_text(render_planning_notes(ContentBundle()))
    assert "本世界暂无任务。" in text
    assert "本世界暂无 NPC 人物条目。" in text
