from __future__ import annotations

import base64
import io
import json

from openpyxl import Workbook

from owcopilot.app.actions import (
    delete_mapping_template_action,
    list_mapping_templates_action,
    recognize_apply_plan_action,
    recognize_content_action,
    recognize_import_action,
    save_mapping_template_action,
)
from owcopilot.cli.main import main
from owcopilot.content.models import ContentBundle, Entity, EntityType, Origin, ReviewStatus
from owcopilot.content.store import ContentStore
from owcopilot.recognize import (
    ColumnMapping,
    build_llm_relation_proposer,
    diff_against_canon,
    evidence_grounded,
    infer_table_mapping,
    plan_to_bundle,
    propose_relations_guarded,
    recognize_articy,
    recognize_engine_data,
    recognize_ink,
    recognize_table,
    recognize_yarn,
    sniff_source_format,
)


# --- table adapter: unknown columns, Chinese headers, foreign-key inference ---------------------
def test_table_infers_mapping_and_foreign_key_relations() -> None:
    rows = [
        {"id": "npc_a", "名称": "Aldric", "类型": "npc", "居住地": "loc_town", "简介": "商队头领"},
        {"id": "loc_town", "名称": "Town", "类型": "location"},
    ]
    plan = recognize_table(rows, source_file="cast.csv")

    mapping = plan.column_mapping
    assert mapping is not None
    assert mapping.id_column == "id"
    assert mapping.name_column == "名称"
    assert mapping.type_column == "类型"
    assert mapping.description_column == "简介"
    # 居住地's only value resolves to a row id -> recognized as a foreign-key (relation) column.
    assert "居住地" in mapping.relation_columns

    assert {e.id for e in plan.entities} == {"npc_a", "loc_town"}
    aldric = next(e for e in plan.entities if e.id == "npc_a")
    assert aldric.name == "Aldric" and aldric.type == "npc"
    assert aldric.description == "商队头领"

    assert len(plan.relations) == 1
    rel = plan.relations[0]
    assert (rel.source, rel.target) == ("npc_a", "loc_town")
    assert rel.method == "deterministic" and "loc_town" in rel.evidence


def test_table_mapping_override_and_dangling_target_is_flagged() -> None:
    rows = [
        {"id": "npc_a", "name": "A", "home": "loc_town"},
        {"id": "npc_b", "name": "B", "home": "loc_missing"},
    ]
    mapping = ColumnMapping(
        id_column="id", name_column="name", relation_columns={"home": "resides_in"}
    )
    plan = recognize_table(rows, mapping=mapping, canon_ids=["loc_town"])

    kinds = {(r.source, r.target, r.kind) for r in plan.relations}
    assert ("npc_a", "loc_town", "resides_in") in kinds
    assert ("npc_b", "loc_missing", "resides_in") in kinds  # kept for review, not silently dropped
    assert any("loc_missing" in w for w in plan.warnings)


def test_table_skips_rows_without_id() -> None:
    rows = [{"id": "", "name": "ghost"}, {"id": "ok", "name": "Real"}]
    plan = recognize_table(rows)
    assert {e.id for e in plan.entities} == {"ok"}
    assert any("缺少 id" in w for w in plan.warnings)


def test_infer_table_mapping_uses_canon_ids_for_foreign_keys() -> None:
    rows = [{"id": "q1", "标题": "Quest", "委托人": "npc_aldric"}]
    mapping = infer_table_mapping(rows, canon_ids=["npc_aldric"])
    assert "委托人" in mapping.relation_columns  # value resolves only via canon, still detected


# --- articy adapter ----------------------------------------------------------------------------
def _m(mtype: str, **props: object) -> dict:
    return {"Type": mtype, "Properties": props}


def _articy_export() -> dict:
    return {
        "Packages": [
            {
                "Name": "Default",
                "Models": [
                    _m("Entity", Id="0xE1", DisplayName="Hero", Text="the hero"),
                    _m("Location", Id="0xL1", DisplayName="Town"),
                    _m("DialogueFragment", Id="0xD1", Text="Hello", Speaker="0xE1"),
                    _m("Connection", Id="0xC1", Source={"IdRef": "0xE1"}, Target={"IdRef": "0xL1"}),
                    _m("Comment"),  # no Id -> skipped with a warning
                ],
            }
        ],
        "GlobalVariables": [
            {
                "Namespace": "game",
                "Variables": [{"Variable": "gold", "Type": "Integer", "Value": "0"}],
            }
        ],
    }


