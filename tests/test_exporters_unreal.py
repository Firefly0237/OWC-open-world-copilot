"""UE DataTable exporter tests: closed round-trip through recognize, contract preflight, StringTable
CSV, C++ row-struct header, and committed contract fixtures.

The ``tests/fixtures/unreal/`` files are this exporter's own deterministic output committed as a
shape lock — they encode the documented UE DataTable contract (array-of-rows keyed by ``Name``,
``FDataTableRowHandle`` as ``{"RowName": ...}``, ``TArray`` as a JSON array, ``FText`` as a string).
They are synthesized from the Epic contract, not captured from a live editor (offline has no
engine); the round-trip assertion against ``recognize_engine_data`` proves they are consumable.
"""

from __future__ import annotations

import json
from pathlib import Path

from owcopilot.content.models import (
    Branch,
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueRef,
    DialogueTree,
    Effect,
    Entity,
    EntityType,
    LogicVar,
    LogicVarType,
    Quest,
    QuestLogic,
    QuestStage,
    Relation,
    Reward,
    StageLogic,
)
from owcopilot.exporters import (
    PreflightSeverity,
    UnrealExportError,
    export_unreal_datatables,
)
from owcopilot.exporters.unreal_datatable import UEType, _cpp_member_name
from owcopilot.recognize import recognize_engine_data

_FIXTURES = Path(__file__).parent / "fixtures" / "unreal"


def _round_trip_bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_mara": Entity(
                id="npc_mara", name="玛拉", type=EntityType.NPC, description="斥候。"
            ),
            "loc_town": Entity(id="loc_town", name="Town", type=EntityType.LOCATION),
        },
        quests={
            "quest_intro": Quest(id="quest_intro", title="序章"),
            "quest_patrol": Quest(
                id="quest_patrol",
                title="巡逻边境",
                giver_npc="npc_mara",
                location="loc_town",
                objective="在天黑前巡视边境线。",
                prerequisites=["quest_intro"],
                dialogue_refs=["dlg_hello"],
                timeline_order=1,
            ),
        },
        dialogues={
            "dlg_hello": DialogueRef(
                id="dlg_hello",
                text_key="dlg.hello",
                speaker_id="npc_mara",
                quest_id="quest_patrol",
                text="站住，报上名来！",
                locale="zh-CN",
            )
        },
        relations=[Relation(source="npc_mara", target="loc_town", kind="resides_in")],
    )