def test_articy_extracts_entities_relations_and_variables() -> None:
    plan = recognize_articy(_articy_export(), source_file="proj.json")

    by_id = {e.id: e for e in plan.entities}
    assert set(by_id) == {"0xE1", "0xL1", "0xD1"}
    assert by_id["0xE1"].name == "Hero"
    assert by_id["0xL1"].type == "location"
    assert by_id["0xD1"].type == "event"  # flow node
    assert by_id["0xE1"].fields.get("articy_type") == "Entity"

    rels = {(r.source, r.target, r.kind) for r in plan.relations}
    assert ("0xE1", "0xL1", "leads_to") in rels
    assert ("0xE1", "0xD1", "speaks_in") in rels

    assert plan.variables == [
        {"namespace": "game", "variable": "gold", "type": "Integer", "value": "0"}
    ]
    assert any("无 Id" in w for w in plan.warnings)


def test_articy_rejects_non_articy_json() -> None:
    plan = recognize_articy([1, 2, 3])
    assert plan.entities == [] and plan.warnings


# --- §8 guards: proactively shrink hallucination before human review ---------------------------
def test_relation_guards_keep_only_grounded_in_world_proposals() -> None:
    text = "Aldric allies with Mira in the north. Borin is unrelated."

    def _prop(
        *, target="mira", kind="allies_with", evidence="Aldric allies with Mira",
        confidence=0.9, source="aldric",
    ) -> dict:
        return {
            "source": source, "target": target, "kind": kind,
            "evidence": evidence, "confidence": confidence,
        }

    def proposer(_text: str, _known: list[str]) -> list[dict]:
        return [
            _prop(),  # valid: grounded, in-world, allowed kind, high confidence
            _prop(target="ghost"),  # out of world
            _prop(evidence="never written here"),  # ungrounded
            _prop(kind="loves"),  # kind not in the allowed vocabulary
            _prop(confidence=0.1),  # below the confidence floor
            _prop(target="aldric"),  # self-loop
            _prop(confidence=0.95),  # duplicate of the valid one
        ]

    kept, dropped = propose_relations_guarded(
        text,
        ["aldric", "mira", "borin"],
        proposer=proposer,
        allowed_kinds=["allies_with", "rivals"],
        min_confidence=0.5,
    )

    assert len(kept) == 1
    assert (kept[0].source, kept[0].target, kept[0].kind) == ("aldric", "mira", "allies_with")
    assert kept[0].method == "llm"
    # one each: out-of-world, ungrounded, bad-kind, low-confidence, self-loop, duplicate
    assert len(dropped) == 6
    assert any("闭世界" in d for d in dropped)
    assert any("无据不立" in d for d in dropped)


def test_evidence_grounded_normalizes_whitespace() -> None:
    assert evidence_grounded("Aldric  allies\nwith Mira", "Aldric allies with Mira")
    assert not evidence_grounded("Aldric allies with Mira", "betrays")
    assert not evidence_grounded("anything", "")


# --- pipeline: diff vs canon + materialization -------------------------------------------------
def test_diff_and_plan_to_bundle_roundtrip() -> None:
    rows = [
        {"id": "npc_a", "name": "Aldric", "type": "npc"},
        {"id": "loc_town", "name": "Town", "type": "location"},
    ]
    mapping = ColumnMapping(id_column="id", name_column="name", type_column="type")
    plan = recognize_table(rows, mapping=mapping)

    # Against empty canon, everything is new.
    fresh = diff_against_canon(plan, ContentBundle())
    assert set(fresh.new) == {"npc_a", "loc_town"} and not fresh.changed and not fresh.unchanged

    # Land it, then re-diff the same plan: now everything is unchanged (provenance carried through).
    canon = plan_to_bundle(plan)
    assert canon.entities["npc_a"].review_status is ReviewStatus.PENDING_REVIEW
    assert canon.entities["npc_a"].origin is Origin.HUMAN  # imported team data, not AI-authored
    again = diff_against_canon(plan, canon)
    assert set(again.unchanged) == {"npc_a", "loc_town"} and not again.new

    # Edit a canon description -> that entity reads as changed.
    canon.entities["npc_a"] = canon.entities["npc_a"].model_copy(update={"description": "edited"})
    edited = diff_against_canon(plan, canon)
    assert edited.changed == ["npc_a"]


def test_plan_to_bundle_marks_llm_relations_as_ai_draft() -> None:
    text = "Aldric allies with Mira."
    kept, _ = propose_relations_guarded(
        text,
        ["aldric", "mira"],
        proposer=lambda *_: [
            {
                "source": "aldric", "target": "mira", "kind": "allies_with",
                "evidence": "Aldric allies with Mira", "confidence": 0.9,
            }
        ],
    )
    from owcopilot.recognize.models import ImportPlan, ProposedEntity

    plan = ImportPlan(
        source_format="table",
        entities=[
            ProposedEntity(id="aldric", name="Aldric", type="npc"),
            ProposedEntity(id="mira", name="Mira", type="npc"),
        ],
        relations=kept,
    )
    bundle = plan_to_bundle(plan)
    rel = bundle.relations[0]
    assert rel.origin is Origin.AI_DRAFT and rel.review_status is ReviewStatus.PENDING_REVIEW
    assert rel.metadata["import_method"] == "llm" and rel.metadata["evidence"]


# --- ink adapter -------------------------------------------------------------------------------
def test_ink_extracts_knots_diverts_and_variables() -> None:
    script = (
        "VAR gold = 0\n"
        "CONST MAX = 10\n"
        "=== paris ===\n"
        "We arrive in Paris.\n"
        "-> cafe\n"
        "= cafe\n"
        "A small cafe.\n"
        "-> london\n"
        "=== london ===\n"
        "The end.\n"
        "-> END\n"
    )
    plan = recognize_ink(script, source_file="story.ink")

    assert {e.id for e in plan.entities} == {"paris", "paris.cafe", "london"}
    rels = {(r.source, r.target, r.kind) for r in plan.relations}
    assert ("paris", "paris.cafe", "leads_to") in rels  # bare stitch resolves within the knot
    assert ("paris.cafe", "london", "leads_to") in rels
    assert not any(r.target == "END" for r in plan.relations)  # control-flow target, not a node
    assert {v["name"] for v in plan.variables} == {"gold", "MAX"}


# --- Yarn adapter ------------------------------------------------------------------------------
def test_yarn_extracts_nodes_jumps_speakers_and_variables() -> None:
    script = (
        "title: Start\n"
        "---\n"
        "Narrator: Welcome.\n"
        "Aldric: Hello, traveler.\n"
        "<<declare $gold = 0>>\n"
        "<<jump Market>>\n"
        "===\n"
        "title: Market\n"
        "---\n"
        "Mira: Want to buy something?\n"
        "[[Leave|Start]]\n"
        "===\n"
    )
    plan = recognize_yarn(script, source_file="dlg.yarn")

    ids = {e.id for e in plan.entities}
    assert {"Start", "Market"} <= ids
    assert {"Narrator", "Aldric", "Mira"} <= ids  # speakers become npc entities
    rels = {(r.source, r.target, r.kind) for r in plan.relations}
    assert ("Start", "Market", "leads_to") in rels  # <<jump Market>> (forward reference resolves)
    assert ("Market", "Start", "leads_to") in rels  # [[Leave|Start]] option link
    assert ("Aldric", "Start", "speaks_in") in rels
    assert ("Mira", "Market", "speaks_in") in rels
    assert {v["name"] for v in plan.variables} == {"gold"}


# --- engine data: UE DataTable / Unity ScriptableObject ----------------------------------------
def test_ue_datatable_rowhandles_and_foreign_keys() -> None:
    data = [
        {
            "Name": "Quest_Intro", "Type": "Quest",
            "GiverNPC": {"RowName": "NPC_Aldric", "DataTable": "/Game/NPCs"},
            "Reward": 100, "Items": [{"RowName": "Item_Sword"}],
        },
        {"Name": "NPC_Aldric", "Type": "NPC", "Faction": "Fac_Iron"},
        {"Name": "Item_Sword", "Type": "Item"},
        {"Name": "Fac_Iron", "Type": "Faction"},
    ]
    plan = recognize_engine_data(data, dialect="ue", source_file="Quests.json")

    assert {e.id for e in plan.entities} == {"Quest_Intro", "NPC_Aldric", "Item_Sword", "Fac_Iron"}
    rels = {(r.source, r.target, r.kind) for r in plan.relations}
    assert ("Quest_Intro", "NPC_Aldric", "GiverNPC") in rels  # RowHandle
    assert ("Quest_Intro", "Item_Sword", "Items") in rels  # list of RowHandles
    assert ("NPC_Aldric", "Fac_Iron", "Faction") in rels  # bare-string foreign key
    quest = next(e for e in plan.entities if e.id == "Quest_Intro")
    assert quest.fields.get("Reward") == 100  # scalar kept as a field