def _all_rows(export_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for name in ("Quests.json", "Entities.json", "Dialogues.json"):
        path = export_dir / name
        if path.exists():
            rows.extend(json.loads(path.read_text(encoding="utf-8")))
    return rows


# --- closed round-trip: export -> recognize reproduces the graph -------------------------------
def test_round_trip_through_recognize(tmp_path: Path) -> None:
    export_unreal_datatables(_round_trip_bundle(), tmp_path)
    plan = recognize_engine_data(_all_rows(tmp_path), dialect="ue")

    assert {e.id for e in plan.entities} == {
        "quest_patrol",
        "quest_intro",
        "npc_mara",
        "loc_town",
        "dlg_hello",
    }
    rels = {(r.source, r.target, r.kind) for r in plan.relations}
    assert ("quest_patrol", "npc_mara", "GiverNPC") in rels
    assert ("quest_patrol", "loc_town", "Location") in rels
    assert ("quest_patrol", "quest_intro", "Prerequisites") in rels
    assert ("quest_patrol", "dlg_hello", "DialogueRefs") in rels
    assert ("npc_mara", "loc_town", "resides_in") in rels  # entity relation-group column
    assert ("dlg_hello", "npc_mara", "Speaker") in rels
    assert ("dlg_hello", "quest_patrol", "Quest") in rels
    # 'Name'/'Type' are skipped by recognize, so a quest's classification never becomes a relation.
    mara = next(e for e in plan.entities if e.id == "npc_mara")
    assert mara.name == "玛拉" and mara.type == "npc"


def test_rowhandle_shape_and_stringtable_csv(tmp_path: Path) -> None:
    result = export_unreal_datatables(_round_trip_bundle(), tmp_path)

    quests = json.loads((tmp_path / "Quests.json").read_text(encoding="utf-8"))
    assert isinstance(quests, list)
    patrol = next(r for r in quests if r["Name"] == "quest_patrol")
    assert patrol["GiverNPC"] == {"RowName": "npc_mara"}  # single handle
    assert patrol["Prerequisites"] == [{"RowName": "quest_intro"}]  # TArray<RowHandle>
    assert patrol["DialogueRefs"] == [{"RowName": "dlg_hello"}]
    assert patrol["TimelineOrder"] == 1
    assert patrol["Type"] == "quest"

    dialogues = json.loads((tmp_path / "Dialogues.json").read_text(encoding="utf-8"))
    hello = next(r for r in dialogues if r["Name"] == "dlg_hello")
    # StringTable mode: the field carries the KEY, not the source string (no strings duplicated).
    assert hello["Text"] == "dlg.hello"
    assert "站住，报上名来！" not in (tmp_path / "Dialogues.json").read_text(encoding="utf-8")

    assert "Dialogues_strings.csv" in result.string_tables
    csv_text = (tmp_path / "Dialogues_strings.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0] == "Key,SourceString"
    assert "dlg.hello,站住，报上名来！" in csv_text


def test_inline_text_mode_writes_source_and_no_csv(tmp_path: Path) -> None:
    result = export_unreal_datatables(_round_trip_bundle(), tmp_path, text_mode="inline")
    dialogues = json.loads((tmp_path / "Dialogues.json").read_text(encoding="utf-8"))
    hello = next(r for r in dialogues if r["Name"] == "dlg_hello")
    assert hello["Text"] == "站住，报上名来！"  # inline FText source string
    assert result.string_tables == []
    assert not (tmp_path / "Dialogues_strings.csv").exists()


def test_string_handle_style_round_trips_in_universe(tmp_path: Path) -> None:
    export_unreal_datatables(_round_trip_bundle(), tmp_path, handle_style="string")
    quests = json.loads((tmp_path / "Quests.json").read_text(encoding="utf-8"))
    patrol = next(r for r in quests if r["Name"] == "quest_patrol")
    assert patrol["GiverNPC"] == "npc_mara"  # bare string, not a handle
    assert patrol["Prerequisites"] == ["quest_intro"]
    # In-universe bare strings still resolve to relations on re-recognize.
    plan = recognize_engine_data(_all_rows(tmp_path), dialect="ue")
    rels = {(r.source, r.target, r.kind) for r in plan.relations}
    assert ("quest_patrol", "npc_mara", "GiverNPC") in rels


def test_rowhandle_round_trips_dangling_but_string_does_not(tmp_path: Path) -> None:
    bundle = ContentBundle(
        quests={"q": Quest(id="q", title="Q", giver_npc="npc_ghost")},  # ghost not in bundle
    )
    handle_dir = tmp_path / "handle"
    string_dir = tmp_path / "string"
    export_unreal_datatables(bundle, handle_dir, force=True)
    export_unreal_datatables(bundle, string_dir, handle_style="string", force=True)

    handle_rels = {
        (r.source, r.target, r.kind) for r in recognize_engine_data(_all_rows(handle_dir)).relations
    }
    string_rels = {
        (r.source, r.target, r.kind) for r in recognize_engine_data(_all_rows(string_dir)).relations
    }
    # RowHandle is an explicit reference -> relation reproduced even for an out-of-universe target.
    assert ("q", "npc_ghost", "GiverNPC") in handle_rels
    # Bare string only resolves when the target is a known id, so the dangling one is dropped.
    assert ("q", "npc_ghost", "GiverNPC") not in string_rels


# --- contract preflight ------------------------------------------------------------------------
def test_preflight_blocks_name_collision(tmp_path: Path) -> None:
    bad_map = {"quests": {"DisplayName": "Name"}}
    try:
        export_unreal_datatables(_round_trip_bundle(), tmp_path, row_struct_map=bad_map)
        raise AssertionError("expected UnrealExportError")
    except UnrealExportError as err:
        assert any(i.code == "name_collision" for i in err.report.errors)
    assert not (tmp_path / "Quests.json").exists()  # nothing written on a blocking failure

    forced = export_unreal_datatables(
        _round_trip_bundle(), tmp_path, row_struct_map=bad_map, force=True
    )
    assert any(i.code == "name_collision" for i in forced.preflight.errors)
    assert (tmp_path / "Quests.json").exists()  # force writes despite the error


def test_preflight_blocks_space_in_name(tmp_path: Path) -> None:
    try:
        export_unreal_datatables(
            _round_trip_bundle(), tmp_path, row_struct_map={"quests": {"DisplayName": "Bad Name"}}
        )
        raise AssertionError("expected UnrealExportError")
    except UnrealExportError as err:
        assert any(i.code == "space_in_name" for i in err.report.errors)


def test_preflight_warns_dangling_without_blocking(tmp_path: Path) -> None:
    bundle = ContentBundle(quests={"q": Quest(id="q", title="Q", giver_npc="npc_ghost")})
    result = export_unreal_datatables(bundle, tmp_path)  # warning, not error -> succeeds
    warnings = result.preflight.warnings
    assert any(i.code == "dangling_reference" and i.field == "GiverNPC" for i in warnings)
    assert result.preflight.ok


def test_preflight_stringtable_literal_note_only_in_stringtable_mode(tmp_path: Path) -> None:
    st = export_unreal_datatables(_round_trip_bundle(), tmp_path / "st")
    assert any(i.code == "stringtable_literal_unverified" for i in st.preflight.warnings)
    inline = export_unreal_datatables(_round_trip_bundle(), tmp_path / "inline", text_mode="inline")
    assert not any(i.code == "stringtable_literal_unverified" for i in inline.preflight.warnings)


# --- C++ row-struct header ---------------------------------------------------------------------
def test_cpp_header_declares_structs_and_types(tmp_path: Path) -> None:
    export_unreal_datatables(_round_trip_bundle(), tmp_path)
    header = (tmp_path / "OWCopilotRows.h").read_text(encoding="utf-8")
    assert "struct FQuestRow : public FTableRowBase" in header
    assert "FText DisplayName;" in header
    assert "FDataTableRowHandle GiverNPC;" in header
    assert "TArray<FDataTableRowHandle> Prerequisites;" in header
    assert "int32 TimelineOrder;" in header
    assert "TArray<FDataTableRowHandle> resides_in;" in header  # data-driven relation column


def test_cpp_member_name_bool_prefix_rule() -> None:
    # UE strips the leading 'b' from a bool's export name, so the member re-adds it.
    assert _cpp_member_name("IsActive", UEType.BOOL) == "bIsActive"
    assert _cpp_member_name("DisplayName", UEType.FTEXT) == "DisplayName"


def test_row_struct_map_renames_json_keys_and_header(tmp_path: Path) -> None:
    export_unreal_datatables(
        _round_trip_bundle(), tmp_path, row_struct_map={"quests": {"DisplayName": "sDisplayName"}}
    )
    quests = json.loads((tmp_path / "Quests.json").read_text(encoding="utf-8"))
    patrol = next(r for r in quests if r["Name"] == "quest_patrol")
    assert "sDisplayName" in patrol and "DisplayName" not in patrol
    header = (tmp_path / "OWCopilotRows.h").read_text(encoding="utf-8")
    assert "FText sDisplayName;" in header


# --- result manifest ---------------------------------------------------------------------------
def test_result_files_carry_kinds_and_valid_hashes(tmp_path: Path) -> None:
    import hashlib

    result = export_unreal_datatables(_round_trip_bundle(), tmp_path)
    kinds = {f.kind for f in result.files}
    assert {"datatable", "string_table", "row_struct_header", "preflight"} <= kinds
    for item in result.files:
        raw = (tmp_path / item.path).read_bytes()
        assert item.sha256 == hashlib.sha256(raw).hexdigest()


# --- committed contract fixtures (shape lock + round-trip) -------------------------------------
def test_contract_fixtures_match_export_and_round_trip(tmp_path: Path) -> None:
    export_unreal_datatables(_round_trip_bundle(), tmp_path)
    for name in ("Quests.json", "Entities.json", "Dialogues.json"):
        produced = (tmp_path / name).read_text(encoding="utf-8")
        golden = (_FIXTURES / name).read_text(encoding="utf-8")
        assert produced == golden, f"{name} drifted from the committed UE contract fixture"

    # The committed fixtures must themselves be consumable by the reader.
    fixture_rows: list[dict] = []
    for name in ("Quests.json", "Entities.json", "Dialogues.json"):
        fixture_rows.extend(json.loads((_FIXTURES / name).read_text(encoding="utf-8")))
    plan = recognize_engine_data(fixture_rows, dialect="ue")
    assert ("quest_patrol", "npc_mara", "GiverNPC") in {
        (r.source, r.target, r.kind) for r in plan.relations
    }


def test_preflight_severity_enum_values() -> None:
    assert PreflightSeverity.ERROR.value == "error"
    assert PreflightSeverity.WARNING.value == "warning"


# --- full-fidelity logic / stage / reward / dialogue-tree sub-tables ---------------------------
#
# The three original tables are a lossy projection: quest logic (variables/preconditions/effects/
# branches), stages, rewards and dialogue trees were dropped. These tests lock the additive
# sub-tables that carry them, a field census that fails when a model gains an unlanded field, and a
# round-trip that reconstructs the source structures from the exported rows.

_SEP = "::"


def _full_logic_bundle() -> ContentBundle:
    """A quest with a complete logic layer + a branching dialogue tree — every nested field set."""
    return ContentBundle(
        entities={
            "npc_mara": Entity(id="npc_mara", name="玛拉", type=EntityType.NPC),
            "fac_guild": Entity(id="fac_guild", name="公会", type=EntityType.FACTION),
            "loc_gate": Entity(id="loc_gate", name="城门", type=EntityType.LOCATION),
            "item_key": Entity(id="item_key", name="钥匙", type=EntityType.ITEM),
        },
        quests={
            "q_gate": Quest(
                id="q_gate",
                title="门后",
                giver_npc="npc_mara",
                stages=[
                    QuestStage(
                        id="s1", summary="谈话", location="loc_gate", required_entities=["npc_mara"]
                    ),
                    QuestStage(id="s2", summary="进门"),
                ],
                rewards=[
                    Reward(kind="item", value="item_key", amount=1),
                    Reward(kind="xp", value="100"),
                ],
                logic=QuestLogic(
                    variables=[
                        LogicVar(id="helped", type=LogicVarType.BOOL, default=False),
                        LogicVar(
                            id="mood",
                            type=LogicVarType.ENUM,
                            default="neutral",
                            enum_values=["neutral", "happy"],
                        ),
                        LogicVar(id="gold", type=LogicVarType.INT, default=0),
                    ],
                    precondition="quest:q_intro.done",
                    stage_logic=[
                        StageLogic(
                            stage_id="s2",
                            precondition="helped",
                            effects_on_complete=[
                                Effect(var="helped", op="set", value=True),
                                Effect(var="gold", op="inc", value=5),
                            ],
                        )
                    ],
                    branches=[
                        Branch(
                            id="b_help",
                            from_stage="s1",
                            to_stage="s2",
                            condition="helped",
                            effects=[Effect(var="rep:fac_guild", op="inc", value=2)],
                        ),
                        Branch(id="b_end", from_stage="s2", to_stage="", outcome="done"),
                    ],
                    unlocks=["q_next"],
                ),
            ),
        },
        dialogue_trees={
            "dlg_gate": DialogueTree(
                id="dlg_gate",
                title="门口对话",
                quest_id="q_gate",
                participants=["npc_mara"],
                root_node="n0",
                tags=["intro"],
                metadata={"mood": "tense"},
                nodes={
                    "n0": DialogueNode(
                        id="n0",
                        speaker_id="npc_mara",
                        text="要帮忙吗？",
                        choices=[
                            DialogueChoice(text="(冷漠离开)", next_node="n_end", condition="cold"),
                            DialogueChoice(
                                text="我来帮你。",
                                next_node="n_end",
                                effects=[Effect(var="helped", op="set", value=True)],
                            ),
                        ],
                    ),
                    "n_end": DialogueNode(id="n_end", text="……"),
                },
            ),
        },
    )


def _read_table(export_dir: Path, name: str) -> list[dict]:
    path = export_dir / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _by_name(rows: list[dict]) -> dict[str, dict]:
    return {r["Name"]: r for r in rows}


def _deref(handle: object) -> str:
    """A RowHandle {"RowName": X} (or a bare string in handle_style='string') -> the target id."""
    return handle["RowName"] if isinstance(handle, dict) else str(handle)


def _unprefix(composite: str, parent: str) -> str:
    """Recover the child id from a parent<sep>child composite RowName (the parent is known)."""
    prefix = parent + _SEP
    assert composite.startswith(prefix), (composite, parent)
    return composite[len(prefix) :]


def _decode_effects(cell: str | None) -> list[Effect]:
    return [Effect(**d) for d in json.loads(cell)] if cell else []


def test_full_fidelity_new_tables_written(tmp_path: Path) -> None:
    result = export_unreal_datatables(_full_logic_bundle(), tmp_path)
    expected = {
        "QuestStages.json",
        "QuestLogicVars.json",
        "QuestLogicBranches.json",
        "QuestRewards.json",
        "QuestLogic.json",
        "DialogueTrees.json",
        "DialogueTreeNodes.json",
        "DialogueTreeChoices.json",
    }
    assert expected <= set(result.tables)
    # every new table is the documented DataTable shape (array of Name-keyed rows) so the shape-
    # driven plugin discovery imports it with no code change.
    for name in expected:
        rows = _read_table(tmp_path, name)
        assert rows and all(isinstance(r.get("Name"), str) and r["Name"] for r in rows)


def test_every_logic_field_has_a_landing() -> None:
    """Census: every field of every nested logic/stage/reward/dialogue-tree model must map to an
    export landing. A new (or removed) model field breaks this — no silent drop, no stale map."""
    landing: dict[type, set[str]] = {
        QuestLogic: {"variables", "precondition", "stage_logic", "branches", "unlocks"},
        LogicVar: {"id", "name", "type", "default", "enum_values"},
        Effect: {"var", "op", "value"},
        StageLogic: {"stage_id", "precondition", "effects_on_complete"},
        Branch: {"id", "from_stage", "condition", "to_stage", "outcome", "effects"},
        QuestStage: {"id", "summary", "location", "required_entities"},
        Reward: {"kind", "value", "amount"},
        DialogueTree: {
            "id",
            "title",
            "quest_id",
            "participants",
            "root_node",
            "nodes",
            "tags",
            "metadata",
            "origin",
            "source_ref",
            "review_status",
        },
        DialogueNode: {"id", "speaker_id", "text", "choices", "next_node", "text_key"},
        DialogueChoice: {"text", "next_node", "condition", "effects", "text_key"},
    }
    for model, mapped in landing.items():
        actual = set(model.model_fields)
        assert actual == mapped, (
            f"{model.__name__} fields drifted from the UE export landing map "
            f"(unlanded/stale: {actual ^ mapped}); update unreal_datatable.py and this map"
        )


def test_round_trip_reconstructs_quest_logic_from_tables(tmp_path: Path) -> None:
    """Export -> rebuild the quest's stages / logic / rewards from the sub-tables, equals source."""
    bundle = _full_logic_bundle()
    export_unreal_datatables(bundle, tmp_path)
    source = bundle.quests["q_gate"]

    stage_rows = _read_table(tmp_path, "QuestStages.json")
    by_stage = {r["StageId"]: r for r in stage_rows if _deref(r["Quest"]) == "q_gate"}
    # stages (QuestStage part)
    restored_stages = {
        sid: QuestStage(
            id=r["StageId"],
            summary=r.get("Summary", ""),
            location=(_deref(r["Location"]) if "Location" in r else None),
            required_entities=[_deref(h) for h in r.get("RequiredEntities", [])],
        )
        for sid, r in by_stage.items()
    }
    assert restored_stages == {s.id: s for s in source.stages}
    # stage logic (precondition + on-complete effects) folded into the same rows
    restored_stage_logic = {
        r["StageId"]: StageLogic(
            stage_id=r["StageId"],
            precondition=r.get("Precondition", ""),
            effects_on_complete=_decode_effects(r.get("EffectsOnComplete")),
        )
        for r in stage_rows
        if r.get("Precondition") or r.get("EffectsOnComplete")
    }
    assert source.logic is not None
    assert restored_stage_logic == {sl.stage_id: sl for sl in source.logic.stage_logic}

    # variables (typed default round-trips through the JSON scalar)
    var_rows = _read_table(tmp_path, "QuestLogicVars.json")
    restored_vars = {
        r["VarId"]: LogicVar(
            id=r["VarId"],
            name=r.get("VarName", ""),
            type=LogicVarType(r["VarType"]),
            default=json.loads(r["DefaultValue"]),
            enum_values=r.get("EnumValues", []),
        )
        for r in var_rows
    }
    assert restored_vars == {v.id: v for v in source.logic.variables}

    # branches (from/to stage recovered from the composite RowHandles + effects)
    branch_rows = _read_table(tmp_path, "QuestLogicBranches.json")
    restored_branches = {
        r["BranchId"]: Branch(
            id=r["BranchId"],
            from_stage=(_unprefix(_deref(r["FromStage"]), "q_gate") if "FromStage" in r else ""),
            to_stage=(_unprefix(_deref(r["ToStage"]), "q_gate") if "ToStage" in r else ""),
            condition=r.get("Condition", ""),
            outcome=r.get("Outcome", ""),
            effects=_decode_effects(r.get("Effects")),
        )
        for r in branch_rows
    }
    assert restored_branches == {b.id: b for b in source.logic.branches}

    # quest-level logic header (precondition gate + unlocks)
    header = _by_name(_read_table(tmp_path, "QuestLogic.json"))["q_gate::logic"]
    assert header["Precondition"] == source.logic.precondition
    assert [_deref(h) for h in header["Unlocks"]] == source.logic.unlocks

    # rewards (ordered by Ordinal)
    reward_rows = sorted(_read_table(tmp_path, "QuestRewards.json"), key=lambda r: r["Ordinal"])
    restored_rewards = [
        Reward(kind=r["RewardKind"], value=r["RewardValue"], amount=r.get("Amount"))
        for r in reward_rows
    ]
    assert restored_rewards == source.rewards


def test_round_trip_reconstructs_dialogue_tree_from_tables(tmp_path: Path) -> None:
    bundle = _full_logic_bundle()
    export_unreal_datatables(bundle, tmp_path)
    source = bundle.dialogue_trees["dlg_gate"]

    tree_row = _by_name(_read_table(tmp_path, "DialogueTrees.json"))["dlg_gate"]
    assert tree_row["Title"] == source.title
    assert _deref(tree_row["Quest"]) == source.quest_id
    assert [_deref(h) for h in tree_row["Participants"]] == source.participants
    assert _unprefix(_deref(tree_row["RootNode"]), "dlg_gate") == source.root_node
    assert tree_row["Tags"] == source.tags
    assert json.loads(tree_row["Metadata"]) == source.metadata

    # nodes
    node_rows = [r for r in _read_table(tmp_path, "DialogueTreeNodes.json")]
    restored_nodes = {}
    for r in node_rows:
        tree_id = _deref(r["Tree"])
        restored_nodes[r["NodeId"]] = DialogueNode(
            id=r["NodeId"],
            speaker_id=(_deref(r["Speaker"]) if "Speaker" in r else None),
            text=r.get("Text", ""),
            next_node=(_unprefix(_deref(r["NextNode"]), tree_id) if "NextNode" in r else None),
        )
    # choices grouped back onto their node, ordered by Ordinal (matches the walk selector index)
    choice_rows = sorted(
        _read_table(tmp_path, "DialogueTreeChoices.json"), key=lambda r: r["Ordinal"]
    )
    for r in choice_rows:
        node_composite = _deref(r["Node"])
        tree_id, node_id = node_composite.split(_SEP, 1)
        restored_nodes[node_id].choices.append(
            DialogueChoice(
                text=r["Text"],
                next_node=(_unprefix(_deref(r["NextNode"]), tree_id) if "NextNode" in r else None),
                condition=r.get("Condition", ""),
                effects=_decode_effects(r.get("Effects")),
            )
        )
    assert restored_nodes == source.nodes


def test_effects_encoded_with_csharp_effectdto_vocabulary(tmp_path: Path) -> None:
    """Effects serialize as a JSON array of {var, op, value} — the exact keys the C# EffectDto /
    content_bundle.json use — so a UE-side parser rebuilds the same effect the simulator runs."""
    export_unreal_datatables(_full_logic_bundle(), tmp_path)
    s2 = next(r for r in _read_table(tmp_path, "QuestStages.json") if r["StageId"] == "s2")
    decoded = json.loads(s2["EffectsOnComplete"])
    assert decoded[0] == {"var": "helped", "op": "set", "value": True}
    assert all(set(e) == {"var", "op", "value"} for e in decoded)


def test_existing_quest_row_structure_unchanged_by_logic(tmp_path: Path) -> None:
    """Quest-level logic rides its own tables; the QuestRow columns stay exactly the legacy set, so
    an existing UE project's Quest DataTable is never broken by the added fidelity."""
    export_unreal_datatables(_full_logic_bundle(), tmp_path)
    quest_row = _by_name(_read_table(tmp_path, "Quests.json"))["q_gate"]
    assert set(quest_row) <= {
        "Name",
        "Type",
        "DisplayName",
        "Objective",
        "GiverNPC",
        "Location",
        "Prerequisites",
        "DialogueRefs",
        "TimelineOrder",
    }
    # the logic layer did NOT leak columns into the quest row
    assert not {"Precondition", "Unlocks", "Stages", "Rewards", "Logic"} & set(quest_row)


def test_new_structs_declared_in_cpp_header(tmp_path: Path) -> None:
    export_unreal_datatables(_full_logic_bundle(), tmp_path)
    header = (tmp_path / "OWCopilotRows.h").read_text(encoding="utf-8")
    for struct in (
        "FQuestStageRow",
        "FQuestLogicVarRow",
        "FQuestLogicBranchRow",
        "FQuestRewardRow",
        "FQuestLogicRow",
        "FDialogueTreeRow",
        "FDialogueTreeNodeRow",
        "FDialogueTreeChoiceRow",
    ):
        assert f"struct {struct} : public FTableRowBase" in header
    # string-column families and the array of literal strings are typed as declared
    assert "FString EffectsOnComplete;" in header
    assert "TArray<FString> EnumValues;" in header
    assert "FDataTableRowHandle FromStage;" in header  # branch graph pointer


def test_new_tables_round_trip_through_recognize(tmp_path: Path) -> None:
    """The sub-tables are valid engine data: recognize lifts the branch/dialogue graph back into
    relations (the pointer graph the reachability audit walks)."""
    export_unreal_datatables(_full_logic_bundle(), tmp_path)
    rows: list[dict] = []
    for name in ("QuestStages.json", "QuestLogicBranches.json", "DialogueTreeChoices.json"):
        rows.extend(_read_table(tmp_path, name))
    plan = recognize_engine_data(rows, dialect="ue")
    rels = {(r.source, r.target, r.kind) for r in plan.relations}
    # a stage points back to its quest; a branch points from->to stage; a choice points to its node
    assert ("q_gate::s1", "q_gate", "Quest") in rels
    assert ("q_gate::branch::b_help", "q_gate::s1", "FromStage") in rels
    assert ("q_gate::branch::b_help", "q_gate::s2", "ToStage") in rels
    assert ("dlg_gate::n0::choice::1", "dlg_gate::n_end", "NextNode") in rels


# --- A1 (localization remediation): dialogue-tree text_key opts into the StringTable -----------
def _bundle_with_keyed_dialogue_tree() -> ContentBundle:
    """One node/choice opts into ``text_key``; a second node stays key-less (pre-existing style) so
    the same export proves both the new opt-in path and the untouched fallback at once."""
    return ContentBundle(
        entities={"npc_mara": Entity(id="npc_mara", name="玛拉", type=EntityType.NPC)},
        dialogue_trees={
            "dt": DialogueTree(
                id="dt",
                root_node="n0",
                nodes={
                    "n0": DialogueNode(
                        id="n0",
                        speaker_id="npc_mara",
                        text="你是谁？",
                        text_key="dt.n0",
                        choices=[
                            DialogueChoice(text="我来帮你。", text_key="dt.n0.c0", next_node="n1")
                        ],
                    ),
                    "n1": DialogueNode(id="n1", text="那就跟我来。"),  # no text_key
                },
            )
        },
    )


def test_dialogue_tree_text_key_opts_into_stringtable(tmp_path: Path) -> None:
    result = export_unreal_datatables(_bundle_with_keyed_dialogue_tree(), tmp_path)

    nodes = json.loads((tmp_path / "DialogueTreeNodes.json").read_text(encoding="utf-8"))
    by_id = {r["NodeId"]: r for r in nodes}
    assert by_id["n0"]["Text"] == "dt.n0"  # keyed node: the field carries the KEY, not the string
    assert by_id["n1"]["Text"] == "那就跟我来。"  # key-less node: untouched SCALAR fallback

    choices = json.loads((tmp_path / "DialogueTreeChoices.json").read_text(encoding="utf-8"))
    assert choices[0]["Text"] == "dt.n0.c0"

    assert "DialogueTreeNodes_strings.csv" in result.string_tables
    node_csv = (tmp_path / "DialogueTreeNodes_strings.csv").read_text(encoding="utf-8")
    assert "dt.n0,你是谁？" in node_csv
    assert "那就跟我来。" not in node_csv  # key-less text never leaks into the StringTable CSV

    assert "DialogueTreeChoices_strings.csv" in result.string_tables
    choice_csv = (tmp_path / "DialogueTreeChoices_strings.csv").read_text(encoding="utf-8")
    assert "dt.n0.c0,我来帮你。" in choice_csv


def test_dialogue_tree_text_key_inline_mode_ignores_key(tmp_path: Path) -> None:
    """Inline mode always writes the source string, exactly like the pre-A1 SCALAR field did —
    text_key is a stringtable-mode-only concept."""
    export_unreal_datatables(_bundle_with_keyed_dialogue_tree(), tmp_path, text_mode="inline")
    nodes = json.loads((tmp_path / "DialogueTreeNodes.json").read_text(encoding="utf-8"))
    by_id = {r["NodeId"]: r for r in nodes}
    assert by_id["n0"]["Text"] == "你是谁？"
    assert by_id["n1"]["Text"] == "那就跟我来。"
    assert not (tmp_path / "DialogueTreeNodes_strings.csv").exists()


def test_dialogue_tree_without_text_key_keeps_pre_a1_byte_shape(tmp_path: Path) -> None:
    """Backward-compat proof: a tree with NO text_key anywhere (the ``_full_logic_bundle`` fixture
    every other logic/dialogue-tree test in this file uses) keeps the exact pre-A1 shape — raw text
    inline in the DataTable row, and no StringTable CSV for the dialogue-tree tables at all."""
    bundle = _full_logic_bundle()
    result = export_unreal_datatables(bundle, tmp_path)

    nodes = json.loads((tmp_path / "DialogueTreeNodes.json").read_text(encoding="utf-8"))
    source_tree = bundle.dialogue_trees["dlg_gate"]
    assert {r["Text"] for r in nodes} == {n.text for n in source_tree.nodes.values()}

    choices = json.loads((tmp_path / "DialogueTreeChoices.json").read_text(encoding="utf-8"))
    source_choice_texts = {c.text for n in source_tree.nodes.values() for c in n.choices}
    assert {r["Text"] for r in choices} == source_choice_texts

    assert "DialogueTreeNodes_strings.csv" not in result.string_tables
    assert "DialogueTreeChoices_strings.csv" not in result.string_tables
    assert not (tmp_path / "DialogueTreeNodes_strings.csv").exists()
    assert not (tmp_path / "DialogueTreeChoices_strings.csv").exists()


# --- B1/B2 (blueprint_friendly demo variant): DNF condition clauses + structured effects --------
#
# 求职导向整改计划.md §B1/§B2: a UE Blueprint has neither an expression parser nor a JSON-string
# decoder, so the demo variant additionally compiles Condition to a flat {Var,Op,Value} clause
# array and structures Effects the same way — additive only, and only on DialogueTreeNodes /
# DialogueTreeChoices / QuestLogicVars. The hard requirement this file must also prove: with
# blueprint_friendly left at its default (False), NOTHING changes — see
# test_blueprint_friendly_default_is_byte_identical_to_omitting_it below, and every pre-existing
# test above (none of which pass blueprint_friendly) already re-proves the default path untouched.


def _blueprint_demo_bundle() -> ContentBundle:
    """One dialogue node with three choices exercising the three DNF outcomes: a flat AND (the
    common case, gets ConditionClauses), an OR (compiles but is not flat, so
    ConditionCompilable=False), and no condition at all (vacuously true, zero clauses)."""
    return ContentBundle(
        entities={"npc_grandma": Entity(id="npc_grandma", name="贾母", type=EntityType.NPC)},
        quests={
            "q_demo": Quest(
                id="q_demo",
                title="演示",
                logic=QuestLogic(
                    variables=[
                        LogicVar(id="grandma_favor", type=LogicVarType.INT, default=0),
                        LogicVar(id="helped", type=LogicVarType.BOOL, default=False),
                    ]
                ),
            )
        },
        dialogue_trees={
            "dt_demo": DialogueTree(
                id="dt_demo",
                quest_id="q_demo",
                root_node="n0",
                nodes={
                    "n0": DialogueNode(
                        id="n0",
                        speaker_id="npc_grandma",
                        text="你读过书吗？",
                        choices=[
                            DialogueChoice(
                                text="flat_and",
                                condition="helped and grandma_favor >= 1",
                                effects=[Effect(var="grandma_favor", op="inc", value=1)],
                                next_node="n1",
                            ),
                            DialogueChoice(
                                text="needs_or",
                                condition="helped or grandma_favor >= 5",
                                next_node="n1",
                            ),
                            DialogueChoice(text="no_gate", next_node="n1"),
                        ],
                    ),
                    "n1": DialogueNode(id="n1", text="……"),
                },
            )
        },
    )


def _choice_row(rows: list[dict], text: str) -> dict:
    return next(r for r in rows if r["Text"] == text)


def test_blueprint_friendly_default_is_byte_identical_to_omitting_it(tmp_path: Path) -> None:
    """blueprint_friendly's default (False) must produce EXACTLY the same bytes as not passing the
    argument at all — the whole point of the knob is that adopting it costs existing callers
    nothing."""
    omitted = tmp_path / "omitted"
    explicit = tmp_path / "explicit_false"
    export_unreal_datatables(_full_logic_bundle(), omitted)
    export_unreal_datatables(_full_logic_bundle(), explicit, blueprint_friendly=False)
    for path in sorted(omitted.iterdir()):
        assert path.read_bytes() == (explicit / path.name).read_bytes(), path.name


def test_blueprint_friendly_only_touches_the_three_demo_tables(tmp_path: Path) -> None:
    """Every other table's bytes are unaffected by blueprint_friendly=True (§3: 其余表保持原样)."""
    off_dir, on_dir = tmp_path / "off", tmp_path / "on"
    export_unreal_datatables(_full_logic_bundle(), off_dir)
    export_unreal_datatables(_full_logic_bundle(), on_dir, blueprint_friendly=True)
    untouched = {
        "Quests.json",
        "Entities.json",
        "QuestStages.json",
        "QuestLogicBranches.json",
        "QuestRewards.json",
        "QuestLogic.json",
        "DialogueTrees.json",
    }
    for name in untouched:
        assert (off_dir / name).read_bytes() == (on_dir / name).read_bytes(), name
    # the 3 in-scope tables DO differ (reference style + new columns), proving the toggle did fire
    assert (off_dir / "QuestLogicVars.json").read_bytes() != (
        on_dir / "QuestLogicVars.json"
    ).read_bytes()
    assert (off_dir / "DialogueTreeChoices.json").read_bytes() != (
        on_dir / "DialogueTreeChoices.json"
    ).read_bytes()


def test_blueprint_friendly_condition_clauses_for_flat_and(tmp_path: Path) -> None:
    result = export_unreal_datatables(_blueprint_demo_bundle(), tmp_path, blueprint_friendly=True)
    choices = json.loads((tmp_path / "DialogueTreeChoices.json").read_text(encoding="utf-8"))
    row = _choice_row(choices, "flat_and")
    assert row["ConditionCompilable"] is True
    assert row["ConditionClauses"] == [
        {"Var": "helped", "Op": "==", "Value": "true"},
        {"Var": "grandma_favor", "Op": ">=", "Value": "1"},
    ]
    # the original string column is untouched, right alongside the new ones
    assert row["Condition"] == "helped and grandma_favor >= 1"
    assert result.preflight.ok


def test_blueprint_friendly_condition_needs_or_is_not_compilable_flat(tmp_path: Path) -> None:
    export_unreal_datatables(_blueprint_demo_bundle(), tmp_path, blueprint_friendly=True)
    choices = json.loads((tmp_path / "DialogueTreeChoices.json").read_text(encoding="utf-8"))
    row = _choice_row(choices, "needs_or")
    assert row["ConditionCompilable"] is False
    assert row["ConditionClauses"] == []
    assert row["Condition"] == "helped or grandma_favor >= 5"  # source preserved as fallback


def test_blueprint_friendly_no_condition_is_vacuously_compilable(tmp_path: Path) -> None:
    export_unreal_datatables(_blueprint_demo_bundle(), tmp_path, blueprint_friendly=True)
    choices = json.loads((tmp_path / "DialogueTreeChoices.json").read_text(encoding="utf-8"))
    row = _choice_row(choices, "no_gate")
    assert row["ConditionCompilable"] is True
    assert row["ConditionClauses"] == []
    assert "Condition" not in row  # empty string is SCALAR-omitted, same as always


def test_blueprint_friendly_effects_structured_added_alongside_json_string(tmp_path: Path) -> None:
    export_unreal_datatables(_blueprint_demo_bundle(), tmp_path, blueprint_friendly=True)
    choices = json.loads((tmp_path / "DialogueTreeChoices.json").read_text(encoding="utf-8"))
    row = _choice_row(choices, "flat_and")
    assert row["EffectsStructured"] == [{"Var": "grandma_favor", "Op": "inc", "Value": "1"}]
    # the original JSON-string column is untouched
    assert json.loads(row["Effects"]) == [{"var": "grandma_favor", "op": "inc", "value": 1}]

    no_gate = _choice_row(choices, "no_gate")
    assert no_gate["EffectsStructured"] == []
    assert "Effects" not in no_gate  # unchanged: empty effects list is SCALAR-omitted


def test_blueprint_friendly_references_are_bare_strings_on_demo_tables(tmp_path: Path) -> None:
    """Reference fields on the 3 demo tables serialize as bare row-name strings — no
    ``{"RowName": ...}`` handle — regardless of the (default) ``handle_style``."""
    export_unreal_datatables(_blueprint_demo_bundle(), tmp_path, blueprint_friendly=True)

    nodes = json.loads((tmp_path / "DialogueTreeNodes.json").read_text(encoding="utf-8"))
    n0 = next(r for r in nodes if r["NodeId"] == "n0")
    n1 = next(r for r in nodes if r["NodeId"] == "n1")
    assert n0["Tree"] == "dt_demo"
    assert n0["Speaker"] == "npc_grandma"
    assert n1["Tree"] == "dt_demo"  # n1 has no speaker/next_node of its own; check via another node

    choices = json.loads((tmp_path / "DialogueTreeChoices.json").read_text(encoding="utf-8"))
    row = _choice_row(choices, "flat_and")
    assert row["Node"] == "dt_demo::n0"
    assert row["NextNode"] == "dt_demo::n1"

    var_rows = json.loads((tmp_path / "QuestLogicVars.json").read_text(encoding="utf-8"))
    grandma_favor = next(r for r in var_rows if r["VarId"] == "grandma_favor")
    assert grandma_favor["Quest"] == "q_demo"  # bare string, not {"RowName": "q_demo"}


def test_blueprint_friendly_speaker_name_resolved_from_entity(tmp_path: Path) -> None:
    """``SpeakerName`` (demo-only column on ``DialogueTreeNodes``) resolves the ``Speaker`` id to
    the entity's display name, so the Blueprint dialogue UI can show "贾母" instead of the raw id
    without a second table lookup. A node with no speaker at all gets an empty string."""
    export_unreal_datatables(_blueprint_demo_bundle(), tmp_path, blueprint_friendly=True)
    nodes = json.loads((tmp_path / "DialogueTreeNodes.json").read_text(encoding="utf-8"))
    n0 = next(r for r in nodes if r["NodeId"] == "n0")
    n1 = next(r for r in nodes if r["NodeId"] == "n1")
    assert n0["Speaker"] == "npc_grandma"
    assert n0["SpeakerName"] == "贾母"
    assert n1["SpeakerName"] == ""  # n1 has no speaker_id at all


def test_blueprint_friendly_speaker_name_empty_for_dangling_speaker(tmp_path: Path) -> None:
    """A ``Speaker`` id with no matching entity in the exported universe leaves ``SpeakerName``
    empty rather than falling back to the raw id — ``DialogueTreeUnknownSpeakerRule`` already
    flags this case during audit, so the export layer must not repeat that judgment."""
    bundle = _blueprint_demo_bundle()
    bundle.dialogue_trees["dt_demo"].nodes["n0"].speaker_id = "ghost_npc"
    export_unreal_datatables(bundle, tmp_path, blueprint_friendly=True)
    nodes = json.loads((tmp_path / "DialogueTreeNodes.json").read_text(encoding="utf-8"))
    n0 = next(r for r in nodes if r["NodeId"] == "n0")
    assert n0["Speaker"] == "ghost_npc"
    assert n0["SpeakerName"] == ""


def test_blueprint_friendly_speaker_name_absent_by_default(tmp_path: Path) -> None:
    """With ``blueprint_friendly`` left at its default, ``DialogueTreeNodes`` never gains a
    ``SpeakerName`` column at all — it is strictly additive to the demo variant."""
    export_unreal_datatables(_blueprint_demo_bundle(), tmp_path)
    nodes = json.loads((tmp_path / "DialogueTreeNodes.json").read_text(encoding="utf-8"))
    assert all("SpeakerName" not in r for r in nodes)


def test_blueprint_friendly_handle_style_still_governs_other_tables(tmp_path: Path) -> None:
    """handle_style="string" for a non-demo table (Quests) still behaves normally when
    blueprint_friendly is also on — the two knobs are independent outside the 3 demo tables."""
    export_unreal_datatables(
        _blueprint_demo_bundle(), tmp_path, blueprint_friendly=True, handle_style="rowhandle"
    )
    # q_demo has no GiverNPC/Location set in this fixture, so nothing to assert on Quests.json;
    # instead prove the dialogue tree's own "Quest" back-reference obeys handle_style, unlike the 3
    # forced-string demo tables. DialogueTrees.json is NOT in the demo-table set.
    trees = json.loads((tmp_path / "DialogueTrees.json").read_text(encoding="utf-8"))
    tree = next(r for r in trees if r["Name"] == "dt_demo")
    assert tree["Quest"] == {"RowName": "q_demo"}  # untouched table: still a RowHandle