def test_unity_asset_refs_are_flagged_not_invented() -> None:
    data = [
        {
            "m_Name": "HeroData", "Type": "Character",
            "homeRegion": {"m_FileID": 11400000, "m_PathID": 5566},
            "alliedFaction": "IronGuard",
        },
        {"m_Name": "IronGuard", "Type": "Faction"},
    ]
    plan = recognize_engine_data(data, dialect="unity", source_file="Assets.json")

    assert {e.id for e in plan.entities} == {"HeroData", "IronGuard"}
    rels = {(r.source, r.target, r.kind) for r in plan.relations}
    assert ("HeroData", "IronGuard", "alliedFaction") in rels
    assert "homeRegion" in plan.unmapped  # unresolvable fileID/PathID ref — kept, not guessed
    assert any("GUID" in w or "fileID" in w for w in plan.warnings)


# --- action + CLI: end to end against a seeded project -----------------------------------------
def _seed_canon(content_root) -> None:
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "loc_town": Entity(id="loc_town", name="Town", type=EntityType.LOCATION),
            }
        )
    )


def test_recognize_import_action_dry_run_then_apply(tmp_path) -> None:
    content_root = tmp_path / "content"
    _seed_canon(content_root)
    csv_path = tmp_path / "cast.csv"
    csv_path.write_text(
        "id,名称,类型,居住地\n"
        "npc_a,Aldric,npc,loc_town\n"
        "npc_b,Bob,npc,loc_missing\n",
        encoding="utf-8",
    )
    mapping = {
        "id_column": "id",
        "name_column": "名称",
        "type_column": "类型",
        "relation_columns": {"居住地": "resides_in"},
    }

    dry = recognize_import_action(
        content_root, source_format="table", input_path=str(csv_path), field_mapping=mapping
    )
    assert dry["applied"] is False
    assert set(dry["new"]) == {"npc_a", "npc_b"}
    assert dry["summary"]["relations"] == 2
    assert dry.get("review_item_id") is None
    assert any("loc_missing" in w for w in dry["warnings"])

    applied = recognize_import_action(
        content_root,
        source_format="table",
        input_path=str(csv_path),
        field_mapping=mapping,
        apply=True,
    )
    assert applied["applied"] is True
    assert applied["review_item_id"] is not None
    assert "totals" in applied["audit_preview"]
    # nothing landed in canon yet — it's staged for human review only
    assert set(ContentStore(content_root).load().entities) == {"loc_town"}


def test_cli_recognize_articy(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    _seed_canon(content_root)
    export_path = tmp_path / "proj.json"
    export_path.write_text(json.dumps(_articy_export()), encoding="utf-8")

    code = main(
        [
            "recognize",
            "--content-root", str(content_root),
            "--source-format", "articy",
            "--input", str(export_path),
        ]
    )
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["source_format"] == "articy"
    assert body["summary"]["entities"] == 3
    assert body["applied"] is False


def test_cli_recognize_ink_apply(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    _seed_canon(content_root)
    ink_path = tmp_path / "story.ink"
    ink_path.write_text("=== start ===\n-> finish\n=== finish ===\n-> END\n", encoding="utf-8")

    code = main(
        [
            "recognize",
            "--content-root", str(content_root),
            "--source-format", "ink",
            "--input", str(ink_path),
            "--apply",
        ]
    )
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["source_format"] == "ink"
    assert set(body["new"]) == {"start", "finish"}
    assert body["applied"] is True and body["review_item_id"] is not None


# --- format sniffing ---------------------------------------------------------------------------
def test_sniff_source_format_by_extension_and_content() -> None:
    assert sniff_source_format("cast.csv", "id,name") == "table"
    assert sniff_source_format("story.ink", "=== x ===") == "ink"
    assert sniff_source_format("dlg.yarn", "title: X\n---\n") == "yarn"
    assert sniff_source_format("p.json", '{"Packages": []}') == "articy"
    assert sniff_source_format("q.json", '[{"Name": "R", "Ref": {"RowName": "A"}}]') == "ue"
    assert sniff_source_format("a.json", '[{"m_Name": "A"}]') == "unity"
    assert sniff_source_format("t.json", '[{"id": "a"}]') == "table"
    # no extension -> fall back to content shape
    assert sniff_source_format("blob", "title: Start\n---\n") == "yarn"
    assert sniff_source_format("blob", "=== knot ===\n") == "ink"


def test_plan_exposes_columns_for_mapping_ui() -> None:
    plan = recognize_table([{"id": "a", "名称": "A", "阵营": "f1"}])
    assert plan.columns == ["id", "名称", "阵营"]


# --- upload (base64) incl. binary XLSX + auto sniff --------------------------------------------
def test_recognize_content_action_base64_csv_auto(tmp_path) -> None:
    content_root = tmp_path / "content"
    _seed_canon(content_root)
    b64 = base64.b64encode(b"id,name,type\nnpc_z,Zed,npc\n").decode()
    res = recognize_content_action(
        content_root, source_format="auto", content_base64=b64, filename="cast.csv"
    )
    assert res["source_format"] == "table"
    assert "npc_z" in res["new"]


def test_recognize_content_action_xlsx_upload(tmp_path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["id", "名称", "类型"])
    sheet.append(["fac_a", "甲", "faction"])
    buf = io.BytesIO()
    workbook.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode()

    content_root = tmp_path / "content"
    _seed_canon(content_root)
    res = recognize_content_action(
        content_root, source_format="table", content_base64=b64, filename="阵营.xlsx"
    )
    assert "fac_a" in res["new"]
    entity = next(e for e in res["plan"]["entities"] if e["id"] == "fac_a")
    assert entity["name"] == "甲" and entity["type"] == "faction"


# --- apply an edited plan (human dropped a proposal) -------------------------------------------
def test_recognize_apply_plan_stages_only_kept(tmp_path) -> None:
    content_root = tmp_path / "content"
    _seed_canon(content_root)
    mapping = ColumnMapping(id_column="id", name_column="name", type_column="type")
    plan = recognize_table(
        [{"id": "a", "name": "A", "type": "npc"}, {"id": "b", "name": "B", "type": "npc"}],
        mapping=mapping,
    ).model_dump(mode="json")
    plan["entities"] = [e for e in plan["entities"] if e["id"] == "a"]  # human dropped "b"

    res = recognize_apply_plan_action(content_root, plan=plan)
    assert res["applied"] is True and res["new"] == ["a"]
    assert res["review_item_id"] is not None


# --- mapping templates -------------------------------------------------------------------------
def test_mapping_templates_save_list_delete(tmp_path) -> None:
    content_root = tmp_path / "content"
    _seed_canon(content_root)
    save_mapping_template_action(
        content_root, name="阵营表", mapping={"id_column": "编号", "name_column": "名称"}
    )
    listed = list_mapping_templates_action(content_root)
    assert listed["templates"]["阵营表"]["id_column"] == "编号"
    delete_mapping_template_action(content_root, name="阵营表")
    assert "阵营表" not in list_mapping_templates_action(content_root)["templates"]


# --- §8 LLM proposer (wired, default-off; offline double for the test) -------------------------
def test_llm_proposer_extracts_json_and_guards_apply() -> None:
    class FakeGateway:
        def complete(self, *, task: str, system: str, user: str) -> str:
            return (
                '前面有废话 [{"source": "a", "target": "b", "kind": "allies_with", '
                '"evidence": "A allies with B", "confidence": 0.9}] 后面也有'
            )

    proposer = build_llm_relation_proposer(FakeGateway())
    kept, dropped = propose_relations_guarded("A allies with B.", ["a", "b"], proposer=proposer)
    assert len(kept) == 1 and kept[0].method == "llm"


def test_enable_llm_offline_adds_guarded_relation(tmp_path) -> None:
    content_root = tmp_path / "content"
    _seed_canon(content_root)
    csv = "id,name,type,desc\nnpc_a,Aldric,npc,Aldric and Mira are allies\nnpc_m,Mira,npc,broker\n"
    res = recognize_content_action(
        content_root, source_format="table", content=csv, filename="c.csv",
        enable_llm=True, llm_mode="offline",
    )
    assert any(r["method"] == "llm" for r in res["plan"]["relations"])
